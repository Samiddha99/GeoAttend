"""Business rules for identity: creating institutes and activating invitees."""
import datetime as dt

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from .emails import send_invitation, send_welcome
from .models import ActivityLog, Institute, Invitation

User = get_user_model()


@transaction.atomic
def create_institute_and_head(payload):
    """Called only after the signup OTP has been verified."""
    institute = Institute.objects.create(
        name=payload["institute_name"],
        code=payload["institute_code"],
        email=payload["institute_email"],
        phone=payload.get("phone", ""),
        website=payload.get("website", ""),
        address=payload.get("address", ""),
    )
    head = User.objects.create_user(
        email=payload["head_email"],
        password=payload["password"],
        full_name=payload["head_name"],
        phone=payload.get("head_phone", ""),
        role=User.Role.HEAD,
        institute=institute,
        email_verified=True,
        registration_completed=True,
        is_staff=True,
    )
    ActivityLog.log(actor=head, action="INSTITUTE_CREATED", detail=institute.name)
    send_welcome(head)
    return institute, head


@transaction.atomic
def invite_user(*, email, role, institute, department=None, full_name="", invited_by=None,
                payload=None, extra_lines=None, send=True):
    """
    Create (or reuse) a not-yet-activated user and mail them a one-time link.
    Returns (user, invitation, created).
    """
    from core.utils import normalise_email

    email = normalise_email(email)
    user = User.objects.filter(email=email).first()
    created = False
    if user is None:
        user = User.objects.create_user(
            email=email,
            password=None,
            full_name=full_name,
            role=role,
            institute=institute,
            department=department,
            registration_completed=False,
            is_active=True,
        )
        created = True
    else:
        if user.registration_completed:
            # Already an active account — don't silently overwrite it.
            return user, None, False
        user.role = role
        user.institute = institute
        user.department = department or user.department
        if full_name:
            user.full_name = full_name
        user.save(update_fields=["role", "institute", "department", "full_name"])

    invitation = Invitation.objects.filter(
        email=email, status=Invitation.Status.PENDING
    ).first()
    if invitation:
        invitation.role = role
        invitation.institute = institute
        invitation.department = department
        invitation.full_name = full_name or invitation.full_name
        invitation.payload = payload or invitation.payload
        invitation.invited_by = invited_by
        invitation.expires_at = timezone.now() + dt.timedelta(days=settings.INVITE_TTL_DAYS)
        invitation.save()
    else:
        invitation = Invitation.objects.create(
            email=email,
            full_name=full_name,
            role=role,
            institute=institute,
            department=department,
            payload=payload or {},
            invited_by=invited_by,
            expires_at=timezone.now() + dt.timedelta(days=settings.INVITE_TTL_DAYS),
        )
    if send:
        send_invitation(invitation, extra_lines=extra_lines)
    return user, invitation, created


@transaction.atomic
def activate_invitee(invitation, *, full_name, phone, raw_password):
    """Finish an invited account: set name/phone/password and flip the flag."""
    user = User.objects.select_for_update().filter(email=invitation.email).first()
    if user is None:
        user = User.objects.create_user(
            email=invitation.email,
            password=None,
            role=invitation.role,
            institute=invitation.institute,
            department=invitation.department,
        )
    user.full_name = full_name or user.full_name
    if phone:
        user.phone = phone
    user.role = invitation.role
    user.institute = invitation.institute
    if invitation.department_id:
        user.department = invitation.department
    user.set_password(raw_password)
    user.email_verified = True
    user.registration_completed = True
    user.is_active = True
    user.save()

    invitation.accept(user)
    ActivityLog.log(actor=user, action="REGISTRATION_COMPLETED", detail=user.role)
    send_welcome(user)
    return user


def unlink_device(user, *, actor=None, request=None, reason=""):
    """
    Release a student's device binding so they can mark attendance from a new phone.

    The binding is what stops one handset marking for several students, so this
    is a staff action: it is recorded in the activity log and the student is
    emailed, which makes quiet abuse of the reset visible after the fact.

    Returns True if something was actually unlinked.
    """
    from notifications.mailer import send_template_mail

    if not user.device_id:
        return False

    user.device_id = ""
    user.device_bound_at = None
    user.save(update_fields=["device_id", "device_bound_at"])

    ActivityLog.log(
        request, actor=actor or user, action="DEVICE_UNLINKED",
        detail=f"{user.email} unlinked by {(actor or user).email}"
               + (f" — {reason}" if reason else ""),
    )
    if user.registration_completed:
        send_template_mail(
            "Your device has been unlinked",
            user.email,
            "device_unlinked",
            {
                "user": user,
                "actor": actor,
                "reason": reason,
                "self_service": actor is None or actor.pk == user.pk,
                "login_url": f"{settings.SITE_URL}/auth/login/",
            },
            messageGroup="DEVICE_UNLINKED",
            utm_source="Device unlinked",
        )
    return True
