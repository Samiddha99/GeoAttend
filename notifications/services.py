"""
Turning "who is below 75%?" into rendered, delivered messages.

The recipient list is always recomputed server-side from the same analytics the
dashboards use, so a tampered form cannot make the system message somebody who
is not actually below the threshold, or somebody outside the sender's scope.
"""
import logging

from django.conf import settings
from django.db import transaction

from academics.models import StudentProfile
from accounts.models import ActivityLog
from core.utils import clean_object_id
from dashboard.services import scoped_students, student_report

from . import message_templates as mt
from .mailer import send_template_mail
from .models import AlertCampaign, AlertDelivery
from .whatsapp import normalise_msisdn, send_whatsapp

log = logging.getLogger("geoattend")

OVERALL, SUBJECT = "OVERALL", "SUBJECT"


# --------------------------------------------------------------------------- #
#  Who should hear from us
# --------------------------------------------------------------------------- #
def _campaign_institute(user, filters):
    """The institute a university's alert campaign belongs to."""
    from accounts.models import Institute

    if filters.institute:
        return Institute.objects.filter(pk=filters.institute).first()
    return None


def build_recipients(user, filters, threshold, scope=OVERALL, subject=None):
    """
    Students inside `user`'s scope whose attendance is below `threshold`.

    For SUBJECT scope the percentage is that subject's alone; `filters.subject`
    is set so every downstream number refers to it.
    """
    if scope == SUBJECT:
        if subject is None:
            raise ValueError("A subject is required for subject-specific alerts.")
        filters.subject = subject.id

    rows = student_report(user, filters)
    profiles = {
        p.id: p
        for p in StudentProfile.objects.filter(
            id__in=[r["student_id"] for r in rows]
        ).select_related("user", "batch", "department", "department__institute")
    }

    recipients = []
    for row in rows:
        if not row["held"]:
            continue                       # no classes held → nothing to judge
        if row["percentage"] >= threshold:
            continue
        profile = profiles.get(row["student_id"])
        if profile is None or not profile.user.is_active:
            continue
        recipients.append(_recipient(profile, row, threshold, scope, subject, filters, user))

    recipients.sort(key=lambda r: r["percentage"])
    return recipients


def _recipient(profile, row, threshold, scope, subject, filters, sender):
    guardian_number, guardian_error = normalise_msisdn(profile.guardian_mobile)
    # The student's own WhatsApp number: roster column first, then whatever they
    # entered on their profile when activating the account.
    raw_student_mobile = profile.mobile or profile.user.phone
    student_number, student_error = normalise_msisdn(raw_student_mobile)
    subject_rows = row.get("subjects") or []
    if scope == SUBJECT:
        match = next((s for s in subject_rows if s["subject_id"] == subject.id), None)
        subject_code = subject.code
        subject_name = subject.name
        held, attended = (match or row)["held"], (match or row)["attended"]
    else:
        subject_code = subject_name = ""
        held, attended = row["held"], row["attended"]

    breakdown = "\n".join(
        f"  • {s['code']} — {s['name']}: {s['attended']}/{s['held']} ({s['percentage']}%)"
        for s in subject_rows
    ) or "  • No subject data in this period."

    context = {
        "student_name": profile.name,
        "first_name": profile.name.split(" ")[0],
        "class_roll": profile.class_roll or "—",
        "exam_roll": profile.exam_roll or "—",
        "roll_number": profile.class_roll or "—",   # legacy placeholder name
        "batch": profile.batch.label,
        "department": profile.department.name,
        "institute": profile.department.institute.name,
        "guardian_name": profile.guardian_name or "Guardian",
        "student_email": profile.email,
        "student_mobile": student_number or raw_student_mobile or "—",
        "percentage": f"{row['percentage']:.1f}",
        "threshold": f"{threshold:g}",
        "shortfall": f"{max(threshold - row['percentage'], 0):.1f}",
        "held": held,
        "attended": attended,
        "missed": max(held - attended, 0),
        "subject_code": subject_code,
        "subject_name": subject_name,
        "subject_list": breakdown,
        "from_date": filters.start.strftime("%d %b %Y"),
        "to_date": filters.end.strftime("%d %b %Y"),
        "sender_name": sender.get_full_name(),
        "sender_role": sender.get_role_display(),
    }
    return {
        "student_id": profile.id,
        "profile": profile,
        "name": profile.name,
        "roll": profile.class_roll,
        "email": profile.email,
        "batch": profile.batch.label,
        "department": profile.department.name,
        "guardian_name": profile.guardian_name,
        "guardian_mobile": profile.guardian_mobile,
        "guardian_number": guardian_number,
        "guardian_error": guardian_error or "",
        "student_mobile": raw_student_mobile or "",
        "student_number": student_number,
        "student_error": student_error or "",
        "percentage": row["percentage"],
        "held": held,
        "attended": attended,
        "missed": max(held - attended, 0),
        "context": context,
    }


