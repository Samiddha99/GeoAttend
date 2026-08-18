"""Business rules for identity: creating institutes and activating invitees."""
import datetime as dt

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from .emails import send_invitation, send_welcome
from .institute_approval import request_institute_approval
from .models import (
    ActivityLog,
    Institute,
    InstituteAffiliation,
    Invitation,
    University,
    UniversityDiscipline,
)

User = get_user_model()


@transaction.atomic
def create_institute_and_head(payload):
    """
    Called only after the signup OTP has been verified.

    The institute's status is decided here rather than defaulted: an institute
    that named an affiliating body needs that body's approval, and one that is
    autonomous in every discipline has nobody left to ask. Getting this wrong
    in either direction is serious — too strict locks out a legitimate head,
    too lax lets anyone claim to be affiliated to a university that never
    agreed.
    """
    affiliations = payload.get("affiliations") or {}
    needs_approval = any(university_id for university_id in affiliations.values())

    institute = Institute.objects.create(
        name=payload["institute_name"],
        code=payload["institute_code"],
        email=payload["institute_email"],
        phone=payload.get("phone", ""),
        website=payload.get("website", ""),
        address=payload.get("address", ""),
        state=payload.get("state", ""),
        district=payload.get("district", ""),
        status=(Institute.Status.PENDING if needs_approval
                else Institute.Status.APPROVED),
    )
    for discipline, university_id in affiliations.items():
        InstituteAffiliation.objects.create(
            institute=institute, discipline=discipline,
            university_id=university_id)
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
    if needs_approval:
        # No welcome yet — the head cannot sign in until a university says so,
        # and a "welcome, you're all set" email would be a lie.
        request_institute_approval(institute)
    else:
        send_welcome(head)
    return institute, head


@transaction.atomic
def create_university_and_admin(payload):
    """
    Register a university, or claim one of the seeded rows.

    Claiming reuses the existing row rather than creating a second: the seeded
    list exists so that "Anna University" is one account with all its
    institutes, not two accounts that cannot see each other's.
    """
    existing = payload.get("existing_university")
    fields = dict(
        name=payload["university_name"],
        short_name=payload.get("short_name", ""),
        code=payload["university_code"],
        email=payload["university_email"],
        phone=payload.get("phone", ""),
        website=payload.get("website", ""),
        address=payload.get("address", ""),
        state=payload.get("state", ""),
        district=payload.get("district", ""),
        grants_affiliation=bool(payload.get("grants_affiliation")),
        claimed_at=timezone.now(),
    )
    if existing is not None:
        for key, value in fields.items():
            setattr(existing, key, value)
        existing.save()
        university = existing
    else:
        university = University.objects.create(**fields)

    # Replace rather than merge: the disciplines chosen at signup are what this
    # university says it covers now, and a seeded row may list one it dropped.
    UniversityDiscipline.objects.filter(university=university).delete()
    UniversityDiscipline.objects.bulk_create([
        UniversityDiscipline(university=university, discipline=d)
        for d in payload["disciplines"]])

    admin = User.objects.create_user(
        email=payload["admin_email"],
        password=payload["password"],
        full_name=payload["admin_name"],
        phone=payload.get("admin_phone", ""),
        role=User.Role.UNIVERSITY,
        university=university,
        email_verified=True,
        registration_completed=True,
    )
    ActivityLog.log(actor=admin, action="UNIVERSITY_CREATED", detail=university.name)
    send_welcome(admin)
    return university, admin


@transaction.atomic
def invite_institute(*, university, name, code, email, head_email,
                     state, district, affiliations, invited_by=None):
    """
    A university creates an institute and invites its head.

    The university fixes what it is entitled to fix — the institute's identity
    and where it is — and the head fills in the rest when they accept. That
    split is the whole point: an institute the university placed in a district
    should not be able to move itself somewhere else, but its own address,
    phone and website are its own business.

    Approved on creation, whatever the affiliation. A university inviting an
    institute *is* the approval; asking it to then approve its own invitation
    would be a queue with one item that it put there itself.

    Works whether or not the university grants affiliation — a university may
    invite an institute it does not affiliate, and that institute simply has no
    affiliation rows pointing at it.
    """
    institute = Institute.objects.create(
        name=name, code=code, email=email,
        state=state, district=district,
        status=Institute.Status.APPROVED,
        invited_by=university,
    )
    for discipline, university_id in (affiliations or {}).items():
        InstituteAffiliation.objects.create(
            institute=institute, discipline=discipline,
            university_id=university_id)

    user, invitation, _created = invite_user(
        email=head_email,
        role=User.Role.HEAD,
        institute=institute,
        invited_by=invited_by,
        extra_lines=[
            f"{university.short_name or university.name} has registered "
            f"{institute.name} on {settings.SITE_NAME} and invited you to run it.",
        ],
    )
    ActivityLog.log(actor=invited_by, action="INSTITUTE_INVITED",
                    detail=f"{institute.name} by {university.name}")
    return institute, user, invitation


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
