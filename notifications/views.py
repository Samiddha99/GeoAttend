from django.conf import settings
from django.shortcuts import get_object_or_404, render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from academics.selectors import batches_for, departments_for, subjects_for
from core.decorators import role_required
from core.http import fail, ok
from dashboard.filters import ReportFilters

from . import message_templates as mt
from . import services as svc
from . import template_service as ts
from .models import AlertCampaign, WhatsAppTemplate
from .whatsapp import is_configured, send_whatsapp

HEAD, HOD, TEACHER = "HEAD", "HOD", "TEACHER"
MAX_PER_SEND = 500


# --------------------------------------------------------------------------- #
#  Page
# --------------------------------------------------------------------------- #
@role_required(HEAD, HOD, TEACHER)
@ensure_csrf_cookie
def alerts_page(request):
    from django.utils import timezone

    # This screen only offers templates WhatsApp has approved, so a verdict
    # that landed since the last visit has to be picked up before the lists
    # below are built. ts.autosync() polls only undecided templates, throttles
    # repeats and uses a short timeout, so the usual case costs nothing.
    ts.autosync(request.user.institute)

    return render(request, "notifications/alerts.html", {
        "departments": departments_for(request.user),
        "batches": batches_for(request.user).select_related("department"),
        "subjects": subjects_for(request.user),
        "threshold": settings.ATTENDANCE["LOW_ATTENDANCE_THRESHOLD"],
        "default_start": f"{timezone.localdate().year}-01-01",
        "default_end": timezone.localdate().isoformat(),
        "placeholders": mt.PLACEHOLDERS,
        "whatsapp_is_live": is_configured() and settings.WHATSAPP.get("ENABLED", True),
        "student_templates": ts.templates_for(
            request.user, WhatsAppTemplate.Audience.STUDENT, approved_only=True),
        "guardian_templates": ts.templates_for(
            request.user, WhatsAppTemplate.Audience.GUARDIAN, approved_only=True),
        "can_manage_templates": request.user.is_head,
    })


# --------------------------------------------------------------------------- #
#  Drafting
# --------------------------------------------------------------------------- #
@role_required(HEAD, HOD, TEACHER)
@require_GET
def api_defaults(request):
    """The starting wording for a scope — senders edit it before sending."""
    scope = request.GET.get("scope", svc.OVERALL).upper()
    if scope not in (svc.OVERALL, svc.SUBJECT):
        return fail("Unknown alert scope.")
    return ok({
        "scope": scope,
        "templates": mt.defaults_for(scope),
        "placeholders": mt.PLACEHOLDERS,
    })


def _parse_request(request, source):
    """Shared validation for the preview and send endpoints."""
    scope = (source.get("scope") or svc.OVERALL).upper()
    if scope not in (svc.OVERALL, svc.SUBJECT):
        return None, fail("Unknown alert scope.")

    try:
        threshold = float(source.get("threshold", settings.ATTENDANCE["LOW_ATTENDANCE_THRESHOLD"]))
    except (TypeError, ValueError):
        return None, fail("Enter a valid threshold percentage.")
    if not 1 <= threshold <= 100:
        return None, fail("The threshold must be between 1 and 100.")

    subject = None
    if scope == svc.SUBJECT:
        subject = svc.resolve_subject(request.user, source.get("subject"))
        if subject is None:
            return None, fail("Choose a subject you are allowed to report on.", status=403)

    filters = ReportFilters.from_request(request)
    return {"scope": scope, "threshold": threshold, "subject": subject,
            "filters": filters}, None


@role_required(HEAD, HOD, TEACHER)
@require_GET
def api_recipients(request):
    """Who would be messaged, given the current scope/threshold/filters."""
    parsed, error = _parse_request(request, request.GET)
    if error:
        return error

    recipients = svc.build_recipients(
        request.user, parsed["filters"], parsed["threshold"],
        parsed["scope"], parsed["subject"],
    )
    rows = [svc.serialise(r) for r in recipients]
    reachable = sum(1 for r in rows if r["guardian_number"])
    reachable_students = sum(1 for r in rows if r["student_number"])
    activated = sum(1 for r in recipients if r["profile"].user.registration_completed)
    return ok({
        "rows": rows,
        "count": len(rows),
        "reachable_guardians": reachable,
        "missing_guardians": len(rows) - reachable,
        "reachable_students": reachable_students,
        "missing_student_numbers": len(rows) - reachable_students,
        "emailable": activated,
        "threshold": parsed["threshold"],
        "scope": parsed["scope"],
        "subject": parsed["subject"].code if parsed["subject"] else "",
        "range": parsed["filters"].label,
        "capped": len(rows) > MAX_PER_SEND,
        "max_per_send": MAX_PER_SEND,
    })


