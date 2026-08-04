"""
The database side of live face verification.

Kept out of the consumer so every rule is testable without a WebSocket, and so
the consumer stays what it should be: a pump that moves frames one way and
verdicts the other.
"""
import datetime as dt
import logging
import os

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from accounts.models import ActivityLog, FaceEnrolment

from .models import (
    AttendanceRecord,
    AttendanceSession,
    FaceVerifyTicket,
    ManualMarkRequest,
)
from .services import AttendanceError, persist_mark

log = logging.getLogger("geoattend")


def antispoof_ready():
    """
    Is passive liveness actually available?

    Asked separately from "is it enabled" because the answer decides whether
    face marking runs at all. A configured-but-missing model must not quietly
    degrade into no liveness check: a photograph passes every other test in the
    pipeline, so that failure would be invisible and total.
    """
    conf = settings.FACE
    if not conf.get("ANTISPOOF_REQUIRED", True):
        return True
    path = conf.get("ANTISPOOF_MODEL") or ""
    # The file, not merely the setting. Checking only that a path was
    # configured let a pointer at a file that does not exist sail through this
    # gate and blow up per frame inside the matcher instead — where the student
    # saw "could not read that frame" forever and the log filled with
    # tracebacks. A missing model has to be refused here, once, with a message
    # that tells them what to do.
    return bool(path) and os.path.exists(path)


def issue_ticket(*, session, cleared, request):
    """Record that this student has passed every gate except their face."""
    seconds = int(settings.FACE.get("LIVE_TICKET_SECONDS", 180))
    from core.http import client_ip

    # One live ticket per student per class: re-opening the page should reuse
    # the attempt rather than leaving a trail of half-used authorisations.
    FaceVerifyTicket.objects.filter(
        session=session, student=cleared["profile"], used_at__isnull=True,
    ).update(expires_at=timezone.now())

    return FaceVerifyTicket.objects.create(
        session=session,
        student=cleared["profile"],
        latitude=round(cleared["latitude"], 6),
        longitude=round(cleared["longitude"], 6),
        accuracy_m=cleared["accuracy"] or None,
        distance_m=round(cleared["distance"], 2),
        device_fingerprint=cleared["fingerprint"],
        ip=client_ip(request) or None,
        user_agent=request.META.get("HTTP_USER_AGENT", "")[:400],
        expires_at=timezone.now() + dt.timedelta(seconds=seconds),
    )


def load_attempt(*, user, session_token, ticket_token):
    """
    Turn a ticket into everything the socket needs, or a reason it cannot.

    Returns {"error": message} rather than raising: every one of these is
    something to show the student, and a traceback across a WebSocket boundary
    helps nobody.
    """
    profile = getattr(user, "student_profile", None)
    if profile is None:
        return {"error": "Your student profile is incomplete."}

    ticket = (FaceVerifyTicket.objects
              .select_related("session", "session__subject", "student")
              .filter(token=(ticket_token or "").strip(), student=profile)
              .first())
    if ticket is None:
        return {"error": "This attempt is not authorised. Please start again."}
    if ticket.session.token != session_token:
        # The ticket names its own class. Letting one be replayed against
        # another would turn a single geo check into a season ticket.
        return {"error": "That authorisation is for a different class."}
    if ticket.is_spent:
        return {"error": "That attempt has already been used."}
    if ticket.is_expired:
        return {"error": "This attempt timed out. Please start again."}
    if not ticket.session.is_open:
        return {"error": "This attendance link has closed."}
    if AttendanceRecord.objects.filter(
            session=ticket.session, student=profile).exists():
        return {"error": "Your attendance for this class is already marked."}

    if not antispoof_ready():
        # Deliberately loud and deliberately blocking. Running face marking
        # with no liveness check is worse than not running it: staff would
        # believe a photograph had been ruled out when nothing had ruled it out.
        log.error("Face marking blocked: ANTISPOOF_REQUIRED is on but "
                  "FACE_ANTISPOOF_MODEL is not set.")
        return {"error": "Face verification is not fully configured on this "
                         "server. Please ask your teacher to mark you."}

    enrolment = (FaceEnrolment.objects
                 .filter(user=user).prefetch_related("samples").first())
    embeddings = [s.embedding for s in enrolment.samples.all()] if enrolment else []
    embeddings = [e for e in embeddings if e]
    if not embeddings:
        return {"error": "No face is on file for you yet."}

    return {"ticket": ticket, "embeddings": embeddings}


@transaction.atomic
def complete_mark(*, ticket, score, liveness=None):
    """
    Spend the ticket and write the present mark.

    The ticket is spent first and conditionally: two frames can be judged a
    match at almost the same moment, and only one of them may produce a record.
    """
    spent = (FaceVerifyTicket.objects
             .filter(pk=ticket.pk, used_at__isnull=True)
             .update(used_at=timezone.now(), attempts=ticket.attempts + 1))
    if not spent:
        return {"ok": False, "message": "That attempt has already been used."}

    cleared = {
        "profile": ticket.student,
        "fingerprint": ticket.device_fingerprint,
        "distance": float(ticket.distance_m),
        "accuracy": ticket.accuracy_m,
        "latitude": float(ticket.latitude),
        "longitude": float(ticket.longitude),
    }
    try:
        record, distance = persist_mark(
            request=None, session=ticket.session, cleared=cleared,
            ip=ticket.ip, user_agent=ticket.user_agent)
    except AttendanceError as exc:
        return {"ok": False, "message": exc.message}
    except IntegrityError:
        return {"ok": False, "message": "Your attendance is already marked."}

    ActivityLog.log(
        actor=ticket.student.user, action="ATTENDANCE_FACE_VERIFIED",
        detail=(f"{ticket.session.subject.code} · match {score:.3f}"
                + (f" · liveness {liveness:.3f}" if liveness is not None else "")))
    return {"ok": True, "distance": distance,
            "message": "Recognised — your attendance is marked."}


def request_manual_mark(*, ticket, reason, attempts=0, best_score=0.0):
    """
    Hand the decision to the teacher.

    Nothing is marked here. The request appears on the teacher's live session
    screen and they decide — which is the right place for it, because the
    student is standing in front of them and the only open question is who
    they are.
    """
    request, created = ManualMarkRequest.objects.get_or_create(
        session=ticket.session, student=ticket.student,
        defaults={
            "ticket": ticket,
            "reason": (reason or "")[:120],
            "attempts": attempts,
            "best_score": round(float(best_score or 0), 3),
        },
    )
    if not created and request.status == ManualMarkRequest.Status.PENDING:
        request.attempts = attempts
        request.best_score = round(float(best_score or 0), 3)
        request.save(update_fields=["attempts", "best_score"])

    return {"ok": True,
            "message": "Your teacher has been asked to mark you. Stay where you are."}


def decide_manual_mark(*, request_obj, teacher, approve, remark=""):
    """Teacher's answer. Approving writes the record; refusing records that too."""
    from .services import manual_mark

    if request_obj.status != ManualMarkRequest.Status.PENDING:
        raise AttendanceError("That request has already been decided.", "DECIDED", 409)

    if approve:
        manual_mark(session=request_obj.session, student=request_obj.student,
                    teacher=teacher, present=True,
                    remark=remark or "Face not recognised — confirmed in class")
    request_obj.status = (ManualMarkRequest.Status.APPROVED if approve
                          else ManualMarkRequest.Status.REJECTED)
    request_obj.decided_by = teacher
    request_obj.decided_at = timezone.now()
    request_obj.save(update_fields=["status", "decided_by", "decided_at"])
    return request_obj
