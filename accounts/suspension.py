"""
Suspending a teacher, by the university that affiliates their department.

**Why this is not a status.** `User.status` is what the *institute* has done
with an account — active, invited, archived. `User.is_revoked` is what happened
to the *discipline* underneath it. This is what the *university* has decided
about the person. Three parties, three facts, and the mistake recorded at the
top of `core/enums.py` is what happens when facts like these get folded into
one field: a suspension written over `status` would have nothing to restore
when it was lifted, and "the institute archived them" and "the university
suspended them" would become the same row on every screen and in every count.

**Who may act.** Only the university that affiliates the teacher's department,
found through `academics.curriculum.governing_university`. Not any university
that happens to affiliate the institute for some *other* discipline: a college
with engineering under one body and pharmacy under another has two affiliating
universities, and letting either sanction the other's staff would make the
per-discipline design meaningless.

**Who may lift it.** The same body, and nobody else. An institute that could
clear a suspension from its own Teachers page would make the whole thing a
note rather than a sanction — so the institute's edit and activate endpoints
refuse a suspended teacher, and say who to ask.

**What it does.** It stops the person signing in, through the existing
`sign_in_blocked_reason` hook, so every entry point is covered by one check
rather than each view remembering. Nothing is deleted and no assignment is
touched: their classes, their attendance and their history stay exactly where
they are, because a suspension is very often lifted and re-entering a term of
teaching records is not a thing anyone should have to do.

**Who is told.** The teacher, the head of their department, and the head of the
institute — at their *login* addresses, never the institute's letterhead one.
See `accounts/recipients.py` for why that distinction has already cost this
project a silently discarded mailbox. The three are told together, in one call,
because a sanction the teacher knows about and their HoD does not is how a
suspended teacher walks into a class on Monday.
"""
import logging

from django.db import transaction
from django.utils import timezone

from .models import ActivityLog, User

log = logging.getLogger("geoattend")


class SuspensionError(Exception):
    """A refusal with a message meant to be shown to the person."""


def governing_university_of(teacher):
    """
    The university entitled to sanction this teacher, or None.

    Keyed on the department, not the institute, because affiliation is
    per-discipline. A teacher with no department has no affiliating body and so
    cannot be suspended by anyone — which is correct rather than an oversight:
    there is no university whose rules they are under.
    """
    from academics.curriculum import governing_university

    if teacher is None or teacher.department_id is None:
        return None
    return governing_university(teacher.department)


def may_suspend(user, teacher):
    """Is this account the body that affiliates this teacher's department?"""
    if not getattr(user, "is_university", False) or user.university_id is None:
        return False
    if teacher is None or teacher.role != User.Role.TEACHER:
        return False
    governing = governing_university_of(teacher)
    return governing is not None and governing.pk == user.university_id


def may_manage(user, teacher):
    """
    May this account edit or deactivate this teacher?

    A suspended teacher is frozen to the institute and to their head of
    department. Not because editing a name would undo the sanction, but because
    **archiving would release their PAN** — and a college that could archive a
    suspended teacher could hand them straight to the next college, with the
    bar still standing and nobody the wiser. See accounts/pan.py: the PAN rule
    keys on "not archived", so the archive button is the escape hatch.

    Editing is refused for the plainer reason that the row is evidence while a
    sanction is live, and a name or department changed underneath it makes the
    record harder to read later.

    The university that imposed it can still do both, which is what makes this
    a freeze rather than a deletion.
    """
    if teacher is None or not getattr(teacher, "is_suspended", False):
        return True
    return bool(getattr(user, "is_university", False))


def manage_refusal(teacher):
    """The message to refuse an institute's edit or deactivation with."""
    body = teacher.suspended_by
    who = (body.short_name or body.name) if body else "their affiliating university"
    return (f"{teacher.get_full_name()} is suspended by {who}, so this record "
            f"is frozen — it cannot be edited, deactivated or reactivated here "
            f"while the suspension stands. {who} has to lift it first.")


def blocked_reason(user):
    """
    Why a suspended account cannot sign in, or None.

    The reason is quoted back verbatim. A sanction the person cannot read is
    one they cannot answer, and "contact your institute" would send them to
    somebody who cannot lift it.
    """
    if not getattr(user, "is_suspended", False):
        return None
    body = user.suspended_by
    who = (body.short_name or body.name) if body else "your affiliating university"
    reason = (user.suspension_reason or "").strip()
    return (f"Your account has been suspended by {who}."
            + (f" Reason: {reason}" if reason else "")
            + f" {who} can lift this; your institute cannot.")