def _resolve_template(request, field, audience):
    """
    Turn a posted template id into an approved template, or an error.

    WhatsApp only accepts wording it has already approved, so the id is checked
    against this institute's approved list — never trusted from the form.
    """
    raw = request.POST.get(field) or ""
    if not raw:
        return None, fail(
            f"Choose an approved {audience.lower()} WhatsApp template. "
            "Your head of institute registers them under WhatsApp templates.")
    template = ts.templates_for(
        request.user, audience, approved_only=True
    ).filter(pk=raw).first()
    if template is None:
        return None, fail(
            "That template is not approved for use. It may have been rejected, "
            "removed, or belong to another institute.", status=403)
    return template, None


@role_required(HEAD, HOD, TEACHER)
@require_POST
def api_preview(request):
    """Render the drafts against one real recipient."""
    parsed, error = _parse_request(request, request.POST)
    if error:
        return error

    recipients = svc.build_recipients(
        request.user, parsed["filters"], parsed["threshold"],
        parsed["scope"], parsed["subject"],
    )
    if not recipients:
        return fail("Nobody is below this threshold, so there is nothing to preview.")

    wanted = request.POST.get("student")
    chosen = next((r for r in recipients if str(r["student_id"]) == str(wanted)), recipients[0])
    drafts = {
        "email_subject": request.POST.get("email_subject", ""),
        "email_body": request.POST.get("email_body", ""),
    }
    student_template = ts.templates_for(
        request.user, WhatsAppTemplate.Audience.STUDENT, approved_only=True
    ).filter(pk=request.POST.get("student_template") or 0).first()
    guardian_template = ts.templates_for(
        request.user, WhatsAppTemplate.Audience.GUARDIAN, approved_only=True
    ).filter(pk=request.POST.get("guardian_template") or 0).first()
    return ok(svc.preview(chosen, drafts, student_template, guardian_template))


# --------------------------------------------------------------------------- #
#  Sending
# --------------------------------------------------------------------------- #
@role_required(HEAD, HOD, TEACHER)
@require_POST
def api_send(request):
    parsed, error = _parse_request(request, request.POST)
    if error:
        return error

    channels = {
        "email": request.POST.get("email_students") == "1",
        "student_whatsapp": request.POST.get("whatsapp_students") == "1",
        "whatsapp": request.POST.get("whatsapp_guardians") == "1",
    }
    if not any(channels.values()):
        return fail("Pick at least one channel — student email, student WhatsApp "
                    "or guardian WhatsApp.")

    drafts = {
        "email_subject": request.POST.get("email_subject", "").strip(),
        "email_body": request.POST.get("email_body", "").strip(),
    }
    if channels["email"] and not (drafts["email_subject"] and drafts["email_body"]):
        return fail("The email needs both a subject line and a body.")

    # WhatsApp wording cannot be typed here — only an approved template may go out.
    student_template = guardian_template = None
    if channels["student_whatsapp"]:
        student_template, error = _resolve_template(
            request, "student_template", WhatsAppTemplate.Audience.STUDENT)
        if error:
            return error
    if channels["whatsapp"]:
        guardian_template, error = _resolve_template(
            request, "guardian_template", WhatsAppTemplate.Audience.GUARDIAN)
        if error:
            return error

    student_ids = request.POST.getlist("students[]") or None
    if student_ids is not None:
        if not student_ids:
            return fail("No students selected.")
        student_ids = svc.scope_guard(request.user, student_ids)
        if not student_ids:
            return fail("None of the selected students are in your scope.", status=403)
        if len(student_ids) > MAX_PER_SEND:
            return fail(f"Please send to at most {MAX_PER_SEND} students at a time.")

    campaign = svc.send_campaign(
        user=request.user,
        filters=parsed["filters"],
        threshold=parsed["threshold"],
        scope=parsed["scope"],
        subject=parsed["subject"],
        drafts=drafts,
        channels=channels,
        student_ids=student_ids,
        student_template=student_template,
        guardian_template=guardian_template,
    )

    if not campaign.total_recipients:
        return fail("Nobody matched — nothing was sent.")

    bits = []
    if campaign.email_students:
        bits.append(f"{campaign.email_sent} email(s)")
    if campaign.whatsapp_students:
        bits.append(f"{campaign.student_whatsapp_sent} WhatsApp to students")
    if campaign.whatsapp_guardians:
        bits.append(f"{campaign.whatsapp_sent} WhatsApp to guardians")
    message = "Sent " + ", ".join(bits) + "."
    if campaign.failed_total:
        message += f" {campaign.failed_total} failed."
    if campaign.skipped:
        message += f" {campaign.skipped} skipped."

    return ok({"campaign_id": campaign.id, **_campaign_dict(campaign)}, message=message)


