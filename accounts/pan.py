"""
One teacher, one college at a time — keyed on PAN.

**Why PAN and not email.** Email identifies an *account*; PAN identifies a
*person*. A teacher who moves colleges arrives with a new work address, and two
institutes would each hold a separate account for one human being with nothing
to connect them. Every rule below is therefore about the PAN, and the login is
irrelevant to all of them.

**The rule.** At most one teacher per PAN may be anything other than archived.
"Archived" is the whole test:

    ACTIVE    — running, holds the PAN
    INVITED   — asked but not yet registered, still holds it: the seat is taken
    ARCHIVED  — released, and another institute may add them
    revoked   — a flag, not a status. A revoked teacher whose row is still
                active *holds* the PAN; the requirement says the first college
                must archive them before the second can add them, and this is
                exactly that sentence.

Suspension does not release a PAN either. A suspended teacher is still on the
first college's books — that is what makes it a sanction rather than a
resignation.

**What this cannot do on its own.** There is no database constraint behind it.
The condition is "unique among rows whose status is not ARCHIVED", which is a
partial index over a column that changes on every archive and restore, and
`django_mongodb_backend` will not express one. Two administrators adding the
same PAN in the same second can both pass the check. The window is small and
the consequence is visible and reversible — two rows, one of which is archived
by hand — but it is real, and pretending otherwise in a docstring would be
worse than saying so here.

**Verification** goes through `core.utils.PAN_verification`, which is the
project's own call to the KYC provider. It is treated as blocking: a PAN that
cannot be confirmed is not recorded. It is also wrapped, because a provider
outage should read as "we could not check this right now" rather than as a
500 on a page somebody is trying to use.
"""
import logging
import re

from django.utils import timezone

from core.enums import RowStatus

from .models import User

log = logging.getLogger("geoattend")

#: Five letters, four digits, one letter — `ABCDE1234F`. Checked here so a
#: typo costs nothing rather than a round trip to the provider.
PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")


class PanError(Exception):
    """A refusal with a message meant to be shown to the person."""

    def __init__(self, message, field="pan_number"):
        super().__init__(message)
        self.field = field


def normalise(pan):
    """Upper-cased and stripped, so one PAN cannot be stored two ways."""
    return re.sub(r"\s+", "", (pan or "")).upper()


def check_format(pan):
    """Raise unless this looks like a PAN at all."""
    pan = normalise(pan)
    if not pan:
        raise PanError("Enter the teacher's PAN.")
    if not PAN_RE.match(pan):
        raise PanError("A PAN is five letters, four digits and a letter — "
                       "for example ABCDE1234F.")
    return pan


def holder(pan, exclude_pk=None):
    """
    The teacher currently holding this PAN, or None.

    "Holding" means any row that is not archived. Ordered oldest first so that
    if the rule has already been breached — by an import, or by the race the
    module docstring admits to — the message names the original rather than
    whichever row happened to come back first.
    """
    pan = normalise(pan)
    if not pan:
        return None
    query = User.objects.filter(
        role=User.Role.TEACHER, pan_number=pan
    ).exclude(status=RowStatus.ARCHIVED).select_related("institute")
    if exclude_pk is not None:
        query = query.exclude(pk=exclude_pk)
    return query.order_by("date_joined").first()


def _where(teacher):
    """Which college is holding them, phrased for somebody who cannot see it."""
    institute = teacher.institute
    if institute is None:
        return "another account"
    return f"{institute.name} ({institute.code})"


def assert_free(pan, *, exclude_pk=None):
    """
    Raise unless this PAN is available for a new or reactivated teacher.

    The message names the college holding it and says what has to happen,
    because the person reading it cannot see that institute's screens and
    otherwise has no way to find out why they are stuck.
    """
    held_by = holder(pan, exclude_pk=exclude_pk)
    if held_by is None:
        return
    state = "invited to" if held_by.status == RowStatus.INVITED else "active at"
    if held_by.is_revoked:
        state = "on the books at"
    raise PanError(
        f"{held_by.get_full_name()} already holds this PAN and is {state} "
        f"{_where(held_by)}. A teacher can run at one institute at a time, so "
        f"{_where(held_by)} has to archive them before you can add them here.")