def serialise(recipient):
    """Trim a recipient down to what the browser needs."""
    return {
        k: recipient[k] for k in (
            "student_id", "name", "roll", "email", "batch", "department",
            "guardian_name", "guardian_mobile", "guardian_number", "guardian_error",
            "student_mobile", "student_number", "student_error",
            "percentage", "held", "attended", "missed",
        )
    }


# --------------------------------------------------------------------------- #
#  Preview
# --------------------------------------------------------------------------- #
def preview(recipient, drafts, student_template=None, guardian_template=None):
    """Render one recipient's messages exactly as they will be delivered."""
    context = recipient["context"]
    return {
        "student": recipient["name"],
        "email_to": recipient["email"],
        "student_whatsapp_to": recipient["student_number"] or recipient["student_mobile"],
        "student_whatsapp_error": recipient["student_error"],
        "whatsapp_to": recipient["guardian_number"] or recipient["guardian_mobile"],
        "whatsapp_error": recipient["guardian_error"],
        "email_subject": mt.render(drafts.get("email_subject", ""), context),
        "email_body": mt.render(drafts.get("email_body", ""), context),
        "student_whatsapp_body": (student_template.preview(context)
                                  if student_template else ""),
        "whatsapp_body": (guardian_template.preview(context)
                          if guardian_template else ""),
        "student_template": student_template.name if student_template else "",
        "guardian_template": guardian_template.name if guardian_template else "",
        "unknown": mt.unknown_placeholders(
            drafts.get("email_subject", ""), drafts.get("email_body", ""),
        ),
    }