# --------------------------------------------------------------------------- #
#  History
# --------------------------------------------------------------------------- #
def _visible_campaigns(user):
    qs = AlertCampaign.objects.filter(institute=user.institute).select_related(
        "created_by", "subject"
    )
    if user.role == TEACHER:
        return qs.filter(created_by=user)
    if user.role == HOD:
        return qs.filter(created_by__department=user.department)
    return qs


def _campaign_dict(campaign):
    return {
        "id": campaign.id,
        "when": campaign.created_at.strftime("%d %b %Y, %H:%M"),
        "scope": campaign.get_scope_display(),
        "subject": campaign.subject.code if campaign.subject else "—",
        # Blank rather than a dash for an overall alert: the column renders a
        # pill, and a pill reading "—" looks like a type called "—".
        "subject_type": campaign.subject.subject_type if campaign.subject else "",
        "threshold": campaign.threshold,
        "range": f"{campaign.date_from:%d %b} – {campaign.date_to:%d %b %Y}",
        "by": campaign.created_by.get_full_name() if campaign.created_by else "—",
        "channels": campaign.channel_label,
        "recipients": campaign.total_recipients,
        "email_sent": campaign.email_sent,
        "student_whatsapp_sent": campaign.student_whatsapp_sent,
        "whatsapp_sent": campaign.whatsapp_sent,
        "failed": campaign.failed_total,
        "skipped": campaign.skipped,
    }


@role_required(HEAD, HOD, TEACHER)
@require_GET
def api_campaigns(request):
    rows = [_campaign_dict(c) for c in _visible_campaigns(request.user)[:100]]
    return ok({"rows": rows})


@role_required(HEAD, HOD, TEACHER)
@require_GET
def api_campaign_detail(request, pk):
    campaign = get_object_or_404(_visible_campaigns(request.user), pk=pk)
    rows = [{
        "student": d.student.name,
        "roll": d.student.class_roll,
        "channel": d.channel,
        "target": d.target,
        "percentage": round(d.percentage, 1),
        "status": d.status,
        "error": d.error,
        "provider_id": d.provider_id,
        "body": d.body,
        "subject_line": d.subject_line,
    } for d in campaign.deliveries.select_related("student", "student__user")]
    return ok({"campaign": _campaign_dict(campaign), "rows": rows})


@role_required(HEAD, HOD)
@require_POST
def api_whatsapp_test(request):
    """Fire one message at a number of the sender's choosing, to prove wiring."""
    to = (request.POST.get("to") or "").strip()
    if not to:
        return fail("Enter a number to test.", {"to": "This field is required."})
    body = request.POST.get("message") or (
        f"Test message from {settings.SITE_NAME}. If you can read this, "
        "WhatsApp delivery is wired up correctly."
    )
    result = send_whatsapp(to, body)
    if not result.ok:
        return fail(f"Delivery failed: {result.error}")
    note = (" Twilio accepted it (status: %s)." % result.status if is_configured()
            else " Console mode — nothing actually left the server.")
    return ok({"provider_id": result.provider_id, "status": result.status},
              message=f"Sent to {to}.{note}")


# --------------------------------------------------------------------------- #
#  WhatsApp templates — head of institute only
#
#  WhatsApp refuses business-initiated free-form text, so the institute registers
#  its wording once and Meta approves it.  Everyone else picks from the approved
#  list; only the head can write or submit one.
# --------------------------------------------------------------------------- #
@role_required(HEAD)
@ensure_csrf_cookie
def templates_page(request):
    return render(request, "notifications/templates.html", {
        "placeholders": mt.PLACEHOLDERS,
        "defaults": {
            "STUDENT": mt.DEFAULT_STUDENT_WHATSAPP_OVERALL,
            "GUARDIAN": mt.DEFAULT_WHATSAPP_OVERALL,
            "STUDENT_SUBJECT": mt.DEFAULT_STUDENT_WHATSAPP_SUBJECT,
            "GUARDIAN_SUBJECT": mt.DEFAULT_WHATSAPP_SUBJECT,
        },
        "whatsapp_is_live": is_configured(),
        "categories": ["UTILITY", "MARKETING", "AUTHENTICATION"],
    })


