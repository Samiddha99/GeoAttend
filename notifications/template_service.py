"""
Registering an institute's WhatsApp wording with Twilio, and tracking approval.

The head writes plain wording with ``{{placeholder}}`` names. Submitting does
three things in order:

    1. save the row locally (so nothing is lost if Twilio is down)
    2. POST the numbered version to Twilio's Content API      → content_sid
    3. POST an approval request to WhatsApp                   → status

Every step records its own error rather than raising, so a half-finished
submission is visible in the UI and can be retried.
"""
import datetime as dt
import logging
import re

from django.conf import settings
from django.utils import timezone
from django.utils.text import slugify

from accounts.models import ActivityLog

from . import whatsapp as wa
from . import message_templates as mt

log = logging.getLogger("geoattend")
from .models import WhatsAppTemplate


def make_twilio_name(institute, name):
    """WhatsApp allows lowercase letters, digits and underscores only."""
    base = re.sub(r"[^a-z0-9_]+", "_", slugify(name).replace("-", "_")).strip("_")
    base = (base or "attendance_alert")[:100]
    candidate, n = base, 2
    while WhatsAppTemplate.objects.filter(
        institute=institute, twilio_name=candidate
    ).exists():
        candidate = f"{base}_{n}"[:120]
        n += 1
    return candidate


def validate_body(body):
    """Return a human error, or None. Guards what WhatsApp itself will reject."""
    body = (body or "").strip()
    if not body:
        return "The message body is empty."
    if len(body) > 1024:
        return f"WhatsApp templates are limited to 1024 characters (this is {len(body)})."
    unknown = mt.unknown_placeholders(body)
    if unknown:
        return ("Unrecognised placeholder(s): "
                + ", ".join("{{%s}}" % u for u in unknown)
                + ". Use only the placeholders listed below.")
    numbered, order = wa.to_numbered(body)
    if not numbered.replace("{{", "").replace("}}", "").strip(" 0123456789"):
        return ("A template cannot be only placeholders — WhatsApp rejects those. "
                "Add some fixed wording around them.")
    if len(order) > 20:
        return "WhatsApp allows at most 20 variables in a template."
    return None


def create_template(*, institute, user, audience, name, body, category="UTILITY",
                    language="en", submit=True):
    """
    Persist the wording and (optionally) push it to Twilio for approval.

    Returns the :class:`WhatsAppTemplate`; check ``.status`` and ``.last_error``.
    """
    template = WhatsAppTemplate.objects.create(
        institute=institute,
        audience=audience,
        name=name.strip()[:120],
        twilio_name=make_twilio_name(institute, name),
        language=language,
        category=category,
        body=body.strip(),
        variable_order=wa.to_numbered(body)[1],
        created_by=user,
        status=WhatsAppTemplate.Status.DRAFT,
    )
    if submit:
        submit_template(template, user=user)
    return template


def submit_template(template, user=None):
    """Push (or re-push) a draft to Twilio and request WhatsApp approval."""
    if not template.is_editable and template.content_sid:
        return template

    content_sid, order, error = wa.create_content(
        template.twilio_name, template.body, template.language)
    if error:
        template.status = WhatsAppTemplate.Status.FAILED
        template.last_error = error[:400]
        template.save(update_fields=["status", "last_error"])
        return template

    template.content_sid = content_sid
    template.variable_order = order
    template.submitted_at = timezone.now()

    status, reason, error = wa.submit_for_approval(
        content_sid, template.twilio_name, template.category)
    if error:
        # The content exists at Twilio but WhatsApp never saw it — retryable.
        template.status = WhatsAppTemplate.Status.FAILED
        template.last_error = error[:400]
    else:
        template.status = status or WhatsAppTemplate.Status.RECEIVED
        template.rejection_reason = reason[:400]
        template.last_error = ""
        template.last_synced_at = timezone.now()
    template.save()

    ActivityLog.log(
        actor=user or template.created_by, action="WA_TEMPLATE_SUBMITTED",
        detail=f"{template.name} ({template.audience}) → {template.status}")
    return template


def sync_template(template, timeout=None):
    """Refresh one template's approval state from Twilio."""
    if not template.content_sid:
        return template
    status, reason, error = wa.fetch_approval(template.content_sid, timeout=timeout)
    if error:
        template.last_error = error[:400]
        template.save(update_fields=["last_error"])
        return template
    if status:
        template.status = status
    template.rejection_reason = (reason or "")[:400]
    template.last_error = ""
    template.last_synced_at = timezone.now()
    template.save(update_fields=["status", "rejection_reason", "last_error",
                                 "last_synced_at"])
    return template


def pending_templates(institute):
    """Templates Twilio has not yet decided — the only ones worth polling."""
    return WhatsAppTemplate.objects.filter(
        institute=institute,
        status__in=[WhatsAppTemplate.Status.RECEIVED, WhatsAppTemplate.Status.PENDING],
    ).exclude(content_sid="")


def sync_all(institute, *, timeout=None):
    """Refresh every template still awaiting a verdict."""
    return [sync_template(t, timeout=timeout) for t in pending_templates(institute)]


def autosync(institute):
    """
    Opportunistic refresh, run when a page that shows template status opens.

    Three guards, because this sits in a request the user is waiting on:
      * templates already approved or rejected are never polled, so an
        institute in its steady state makes no network calls at all;
      * a template synced within the throttle window is skipped, so reloading
        or moving between the two screens does not re-poll;
      * a short timeout, so a slow Twilio delays the page by seconds rather
        than the full TIMEOUT.

    Failures are swallowed on purpose: a page must still render when Twilio is
    unreachable. `sync_template` records the error on the row either way.
    """
    conf = settings.WHATSAPP
    if not conf.get("AUTOSYNC", True) or not wa.is_configured():
        return []

    cutoff = timezone.now() - dt.timedelta(
        seconds=int(conf.get("AUTOSYNC_THROTTLE_SEC", 120) or 0))
    due = [t for t in pending_templates(institute)
           if t.last_synced_at is None or t.last_synced_at <= cutoff]
    if not due:
        return []

    timeout = conf.get("AUTOSYNC_TIMEOUT", 6)
    synced = []
    for template in due:
        try:
            synced.append(sync_template(template, timeout=timeout))
        except Exception:                       # pragma: no cover - defensive
            log.exception("Auto-sync failed for template %s", template.pk)
    return synced


def templates_for(user, audience=None, approved_only=False):
    """Templates belonging to this user's institute."""
    qs = WhatsAppTemplate.objects.filter(institute=user.institute, is_active=True)
    if audience:
        qs = qs.filter(audience=audience)
    if approved_only:
        qs = qs.filter(status=WhatsAppTemplate.Status.APPROVED).exclude(content_sid="")
    return qs


def serialise(template):
    return {
        "id": template.id,
        "name": template.name,
        "twilio_name": template.twilio_name,
        "audience": template.audience,
        "audience_label": template.get_audience_display(),
        "body": template.body,
        "variables": template.variable_order,
        "status": template.status,
        "status_label": template.get_status_display(),
        "is_sendable": template.is_sendable,
        "is_editable": template.is_editable,
        "content_sid": template.content_sid,
        "category": template.category,
        "language": template.language,
        "rejection_reason": template.rejection_reason,
        "last_error": template.last_error,
        "created_by": template.created_by.get_full_name() if template.created_by else "",
        "created_at": template.created_at.strftime("%d %b %Y, %H:%M"),
        "last_synced_at": (template.last_synced_at.strftime("%d %b %Y, %H:%M")
                           if template.last_synced_at else ""),
    }
