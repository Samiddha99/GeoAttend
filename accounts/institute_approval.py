"""
The approval handshake between an institute and the university it named.

An institute that registers itself and claims affiliation is making a claim
about somebody else — that a university accepts it. Nobody but that university
can confirm it, so the institute waits in PENDING and its head cannot sign in.

Kept in its own module rather than in services.py because it is a small state
machine with three transitions and two emails, and it reads better whole.
"""
import logging

from django.utils import timezone

log = logging.getLogger("geoattend")


def approvers_for(institute):
    """
    The universities that must decide on this institute.

    One institute can name two bodies — engineering to AKTU, pharmacy to a
    health-sciences university. Both are asked. The *first* to approve lets the
    institute in: waiting for unanimity would leave an institute blocked
    because one of its two universities never logs in, and the university that
    did not answer keeps its own approval queue either way.
    """
    from .models import University

    return University.objects.filter(
        affiliated_institutes__institute=institute, is_active=True).distinct()


def request_institute_approval(institute):
    """
    Tell every university this institute named that it is waiting.

    Addressed to the university's *logins*, not `University.email`. Every
    seeded university's official address is `@unclaimed.invalid` — a reserved
    domain that cannot receive mail — so addressing it there meant the pending
    tab filled up and nobody was ever told to look at it.
    """
    from notifications.mailer import send_template_mail

    from .recipients import university_recipients

    for university in approvers_for(institute):
        to = university_recipients(university)
        if not to:
            continue
        try:
            send_template_mail(
                subject=f"{institute.name} is waiting for your approval",
                to=to,
                template="institute_pending",
                context={"institute": institute, "university": university},
            )
        except Exception:
            # A mail provider being down must not roll back the signup — the
            # institute still appears in the university's pending tab, which is
            # where the decision actually gets made.
            log.exception("could not email %s about %s", to, institute.name)


def _head_of(institute):
    from .models import User

    return institute.users.filter(role=User.Role.HEAD).order_by("date_joined").first()


def approve_institute(*, institute, actor):
    """
    Let the institute in. Idempotent — approving twice is not an error.

    The rejection reason is deliberately *not* cleared: an institute that was
    turned down once and later accepted has a history worth keeping, and the
    screens show it against the decision date rather than as a current state.
    """
    from .models import ActivityLog, Institute
    from notifications.mailer import send_template_mail

    from .recipients import institute_recipients

    if institute.status == Institute.Status.APPROVED:
        return institute

    institute.status = Institute.Status.APPROVED
    institute.decided_at = timezone.now()
    institute.decided_by = actor
    institute.save(update_fields=["status", "decided_at", "decided_by"])

    head = _head_of(institute)
    to = institute_recipients(institute)
    if to:
        try:
            send_template_mail(
                subject=f"{institute.name} has been approved",
                to=to,
                template="institute_approved",
                context={"institute": institute, "head": head},
            )
        except Exception:
            log.exception("could not email the head of %s", institute.name)
    ActivityLog.log(actor=actor, action="INSTITUTE_APPROVED", detail=institute.name)
    return institute


def reject_institute(*, institute, actor, reason):
    """
    Turn the institute down, with a reason the institute is shown verbatim.

    The reason is required. "Rejected" with no explanation leaves a head with
    nothing to correct and nothing to appeal, and the support conversation that
    follows costs the university more than writing a sentence would have.
    """
    from .models import ActivityLog, Institute
    from notifications.mailer import send_template_mail

    from .recipients import institute_recipients

    reason = (reason or "").strip()
    if not reason:
        raise ValueError("A rejection needs a reason.")

    institute.status = Institute.Status.REJECTED
    institute.rejection_reason = reason
    institute.decided_at = timezone.now()
    institute.decided_by = actor
    institute.save(update_fields=["status", "rejection_reason",
                                  "decided_at", "decided_by"])

    head = _head_of(institute)
    to = institute_recipients(institute)
    if to:
        try:
            send_template_mail(
                subject=f"About your registration for {institute.name}",
                to=to,
                template="institute_rejected",
                context={"institute": institute, "head": head, "reason": reason},
            )
        except Exception:
            log.exception("could not email the head of %s", institute.name)
    ActivityLog.log(actor=actor, action="INSTITUTE_REJECTED",
                    detail=f"{institute.name}: {reason[:80]}")
    return institute


def sign_in_blocked_reason(user):
    """
    Why this account cannot sign in yet, or None.

    Only the institute's own people are held back. A university reaches its
    institutes precisely so it can decide on them, so it is never blocked by
    an institute's status.
    """
    from .models import Institute
    from .suspension import blocked_reason as suspension_reason

    # Checked first and outside the institute test, because a suspension is
    # about the person rather than their college — and because it is the one
    # refusal here that names somebody who can actually undo it.
    suspended = suspension_reason(user)
    if suspended:
        return suspended

    institute = getattr(user, "institute", None)
    if institute is None or getattr(user, "is_university", False):
        return None
    if institute.status == Institute.Status.PENDING:
        names = ", ".join(u.short_name or u.name for u in approvers_for(institute))
        return ("This institute is still waiting for approval"
                + (f" from {names}" if names else "")
                + ". You will get an email as soon as it is decided.")
    if institute.status == Institute.Status.REJECTED:
        reason = institute.rejection_reason or "No reason was recorded."
        return f"This institute's registration was not approved. {reason}"
    return None