# --------------------------------------------------------------------------- #
#  Delivery
# --------------------------------------------------------------------------- #
def send_campaign(*, user, filters, threshold, scope, subject, drafts,
                  channels, student_ids=None, student_template=None,
                  guardian_template=None):
    """
    Recompute, render and deliver. Returns the persisted :class:`AlertCampaign`.

    `student_ids` narrows an already-qualifying list (the sender may untick
    rows); it can never widen it.
    """
    recipients = build_recipients(user, filters, threshold, scope, subject)
    if student_ids is not None:
        # Both sides via str(): r["student_id"] comes from the ORM as an
        # ObjectId, student_ids comes from the request as hex text.
        wanted = {str(i) for i in student_ids}
        recipients = [r for r in recipients if str(r["student_id"]) in wanted]

    email_on = bool(channels.get("email"))
    student_wa_on = bool(channels.get("student_whatsapp"))
    whatsapp_on = bool(channels.get("whatsapp"))

    # A university has no institute of its own, so the campaign is filed
    # against the institute it was focused on when sending. Sending across
    # every institute at once is refused upstream for exactly this reason:
    # one campaign row cannot honestly belong to forty colleges.
    campaign = AlertCampaign.objects.create(
        institute=(user.institute if not getattr(user, "is_university", False)
                   else _campaign_institute(user, filters)),
        created_by=user,
        scope=scope,
        subject=subject,
        threshold=threshold,
        date_from=filters.start,
        date_to=filters.end,
        email_students=email_on,
        whatsapp_students=student_wa_on,
        whatsapp_guardians=whatsapp_on,
        email_subject=drafts.get("email_subject", "")[:250],
        email_body=drafts.get("email_body", ""),
        # Store the approved wording so the audit trail survives the template
        # later being edited, rejected or deleted.
        student_whatsapp_body=student_template.body if student_template else "",
        whatsapp_body=guardian_template.body if guardian_template else "",
        student_template=student_template,
        guardian_template=guardian_template,
        total_recipients=len(recipients),
    )

    deliveries = []
    tally = {"email_sent": 0, "email_failed": 0,
             "student_whatsapp_sent": 0, "student_whatsapp_failed": 0,
             "whatsapp_sent": 0, "whatsapp_failed": 0, "skipped": 0}

    def deliver_whatsapp(recipient, *, channel, template, number_key,
                         error_key, raw_key, sent_key, failed_key, missing_note):
        """One WhatsApp send — identical logic for the student and the guardian."""
        context = recipient["context"]
        # `body` is only for the delivery report; WhatsApp renders the approved
        # template itself from the numbered variables.
        body = template.preview(context)
        variables = template.content_variables(context)
        number = recipient[number_key]
        target = number or recipient[raw_key]
        if not number:
            tally["skipped"] += 1
            deliveries.append(AlertDelivery(
                campaign=campaign, student=recipient["profile"], channel=channel,
                target=target or "—", percentage=recipient["percentage"], body=body,
                status=AlertDelivery.Status.SKIPPED,
                error=(recipient[error_key] or missing_note)[:300],
            ))
            return
        result = send_whatsapp(target, body, content_sid=template.content_sid,
                               content_variables=variables)
        tally[sent_key if result.ok else failed_key] += 1
        deliveries.append(AlertDelivery(
            campaign=campaign, student=recipient["profile"], channel=channel,
            target=target, percentage=recipient["percentage"], body=body,
            status=AlertDelivery.Status.SENT if result.ok else AlertDelivery.Status.FAILED,
            error=result.error[:300], provider_id=result.provider_id[:120],
        ))

    for recipient in recipients:
        ctx = recipient["context"]

        if email_on:
            subject_line = mt.render(drafts.get("email_subject", ""), ctx)
            body = mt.render(drafts.get("email_body", ""), ctx)
            if not recipient["profile"].user.registration_completed:
                tally["skipped"] += 1
                deliveries.append(AlertDelivery(
                    campaign=campaign, student=recipient["profile"],
                    channel=AlertDelivery.Channel.EMAIL, target=recipient["email"],
                    percentage=recipient["percentage"], subject_line=subject_line,
                    body=body, status=AlertDelivery.Status.SKIPPED,
                    error="Account not activated yet.",
                ))
            else:
                outcome = send_template_mail(
                    subject_line[:250], recipient["email"], "alert",
                    {"body": body, "institute": ctx["institute"],
                     "login_url": f"{settings.SITE_URL}/auth/login/"},
                    messageGroup="LOW_ATTENDANCE_ALERT",
                    utm_source="Low attendance alert",
                    # Wait, so the delivery report records what actually
                    # happened rather than what was merely queued.
                    wait=True,
                ).result()
                if outcome.ok:
                    status, error = AlertDelivery.Status.SENT, ""
                    tally["email_sent"] += 1
                else:
                    log.error("Alert email to %s failed: %s", recipient["email"], outcome.error)
                    status, error = AlertDelivery.Status.FAILED, outcome.error or "Send failed"
                    tally["email_failed"] += 1
                deliveries.append(AlertDelivery(
                    campaign=campaign, student=recipient["profile"],
                    channel=AlertDelivery.Channel.EMAIL, target=recipient["email"],
                    percentage=recipient["percentage"], subject_line=subject_line,
                    body=body, status=status, error=error[:300],
                ))

        if student_wa_on:
            deliver_whatsapp(
                recipient,
                channel=AlertDelivery.Channel.WHATSAPP_STUDENT,
                template=student_template,
                number_key="student_number", error_key="student_error",
                raw_key="student_mobile",
                sent_key="student_whatsapp_sent", failed_key="student_whatsapp_failed",
                missing_note="No mobile number on record for this student.",
            )

        if whatsapp_on:
            deliver_whatsapp(
                recipient,
                channel=AlertDelivery.Channel.WHATSAPP,
                template=guardian_template,
                number_key="guardian_number", error_key="guardian_error",
                raw_key="guardian_mobile",
                sent_key="whatsapp_sent", failed_key="whatsapp_failed",
                missing_note="No guardian number on record.",
            )

    with transaction.atomic():
        AlertDelivery.objects.bulk_create(deliveries, batch_size=500)
        for field, value in tally.items():
            setattr(campaign, field, value)
        campaign.save(update_fields=list(tally))

    ActivityLog.log(
        actor=user, action="ALERTS_SENT",
        detail=(f"{campaign.sent_total} sent to {len(recipients)} student(s) below "
                f"{threshold}% ({'overall' if scope == OVERALL else subject.code})"),
    )
    return campaign


def resolve_subject(user, subject_id):
    """A sender may only alert on subjects inside their own scope."""
    from academics.selectors import subjects_for

    subject_id = clean_object_id(subject_id)
    if subject_id is None:
        return None                 # malformed id reads as "not found", not 500
    return subjects_for(user).filter(pk=subject_id).first()


def scope_guard(user, student_ids):
    """Drop any student id outside the sender's scope."""
    # Compare as strings: the browser sends 24-char hex, the database returns
    # ObjectId, and the two never compare equal. Getting this wrong fails
    # closed (every recipient dropped) rather than loudly, so it is worth
    # being explicit — this is the check that stops a teacher alerting
    # students outside their own scope.
    allowed = {str(pk) for pk in scoped_students(user).values_list("id", flat=True)}
    return [str(i) for i in student_ids if str(i) in allowed]
