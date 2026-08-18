"""
Academic actions that more than one screen performs.

Right now that is assigning a head of department, which happens from the
institute's own Departments page and again when a department is adopted from a
university's catalogue. It was inline in the view; `catalogue.adopt` needed it
too and imported a function that did not exist yet — reachable only when a HoD
email was supplied, so nothing noticed until this file.
"""
from django.db import transaction

from accounts.models import ActivityLog, User
from accounts.services import invite_user


class HodError(Exception):
    """A refusal with a message meant to be shown to the person."""


@transaction.atomic
def assign_hod(department, email, actor=None):
    """
    Put somebody in charge of a department, inviting them if they are new.

    Returns `(user, invited)` — `invited` says whether an email went out, which
    the caller needs because "saved" and "saved, and we have emailed a stranger"
    are different things to report.

    **One HoD per department, and one department per HoD.** The second half is
    the one worth guarding: `Department.hod` is a OneToOne, so silently moving
    somebody would leave their old department headless without saying so. The
    clash is refused and names the department they already lead.
    """
    from .models import Department

    email = (email or "").strip().lower()
    if not email:
        return None, False

    if (department.hod and department.hod.email == email
            and department.hod.registration_completed):
        return department.hod, False

    clash = Department.objects.filter(hod__email=email).exclude(
        pk=department.pk).first()
    if clash:
        raise HodError(f"{email} already leads {clash.name}.")

    user, invitation, _ = invite_user(
        email=email, role=User.Role.HOD, institute=department.institute,
        department=department, invited_by=actor,
        extra_lines=[f"Department: {department.name} ({department.code})"],
    )
    if invitation is None:
        raise HodError(f"{email} already has an active account in this system.")

    department.hod = user
    department.save(update_fields=["hod"])
    ActivityLog.log(actor=actor, action="HOD_ASSIGNED",
                    detail=f"{department.name}: {email}")
    return user, True