@role_required(HEAD)
@require_GET
def api_templates(request):
    # Synced here rather than in templates_page() so the page paints first and
    # the table's loading skeleton covers the wait.
    ts.autosync(request.user.institute)
    rows = [ts.serialise(t) for t in ts.templates_for(request.user)
            .select_related("created_by")]
    return ok({
        "rows": rows,
        "live": is_configured(),
        "approved": sum(1 for r in rows if r["is_sendable"]),
    })


@role_required(HEAD)
@require_POST
def api_template_create(request):
    audience = (request.POST.get("audience") or "").upper()
    if audience not in WhatsAppTemplate.Audience.values:
        return fail("Choose whether this template is for students or guardians.")

    name = (request.POST.get("name") or "").strip()
    if not name:
        return fail("Give the template a name.", {"name": "This field is required."})

    body = request.POST.get("body") or ""
    error = ts.validate_body(body)
    if error:
        return fail(error, {"body": error})

    category = (request.POST.get("category") or "UTILITY").upper()
    if category not in ("UTILITY", "MARKETING", "AUTHENTICATION"):
        return fail("Unknown WhatsApp category.")

    template = ts.create_template(
        institute=request.user.institute, user=request.user, audience=audience,
        name=name, body=body, category=category,
        language=(request.POST.get("language") or "en").strip()[:10],
    )
    data = ts.serialise(template)
    if template.status == WhatsAppTemplate.Status.FAILED:
        return ok(data, message=(
            "Saved, but Twilio rejected the submission: "
            f"{template.last_error} You can fix it and resubmit."))
    return ok(data, message=(
        f"'{template.name}' sent to WhatsApp for approval. Status: "
        f"{template.get_status_display()}. Approval usually takes minutes to a "
        "few hours — use Refresh to check."))


@role_required(HEAD)
@require_POST
def api_template_resubmit(request, pk):
    template = get_object_or_404(ts.templates_for(request.user), pk=pk)
    if not template.is_editable:
        return fail("Only a draft or a failed submission can be resubmitted.")
    ts.submit_template(template, user=request.user)
    if template.status == WhatsAppTemplate.Status.FAILED:
        return fail(f"Twilio rejected it again: {template.last_error}")
    return ok(ts.serialise(template), message="Resubmitted for approval.")


@role_required(HEAD)
@require_POST
def api_template_sync(request, pk=None):
    """Poll Twilio for the latest verdict — one template, or every pending one."""
    if pk:
        template = get_object_or_404(ts.templates_for(request.user), pk=pk)
        ts.sync_template(template)
        if template.last_error:
            return fail(template.last_error)
        return ok(ts.serialise(template),
                  message=f"Status: {template.get_status_display()}.")

    updated = ts.sync_all(request.user.institute)
    if not updated:
        return ok({"rows": []}, message="Nothing is awaiting approval.")
    approved = sum(1 for t in updated if t.status == WhatsAppTemplate.Status.APPROVED)
    return ok({"rows": [ts.serialise(t) for t in updated]},
              message=f"Checked {len(updated)} template(s); {approved} now approved.")


@role_required(HEAD)
@require_POST
def api_template_delete(request, pk):
    template = get_object_or_404(ts.templates_for(request.user), pk=pk)
    error = None
    if template.content_sid:
        from .whatsapp import delete_content

        error = delete_content(template.content_sid)
    template.is_active = False
    template.save(update_fields=["is_active"])
    note = f" (Twilio said: {error})" if error else ""
    return ok(message=f"'{template.name}' removed from the picker.{note}")


@role_required(HEAD)
@require_POST
def api_template_preview(request, pk):
    """Render a template against a real low-attendance student."""
    template = get_object_or_404(ts.templates_for(request.user), pk=pk)
    f = ReportFilters.from_request(request)
    recipients = svc.build_recipients(request.user, f, 100)
    if not recipients:
        return fail("No student data yet to preview against.")
    context = recipients[0]["context"]
    return ok({
        "student": recipients[0]["name"],
        "rendered": template.preview(context),
        "variables": template.content_variables(context),
    })
