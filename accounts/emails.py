"""
Wording and templates for account-related email.

Delivery itself is *not* done here — every message is handed to
``notifications.mailer.send_mail``, the single send function for the whole
project. Change transports there and everything below follows.
"""
import logging

from django.conf import settings
from django.utils import timezone

from notifications.mailer import send_template_mail

log = logging.getLogger("geoattend")


def _send(subject, to, template, context, wait=False, **options):
    """Render a templated email and hand it to the one mailer."""
    result = send_template_mail(
        subject, to, template, context,
        messageGroup=options.pop("messageGroup", template.upper()),
        utm_source=options.pop("utm_source", f"{template} email"),
        wait=wait,
        **options,
    )
    if wait:
        outcome = result.result()
        if not outcome.ok:
            log.error("Email to %s failed: %s", to, outcome.error)
        return outcome.ok
    return True          # queued; failures are logged by the mailer thread


def send_otp(email, code, purpose_label="verify your email"):
    return _send(
        f"{settings.SITE_NAME} verification code: {code}",
        email,
        "otp",
        {"code": code, "purpose": purpose_label, "ttl": settings.OTP_TTL_MINUTES},
    )


def send_invitation(invitation, extra_lines=None):
    labels = {
        "HOD": "Head of Department",
        "TEACHER": "Teacher",
        "STUDENT": "Student",
    }
    ok = _send(
        f"You have been invited to {invitation.institute.name} on {settings.SITE_NAME}",
        invitation.email,
        "invitation",
        {
            "invitation": invitation,
            "role_label": labels.get(invitation.role, invitation.role),
            "accept_url": invitation.accept_url,
            "extra_lines": extra_lines or [],
            "expires_days": settings.INVITE_TTL_DAYS,
        },
    )
    invitation.sent_count += 1
    invitation.last_sent_at = timezone.now()
    invitation.save(update_fields=["sent_count", "last_sent_at"])
    return ok


def send_welcome(user):
    return _send(
        f"Welcome to {settings.SITE_NAME}",
        user.email,
        "welcome",
        {"user": user, "login_url": f"{settings.SITE_URL}/auth/login/"},
    )


def send_low_attendance_alert(user, rows, threshold):
    return _send(
        f"Attendance alert — you are below {threshold}%",
        user.email,
        "low_attendance",
        {"user": user, "rows": rows, "threshold": threshold},
    )