def verify(pan, name, date_of_birth):
    """
    Confirm the PAN belongs to this person, through the project's KYC call.

    Blocking, as specified. Wrapped because the call reaches an external
    provider: an outage should say so plainly, not surface as a traceback on
    the invite form. A refusal and an outage read differently on purpose —
    one is the teacher's problem, the other is nobody's.
    """
    from core.utils import PAN_verification

    if not name:
        raise PanError(
            "Enter the teacher's full name as printed on their PAN — the check "
            "matches the name and date of birth against the number.",
            field="full_name")
    if date_of_birth is None:
        raise PanError("Enter the teacher's date of birth.",
                       field="date_of_birth")

    try:
        result = PAN_verification(pan, name, date_of_birth.strftime("%Y-%m-%d"))
    except Exception as exc:                                    # noqa: BLE001
        log.warning("PAN verification unavailable for %s: %s", pan, exc)
        raise PanError(
            "The PAN verification service could not be reached, so this "
            "teacher has not been saved. Nothing was changed — please try "
            "again shortly.") from exc

    if not (result or {}).get("verified"):
        raise PanError(
            "That PAN could not be verified against the name and date of "
            "birth given. Check all three against the card itself.")
    return True


def assert_can_hold(*, pan, name, date_of_birth, exclude_pk=None):
    """
    The whole gate, in the order that costs the least.

    Format first — a typo should not spend a request on the provider. Then
    availability, because a PAN already held is refused whoever it belongs to
    and there is no point verifying it. Verification last.
    """
    pan = check_format(pan)
    assert_free(pan, exclude_pk=exclude_pk)
    verify(pan, name, date_of_birth)
    return pan


def is_echo_of(value, stored):
    """
    Is this the *masked* form of the stored PAN, handed straight back to us?

    The edit form shows `ABCDE****F` — it never receives the whole number, by
    design — and a readonly input still posts its value. So a person who edits
    a teacher's phone number and saves sends the mask back, and a naive
    comparison reads that as "the PAN changed" and refuses a save that changed
    nothing else. That is a bug this had, and it is not the person's to work
    around.

    Kept narrow on purpose: only the exact mask of *this* stored value counts.
    Anything else containing a `*` is still refused by the format check, so
    this cannot be used to slip a value past.
    """
    value = normalise(value)
    return bool(stored) and value == masked(stored)


def assert_immutable(teacher, *, pan, date_of_birth):
    """
    Refuse a change to either field once they are on file.

    They are the answer to "who is this person", and a screen that could edit
    them could quietly move a teacher's history onto somebody else. Clearing
    them is refused for the same reason. Setting them on a row that has none —
    a teacher who predates this — is allowed, and is the only way those rows
    ever acquire one.

    A posted value that is only the masked form of the stored one is *not* a
    change; see `is_echo_of`.
    """
    if teacher.pan_number:
        posted = normalise(pan)
        if (posted and posted != teacher.pan_number
                and not is_echo_of(posted, teacher.pan_number)):
            raise PanError(
                "A PAN cannot be changed once it is on file. If it is wrong, "
                "archive this teacher and add them again with the right one.")
    if teacher.date_of_birth is not None:
        if date_of_birth is not None and date_of_birth != teacher.date_of_birth:
            raise PanError("A date of birth cannot be changed once it is on "
                           "file.", field="date_of_birth")


def assert_can_reactivate(teacher):
    """
    Raise unless this archived teacher may be switched back on.

    The same rule as adding, asked at the other end: while they were archived
    another college may have taken them on, and restoring them here would put
    one person on two payrolls. A teacher with no PAN on file — one that
    predates this — is not blocked, because there is nothing to compare.
    """
    if not teacher.pan_number:
        return
    held_by = holder(teacher.pan_number, exclude_pk=teacher.pk)
    if held_by is None:
        return
    raise PanError(
        f"{teacher.get_full_name()} has since been taken on by "
        f"{_where(held_by)}, so they cannot be reactivated here. "
        f"{_where(held_by)} would have to archive them first.")


def record(teacher, *, pan, date_of_birth, actor=None):
    """Write both fields and log it. Assumes the gate above has already run."""
    from .models import ActivityLog

    teacher.pan_number = normalise(pan)
    teacher.date_of_birth = date_of_birth
    teacher.save(update_fields=["pan_number", "date_of_birth"])
    ActivityLog.log(actor=actor, action="TEACHER_PAN_RECORDED",
                    detail=f"{teacher.email}: {teacher.pan_number}")
    return teacher


def masked(pan):
    """`ABCDE1234F` as `ABCDE****F` — enough to recognise, not to copy."""
    pan = normalise(pan)
    if len(pan) != 10:
        return pan
    return pan[:5] + "****" + pan[-1]


def age_on(date_of_birth, when=None):
    """Whole years, for a column that should not print a raw date of birth."""
    if date_of_birth is None:
        return None
    when = (when or timezone.now()).date()
    return when.year - date_of_birth.year - (
        (when.month, when.day) < (date_of_birth.month, date_of_birth.day))