def recipients_for(teacher):
    """
    Everybody who has to know: the teacher, their HoD, and the institute head.

    Login addresses throughout — see accounts/recipients.py. Deduplicated and
    order-stable, because one person can hold two of these roles in a small
    college and being emailed the same sanction twice reads like two of them.
    """
    from .recipients import institute_recipients

    addresses = []
    if teacher.email:
        addresses.append(teacher.email)

    department = teacher.department
    hod = getattr(department, "hod", None) if department else None
    if hod is not None and hod.is_active and hod.email:
        addresses.append(hod.email)

    if teacher.institute_id:
        # `fallback=False`: the institute's letterhead address is a reasonable
        # last resort for "your registration was approved", but a named
        # person's sanction should not be posted to a shared inbox nobody
        # signs in to.
        addresses.extend(institute_recipients(teacher.institute, fallback=False))

    seen, ordered = set(), []
    for address in addresses:
        key = address.lower()
        if key not in seen:
            seen.add(key)
            ordered.append(address)
    return ordered


@transaction.atomic
def suspend(*, teacher, reason, actor):
    """
    Bar a teacher, and tell the three people who need to know.

    `reason` is required. A suspension without one cannot be answered, appealed
    or lifted with any confidence, and the person reading the email is entitled
    to know what it is about.
    """
    if not may_suspend(actor, teacher):
        raise SuspensionError(
            "This teacher's department is not affiliated to you, so they are "
            "not yours to suspend.")
    reason = (reason or "").strip()
    if len(reason) < 10:
        raise SuspensionError(
            "Give a reason of at least a few words. It is quoted to the "
            "teacher, their head of department and the institute, and it is "
            "what they have to answer.")
    if teacher.is_suspended:
        raise SuspensionError(f"{teacher.get_full_name()} is already suspended.")

    teacher.is_suspended = True
    teacher.suspension_reason = reason
    teacher.suspended_at = timezone.now()
    teacher.suspended_by = actor.university
    # **`status` and `is_active` are deliberately untouched.** See the module
    # docstring: this is a fourth fact about the row, not a new value for an
    # existing one.
    teacher.save(update_fields=["is_suspended", "suspension_reason",
                                "suspended_at", "suspended_by"])

    sent = _notify(teacher, reason, actor, lifted=False)
    ActivityLog.log(actor=actor, action="TEACHER_SUSPENDED",
                    detail=f"{teacher.email}: {reason[:120]}")
    return {"suspended": True, "notified": sent}


@transaction.atomic
def lift(*, teacher, reason, actor):
    """
    Clear a suspension. Only the body that imposed it may do so.

    Checked against `suspended_by` rather than against the current affiliation,
    because the two can differ: if the discipline has since been delinked, the
    teacher is left barred by a university that no longer affiliates them, and
    only that university can put it right.
    """
    if not teacher.is_suspended:
        raise SuspensionError(f"{teacher.get_full_name()} is not suspended.")
    if (not getattr(actor, "is_university", False)
            or actor.university_id is None
            or teacher.suspended_by_id != actor.university_id):
        body = teacher.suspended_by
        who = (body.short_name or body.name) if body else "another university"
        raise SuspensionError(
            f"This suspension was imposed by {who}, so only they can lift it.")

    teacher.is_suspended = False
    teacher.suspension_reason = ""
    teacher.suspended_at = None
    teacher.suspended_by = None
    teacher.save(update_fields=["is_suspended", "suspension_reason",
                                "suspended_at", "suspended_by"])

    sent = _notify(teacher, (reason or "").strip(), actor, lifted=True)
    ActivityLog.log(actor=actor, action="TEACHER_SUSPENSION_LIFTED",
                    detail=teacher.email)
    return {"suspended": False, "notified": sent}


def _notify(teacher, reason, actor, *, lifted):
    """
    One message to all three. Never allowed to undo the decision.

    Wrapped because a mail provider being down is not a reason for a sanction
    to fail to save — the decision is the record, the email is the courtesy,
    and losing the second must not lose the first. What went wrong is logged
    and the count returned is what actually went out.
    """
    from .emails import send_teacher_suspension

    addresses = recipients_for(teacher)
    if not addresses:
        log.warning("no deliverable address for teacher %s — suspension not "
                    "announced", teacher.email)
        return []
    try:
        send_teacher_suspension(teacher, reason, actor, addresses, lifted=lifted)
    except Exception:                                    # noqa: BLE001
        log.exception("could not announce suspension of %s", teacher.email)
        return []
    return addresses
