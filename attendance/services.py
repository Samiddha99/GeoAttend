"""
The heart of the product: creating attendance sessions and validating a
student's attempt to mark themselves present.

Every rule here is server-side.  The browser only supplies raw GPS numbers;
it can never decide the outcome.
"""
import datetime as dt
from pathlib import Path

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from academics.models import Subject, TeacherAssignment
from academics.selectors import can_teach, departments_for, enrolled_students, is_enrolled
from core.http import client_ip
from core.utils import device_fingerprint, haversine_m, valid_coords

from .models import (
    AbsenceAttachment,
    AbsenceReason,
    AttendanceRecord,
    AttendanceSession,
    MarkAttempt,
    PlannedAbsence,
    PlannedAbsenceDecision,
)

CONF = settings.ATTENDANCE


class AttendanceError(Exception):
    """Carries a user-facing message plus a machine code for the UI."""

    def __init__(self, message, code="ERROR", status=400, **extra):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.extra = extra


# --------------------------------------------------------------------------- #
#  Teacher side
# --------------------------------------------------------------------------- #
def session_limits():
    """
    The bounds on link validity and fence radius, read live.

    Read from `settings` on every call rather than from the module-level CONF
    snapshot. CONF is bound once at import, so `override_settings` — and any
    runtime change — replaces the dict without CONF ever noticing, and a test
    that lowers the ceiling would silently keep testing the old one.
    """
    conf = settings.ATTENDANCE
    return {key: int(conf[key]) for key in (
        "MIN_EXPIRY_MIN", "MAX_EXPIRY_MIN", "DEFAULT_EXPIRY_MIN",
        "MIN_RADIUS_M", "MAX_RADIUS_M", "DEFAULT_RADIUS_M")}


def _bounded(value, key, label, unit, code):
    """
    One number, checked against its configured range.

    Refuses rather than clamps. Silently rounding 300 minutes down to 30 would
    hand the teacher a link that behaves differently from what they asked for,
    and they would only find out when it expired mid-lesson.

    Anything unparseable is refused too: `int("")` raising a ValueError that
    escapes as a 500 is not the message this deserves.
    """
    limits = session_limits()
    low, high = limits[f"MIN_{key}"], limits[f"MAX_{key}"]
    if value in (None, ""):
        return limits[f"DEFAULT_{key}"]
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise AttendanceError(
            f"{label} must be a whole number of {unit}.", code)
    if not (low <= number <= high):
        raise AttendanceError(
            f"{label} must be between {low} and {high} {unit}.", code)
    return number


def create_session(*, teacher, subject, batch, latitude, longitude, accuracy=0,
                   minutes=None, radius=None, note=""):
    if not batch.is_active:
        raise AttendanceError(
            f"Batch {batch.label} is archived. Re-activate it before taking attendance.",
            "BATCH_ARCHIVED", 409,
        )
    if not can_teach(teacher, subject, batch):
        raise AttendanceError(
            "You are not assigned to teach this subject to this batch.",
            "NOT_ASSIGNED", 403,
        )
    if not valid_coords(latitude, longitude):
        raise AttendanceError(
            "We could not read your location. Please allow location access and try again.",
            "NO_LOCATION",
        )

    minutes = _bounded(minutes, "EXPIRY_MIN", "Validity", "minutes", "BAD_EXPIRY")
    radius = _bounded(radius, "RADIUS_M", "Radius", "metres", "BAD_RADIUS")

    audience = enrolled_students(subject, batch)
    if not audience.exists():
        raise AttendanceError(
            "No registered students are enrolled in this subject for this batch.",
            "NO_AUDIENCE",
        )

    # Close any still-open session for the same subject+batch to avoid double links.
    AttendanceSession.objects.filter(
        subject=subject, batch=batch, status=AttendanceSession.Status.OPEN,
        expires_at__gt=timezone.now(),
    ).update(status=AttendanceSession.Status.CLOSED, closed_at=timezone.now())

    session = AttendanceSession.objects.create(
        teacher=teacher,
        subject=subject,
        batch=batch,
        latitude=round(float(latitude), 6),
        longitude=round(float(longitude), 6),
        accuracy_m=float(accuracy or 0),
        radius_m=radius,
        note=note[:140],
        expected_count=audience.count(),
        expires_at=timezone.now() + dt.timedelta(minutes=minutes),
    )
    # Re-anchor the expiry to `created_at`, which the database stamps on INSERT
    # — a moment after the `now()` above. Both the total-validity ceiling and
    # the manual-marking window are measured from `created_at`, so leaving the
    # two clocks a few milliseconds apart makes a "30 minute" link fractionally
    # short of its own ceiling, and the boundary impossible to state exactly.
    session.expires_at = session.created_at + dt.timedelta(minutes=minutes)
    session.save(update_fields=["expires_at"])
    return session


def notify_session(session):
    """
    Email the mark-link to every enrolled, activated student.

    Goes through the project's single mail function. Sends are queued rather
    than awaited: the teacher is standing in front of a class waiting for the
    link, so a slow mail provider must not hold up the response.
    """
    from notifications.mailer import send_template_mail

    sent = 0
    for student in enrolled_students(session.subject, session.batch):
        if not student.user.registration_completed:
            continue
        send_template_mail(
            f"Attendance open: {session.subject.code} ({session.batch.label})",
            student.user.email,
            "attendance_request",
            {
                "student": student,
                "session": session,
                "mark_url": session.mark_url,
                "minutes": max(int(session.seconds_left / 60), 1),
                "radius": session.radius_m,
            },
            messageGroup="ATTENDANCE_REQUEST",
            utm_source="Attendance request",
        )
        sent += 1
    return sent


# --------------------------------------------------------------------------- #
#  Student side
# --------------------------------------------------------------------------- #
def _log_attempt(session, user, reason, request=None, lat=None, lng=None,
                 accuracy=None, distance=None, fingerprint="", detail=""):
    MarkAttempt.objects.create(
        session=session,
        user=user if getattr(user, "is_authenticated", False) else None,
        reason=reason,
        latitude=lat if valid_coords(lat, lng or 0) else None,
        longitude=lng if valid_coords(lat or 0, lng) else None,
        accuracy_m=accuracy,
        distance_m=distance,
        ip=(client_ip(request) or None) if request else None,
        device_fingerprint=fingerprint,
        detail=detail[:250],
    )


def check_mark_allowed(*, request, session, latitude, longitude, accuracy=None,
                       client_hash=""):
    """
    Every gate that stands between a student and a present mark, with nothing
    written.

    Split out from mark_attendance so the same rules can run once over HTTP and
    the result be carried into the face-matching socket. Two copies of these
    checks would drift, and the one that drifted would be the one a student
    found.

    Returns the values the caller needs to persist: profile, fingerprint,
    distance and accuracy.
    """
    user = request.user
    fingerprint = device_fingerprint(request, client_hash)

    def boom(message, code, reason, status=400, distance=None, **extra):
        _log_attempt(session, user, reason, request, latitude, longitude,
                     accuracy, distance, fingerprint, message)
        raise AttendanceError(message, code, status, **extra)

    # --- who is asking? ---------------------------------------------------- #
    if not user.is_student:
        boom("Only student accounts can mark attendance.", "NOT_STUDENT",
             MarkAttempt.Reason.NOT_STUDENT, 403)
    profile = getattr(user, "student_profile", None)
    if profile is None:
        boom("Your student profile is incomplete. Please contact your department.",
             "NO_PROFILE", MarkAttempt.Reason.NOT_STUDENT, 403)

    # --- is the link alive? ------------------------------------------------ #
    if session.status == AttendanceSession.Status.CANCELLED:
        boom("This attendance request was cancelled by the teacher.", "CANCELLED",
             MarkAttempt.Reason.CANCELLED, 410)
    if not session.is_open:
        boom("This attendance link has expired. Please ask your teacher to reopen it.",
             "EXPIRED", MarkAttempt.Reason.EXPIRED, 410)
    # A direct link must not outlive its batch: archiving hides the cohort
    # everywhere, so marking into it would create an invisible record.
    if not session.batch.is_active:
        boom("This class belongs to an archived batch and is no longer accepting "
             "attendance.", "BATCH_ARCHIVED", MarkAttempt.Reason.BATCH_ARCHIVED, 410)

    # --- is it meant for this student? ------------------------------------- #
    if profile.batch_id != session.batch_id:
        boom(f"This attendance request is for batch {session.batch.label}, not yours.",
             "WRONG_BATCH", MarkAttempt.Reason.WRONG_BATCH, 403)
    if not is_enrolled(profile, session.subject):
        boom(f"You are not enrolled in {session.subject.code}.", "NOT_ENROLLED",
             MarkAttempt.Reason.NOT_ENROLLED, 403)
    if AttendanceRecord.objects.filter(session=session, student=profile).exists():
        boom("Your attendance for this class is already marked.", "DUPLICATE",
             MarkAttempt.Reason.DUPLICATE, 409)

    # --- device checks (anti proxy attendance) ----------------------------- #
    if CONF["ENFORCE_DEVICE_LOCK"] and user.device_id and fingerprint != user.device_id:
        boom("This is not the device registered to your account. Attendance must be "
             "marked from your own device.", "DEVICE_MISMATCH",
             MarkAttempt.Reason.DEVICE_MISMATCH, 403)
    if CONF["BLOCK_SHARED_DEVICE"] and AttendanceRecord.objects.filter(
        session=session, device_fingerprint=fingerprint
    ).exclude(device_fingerprint="").exists():
        boom("Another student has already marked attendance from this device for "
             "this class.", "SHARED_DEVICE", MarkAttempt.Reason.SHARED_DEVICE, 409)

    # --- geo-fence --------------------------------------------------------- #
    if not valid_coords(latitude, longitude):
        boom("We could not read your location. Please enable GPS/location permission "
             "and try again.", "NO_LOCATION", MarkAttempt.Reason.NO_LOCATION)
    accuracy = float(accuracy or 0)
    if accuracy and accuracy > CONF["MAX_GPS_ACCURACY_M"]:
        boom(f"Your GPS fix is too imprecise (±{accuracy:.0f} m). Step near a window "
             "or enable high-accuracy location, then retry.", "LOW_ACCURACY",
             MarkAttempt.Reason.LOW_ACCURACY)

    distance = haversine_m(session.latitude, session.longitude, latitude, longitude)
    if distance > session.radius_m:
        boom(
            f"You are not present in the class. You appear to be {distance:.0f} m away "
            f"(allowed: {session.radius_m} m).",
            "OUT_OF_RANGE", MarkAttempt.Reason.OUT_OF_RANGE, 403, distance=distance,
            distance_m=round(distance, 1),
        )

    return {
        "profile": profile,
        "fingerprint": fingerprint,
        "distance": distance,
        "accuracy": accuracy,
        "latitude": float(latitude),
        "longitude": float(longitude),
    }


def persist_mark(*, request, session, cleared, ip=None, user_agent=""):
    """Write the present mark from an already-cleared attempt."""
    user = request.user if request is not None else cleared["profile"].user
    profile = cleared["profile"]
    distance = cleared["distance"]
    try:
        with transaction.atomic():
            record = AttendanceRecord.objects.create(
                session=session,
                student=profile,
                status=AttendanceRecord.Status.PRESENT,
                marked_at=timezone.now(),
                latitude=round(cleared["latitude"], 6),
                longitude=round(cleared["longitude"], 6),
                accuracy_m=cleared["accuracy"] or None,
                distance_m=round(distance, 2),
                ip=(client_ip(request) if request is not None else ip) or None,
                user_agent=(request.META.get("HTTP_USER_AGENT", "")[:400]
                            if request is not None else user_agent[:400]),
                device_fingerprint=cleared["fingerprint"],
            )
    except IntegrityError:
        raise AttendanceError("Your attendance for this class is already marked.",
                              "DUPLICATE", 409)

    user.bind_device(cleared["fingerprint"])
    _log_attempt(session, user, MarkAttempt.Reason.OK, request,
                 cleared["latitude"], cleared["longitude"], cleared["accuracy"],
                 distance, cleared["fingerprint"], "accepted")
    return record, round(distance, 1)


def mark_attendance(*, request, session, latitude, longitude, accuracy=None,
                    client_hash=""):
    """
    Validate and persist a student's presence.
    Raises AttendanceError with a friendly message on every failure path.
    """
    cleared = check_mark_allowed(
        request=request, session=session, latitude=latitude, longitude=longitude,
        accuracy=accuracy, client_hash=client_hash)
    return persist_mark(request=request, session=session, cleared=cleared)


# --------------------------------------------------------------------------- #
#  Teacher overrides
# --------------------------------------------------------------------------- #
def manual_mark_minutes():
    # Read from settings on each call rather than the module-level CONF
    # snapshot, so the window can be changed (and overridden in tests) without
    # reimporting this module.
    return int(settings.ATTENDANCE.get("MANUAL_MARK_MINUTES", 30) or 0)


def manual_mark_deadline(session):
    """The moment after which nobody may be marked present by hand."""
    minutes = manual_mark_minutes()
    if minutes <= 0:
        return None
    return session.created_at + dt.timedelta(minutes=minutes)


def manual_mark_open(session, now=None):
    deadline = manual_mark_deadline(session)
    if deadline is None:
        return False
    return (now or timezone.now()) <= deadline


def manual_mark_seconds_left(session, now=None):
    deadline = manual_mark_deadline(session)
    if deadline is None:
        return 0
    return max(0, int((deadline - (now or timezone.now())).total_seconds()))


def check_manual_window(session):
    """
    Raise unless a hand-entered present mark is still allowed.

    Counted from when the link was created, so the teacher is still in the room
    and can see who is in front of them. Afterwards "mark present" would be an
    unverifiable claim about the past.

    Only marking *present* is bound by this. Marking someone absent — undoing a
    mistake — stays open, because the window exists to stop attendance being
    conjured up later, and removing a mark cannot do that.
    """
    minutes = manual_mark_minutes()
    if minutes <= 0:
        raise AttendanceError(
            "Manual marking is switched off for this institute.",
            "MANUAL_DISABLED", 403)
    if not manual_mark_open(session):
        raise AttendanceError(
            f"Manual marking closed {minutes} minutes after this link was "
            "created. Ask the student to submit an absence reason instead.",
            "MANUAL_WINDOW_CLOSED", 403)


def manual_mark(*, session, student, teacher, present=True, remark=""):
    if session.teacher_id != teacher.id and not (teacher.is_hod or teacher.is_head):
        raise AttendanceError("You can only edit your own sessions.", "FORBIDDEN", 403)
    if not is_enrolled(student, session.subject) or student.batch_id != session.batch_id:
        raise AttendanceError("That student is not in this class.", "NOT_ENROLLED", 400)
    if present:
        check_manual_window(session)
        record, created = AttendanceRecord.objects.update_or_create(
            session=session, student=student,
            defaults={
                "status": AttendanceRecord.Status.MANUAL,
                "marked_by": teacher,
                "remark": remark[:200] or "Marked by teacher",
                "marked_at": timezone.now(),
            },
        )
        return record, "Marked present."
    AttendanceRecord.objects.filter(session=session, student=student).delete()
    return None, "Marked absent."


# --------------------------------------------------------------------------- #
#  Absence reasons
# --------------------------------------------------------------------------- #
def reason_window_days():
    # Read from settings each call rather than the module-level CONF snapshot,
    # so the window can actually be changed (and overridden in tests) without
    # reimporting this module.
    return int(settings.ATTENDANCE.get("ABSENCE_REASON_DAYS", 3) or 0)


def reason_deadline(session):
    """The last date a reason may be submitted for `session`, or None if off."""
    days = reason_window_days()
    if days <= 0:
        return None
    return session.session_date + dt.timedelta(days=days)


def reason_window_open(session, today=None):
    deadline = reason_deadline(session)
    if deadline is None:
        return False
    return (today or timezone.localdate()) <= deadline


def can_review_reason(user, reason):
    """
    Who may act on a submitted reason.

    The teacher who actually took the class, their HoD, or the head. Not every
    teacher of the subject — the person who ran the session is the one who
    knows whether the student was there.
    """
    if not user.is_authenticated:
        return False
    if user.is_head:
        return user.institute_id == reason.session.subject.department.institute_id
    if user.is_hod:
        return reason.session.subject.department_id in set(
            departments_for(user).values_list("id", flat=True))
    if user.is_teacher:
        return reason.session.teacher_id == user.pk
    return False


# --------------------------------------------------------------------------- #
#  Evidence attached to an absence request
# --------------------------------------------------------------------------- #
#
# Keyed by the signature we find at the head of the file, not by what the
# browser claims. A content-type header is chosen by the uploader, and an
# extension is chosen by whoever renamed the file — neither is evidence of
# anything. The first few bytes are much harder to get wrong by accident and
# much harder to lie about usefully.
ATTACHMENT_SIGNATURES = (
    (b"%PDF-", "application/pdf", {".pdf"}),
    (b"\x89PNG\r\n\x1a\n", "image/png", {".png"}),
    (b"\xff\xd8\xff", "image/jpeg", {".jpg", ".jpeg"}),
)
ATTACHMENT_KINDS = "PDF, JPEG, PNG, WebP or HEIC"


def _sniff_attachment(head):
    """The real type of a file from its first bytes, or ``None`` if unknown."""
    for magic, content_type, extensions in ATTACHMENT_SIGNATURES:
        if head.startswith(magic):
            return content_type, extensions
    # WebP and HEIC are container formats: the marker sits at a fixed offset
    # rather than at byte zero, so they cannot use the prefix table above.
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp", {".webp"}
    if head[4:8] == b"ftyp" and head[8:12] in (b"heic", b"heix", b"hevc", b"mif1", b"msf1"):
        return "image/heic", {".heic", ".heif"}
    return None, set()


def attachment_limits():
    """
    Read the limits live rather than from the CONF snapshot, so a test using
    override_settings actually changes them.
    """
    conf = settings.ATTENDANCE
    return (int(conf.get("ATTACHMENT_MAX_FILES", 5)),
            int(conf.get("ATTACHMENT_MAX_TOTAL_MB", 20)))


def validate_attachments(files):
    """
    Check a whole batch of uploads and return them with their real types.

    Everything is checked *before* anything is written, so a request that
    breaks a rule leaves no half-saved reason and no orphaned file behind. The
    return value is a list of ``(file, content_type)`` pairs.
    """
    files = [f for f in (files or []) if f]
    if not files:
        return []

    max_files, max_mb = attachment_limits()
    if max_files <= 0:
        raise AttendanceError("Attaching files is turned off.", "ATTACHMENTS_DISABLED")
    if len(files) > max_files:
        raise AttendanceError(
            f"You can attach at most {max_files} files — you chose {len(files)}.",
            "TOO_MANY_FILES")

    max_bytes = max_mb * 1024 * 1024
    checked, total = [], 0
    for upload in files:
        total += upload.size
        if total > max_bytes:
            raise AttendanceError(
                f"The attachments come to more than {max_mb} MB in total. "
                "Please remove one, or use a smaller scan.", "FILES_TOO_LARGE")

        head = upload.read(32)
        upload.seek(0)
        content_type, extensions = _sniff_attachment(head)
        if content_type is None:
            raise AttendanceError(
                f"“{upload.name}” is not a {ATTACHMENT_KINDS} file.", "BAD_FILE_TYPE")
        # The extension must agree with the contents. A PDF named .jpg is
        # harmless in itself, but a mismatch is the shape of an attempt to get
        # something served as a type it is not.
        if Path(upload.name).suffix.lower() not in extensions:
            raise AttendanceError(
                f"“{upload.name}” does not match its contents — it is really a "
                f"{content_type} file. Please rename it and try again.",
                "EXTENSION_MISMATCH")
        checked.append((upload, content_type))

    return checked


def can_view_attachment(user, attachment):
    """
    Who may open one piece of evidence.

    The student who uploaded it, and whoever is entitled to decide the request
    it belongs to — reusing the review rules rather than inventing a second,
    looser set that would quietly drift from them. A medical certificate is not
    something every teacher in the institute should be able to read.
    """
    if not user.is_authenticated:
        return False
    if user.is_student or user.is_guardian:
        # A guardian sees the evidence their child attached, and only that.
        # The medical certificate is about their child; withholding it while
        # showing the request it belongs to would be a strange half-view.
        from accounts.guardians import acting_profile

        profile = acting_profile(user)
        if profile is None:
            return False
        parent = attachment.reason or attachment.planned
        return parent is not None and parent.student_id == profile.pk
    if attachment.reason_id:
        return can_review_reason(user, attachment.reason)
    if attachment.planned_id:
        # Any one subject they may decide is enough: the evidence covers the
        # whole request, not one subject of it.
        return any(can_review_planned(user, d)
                   for d in attachment.planned.decisions.all())
    return False


def store_attachments(checked, *, reason=None, planned=None):
    """Write validated uploads against exactly one parent request."""
    return [
        AbsenceAttachment.objects.create(
            reason=reason, planned=planned, file=upload,
            original_name=Path(upload.name).name[:255],
            content_type=content_type, size_bytes=upload.size,
        )
        for upload, content_type in checked
    ]


@transaction.atomic
def submit_absence_reason(*, student, session, text, files=None):
    """
    Record a student's explanation for missing one class.

    Every rule is checked here rather than in the view, so the same guarantees
    hold however the request arrives.
    """
    text = (text or "").strip()
    if not text:
        raise AttendanceError("Please write a reason.", "EMPTY_REASON")
    if len(text) > 1000:
        raise AttendanceError("Please keep the reason under 1000 characters.", "TOO_LONG")

    if session.batch_id != student.batch_id:
        raise AttendanceError("That class was not for your batch.", "WRONG_BATCH")
    if not is_enrolled(student, session.subject):
        raise AttendanceError(
            f"You are not enrolled in {session.subject.code}.", "NOT_ENROLLED")

    record = AttendanceRecord.objects.filter(session=session, student=student).first()
    if record and record.is_present:
        raise AttendanceError(
            "You were marked present for this class, so there is nothing to explain.",
            "NOT_ABSENT")

    days = reason_window_days()
    if days <= 0:
        raise AttendanceError("Submitting reasons is turned off.", "DISABLED")
    if not reason_window_open(session):
        raise AttendanceError(
            f"The {days}-day window for explaining this class has closed "
            f"(it ended on {reason_deadline(session):%d %b %Y}).", "WINDOW_CLOSED")

    existing = AbsenceReason.objects.filter(session=session, student=student).first()
    if existing is not None:
        # One attempt per class, including after a rejection.
        raise AttendanceError(
            "You have already given a reason for this class.", "ALREADY_SUBMITTED")

    # Validated before the reason exists: there is one attempt per class, so a
    # reason saved alongside a rejected upload would burn that attempt and
    # leave the student unable to try again with a smaller file.
    checked = validate_attachments(files)
    reason = AbsenceReason.objects.create(session=session, student=student, reason=text)
    store_attachments(checked, reason=reason)
    return reason


@transaction.atomic
def review_absence_reason(*, reason, actor, approve, remark=""):
    """Approve or reject. Reviewing again is refused rather than silently redone."""
    if not can_review_reason(actor, reason):
        raise AttendanceError(
            "Only the teacher who took this class, the HoD or the head can review it.",
            "NOT_REVIEWER", status=403)
    if not reason.is_pending:
        raise AttendanceError(
            f"This reason was already {reason.get_status_display().lower()}.",
            "ALREADY_REVIEWED")

    reason.status = (AbsenceReason.Status.APPROVED if approve
                     else AbsenceReason.Status.REJECTED)
    reason.review_remark = (remark or "").strip()[:300]
    reason.reviewed_by = actor
    reason.reviewed_at = timezone.now()
    reason.save(update_fields=["status", "review_remark", "reviewed_by", "reviewed_at"])
    return reason


# --------------------------------------------------------------------------- #
#  Planned absences (declared in advance)
# --------------------------------------------------------------------------- #
MAX_PLANNED_DAYS = 60


def planned_subjects_for(student, subject_ids=None):
    """
    The subjects a planned absence will cover.

    Resolved once, at submission, and frozen into decision rows — so a later
    enrolment change cannot quietly widen an absence somebody already approved.
    """
    qs = Subject.objects.filter(
        enrollments__student=student, enrollments__is_active=True).distinct()
    if subject_ids:
        qs = qs.filter(id__in=subject_ids)
    return list(qs)


def can_review_planned(user, decision):
    """
    Who may decide one subject of a planned absence.

    The teacher allocated to that subject for the student's batch — the nearest
    thing to "the teacher whose class it is" when the class does not exist yet —
    plus the HoD and the head.
    """
    if not user.is_authenticated:
        return False
    department_id = decision.subject.department_id
    if user.is_head:
        return user.institute_id == decision.subject.department.institute_id
    if user.is_hod:
        return department_id in set(departments_for(user).values_list("id", flat=True))
    if user.is_teacher:
        return TeacherAssignment.objects.filter(
            teacher=user, subject=decision.subject,
            batch=decision.planned.student.batch, is_active=True,
        ).exists()
    return False


@transaction.atomic
def submit_planned_absence(*, student, from_date, to_date, text, subject_ids=None,
                           files=None):
    """File an absence in advance. Creates one decision row per covered subject."""
    text = (text or "").strip()
    if not text:
        raise AttendanceError("Please write a reason.", "EMPTY_REASON")
    if len(text) > 1000:
        raise AttendanceError("Please keep the reason under 1000 characters.", "TOO_LONG")
    if not from_date or not to_date:
        raise AttendanceError("Please choose both dates.", "NO_DATES")
    if to_date < from_date:
        raise AttendanceError("The end date cannot be before the start date.", "BAD_RANGE")

    today = timezone.localdate()
    if from_date <= today:
        # The point of this form is advance notice. A class already held is the
        # other feature's job, and it has its own window and its own reviewer.
        raise AttendanceError(
            "Planned absences are for future dates. To explain a class that has "
            "already happened, use the absent mark in your class history.",
            "NOT_FUTURE")
    span = (to_date - from_date).days + 1
    if span > MAX_PLANNED_DAYS:
        raise AttendanceError(
            f"A planned absence can cover at most {MAX_PLANNED_DAYS} days.", "TOO_LONG_RANGE")

    clash = PlannedAbsence.objects.filter(
        student=student, cancelled_at__isnull=True,
        from_date__lte=to_date, to_date__gte=from_date,
    ).first()
    if clash is not None:
        raise AttendanceError(
            f"You already have a planned absence covering "
            f"{clash.from_date:%d %b} – {clash.to_date:%d %b}.", "OVERLAP")

    subjects = planned_subjects_for(student, subject_ids)
    if not subjects:
        raise AttendanceError(
            "You are not enrolled in any of the subjects selected.", "NO_SUBJECTS")

    checked = validate_attachments(files)
    planned = PlannedAbsence.objects.create(
        student=student, from_date=from_date, to_date=to_date, reason=text,
        all_subjects=not bool(subject_ids),
    )
    PlannedAbsenceDecision.objects.bulk_create([
        PlannedAbsenceDecision(planned=planned, subject=s) for s in subjects
    ])
    store_attachments(checked, planned=planned)
    return planned


@transaction.atomic
def review_planned_decision(*, decision, actor, approve, remark=""):
    """Approve or reject one subject of a planned absence."""
    if not can_review_planned(actor, decision):
        raise AttendanceError(
            "Only a teacher of this subject, the HoD or the head can review it.",
            "NOT_REVIEWER", status=403)
    if not decision.is_pending:
        raise AttendanceError(
            f"This was already {decision.get_status_display().lower()}.", "ALREADY_REVIEWED")

    decision.status = (AbsenceReason.Status.APPROVED if approve
                       else AbsenceReason.Status.REJECTED)
    decision.review_remark = (remark or "").strip()[:300]
    decision.reviewed_by = actor
    decision.reviewed_at = timezone.now()
    decision.save(update_fields=["status", "review_remark", "reviewed_by", "reviewed_at"])
    return decision


@transaction.atomic
def cancel_planned_absence(*, planned, actor):
    """A student calls off an absence they no longer expect to take."""
    if planned.student.user_id != actor.pk:
        raise AttendanceError("That is not your planned absence.", "NOT_OWNER", status=403)
    if planned.is_cancelled:
        raise AttendanceError("That planned absence is already cancelled.", "ALREADY")
    if planned.from_date <= timezone.localdate():
        raise AttendanceError(
            "It has already started, so it can no longer be cancelled.", "STARTED")
    planned.cancelled_at = timezone.now()
    planned.save(update_fields=["cancelled_at"])
    return planned


def planned_cover_for(student, sessions):
    """
    Map session id -> the decision excusing that class, for absent students.

    Built in two queries for a whole class history rather than one per row.
    Returns only sessions a live (non-cancelled) planned absence covers.
    """
    if not sessions:
        return {}
    dates = [s.session_date for s in sessions]
    planned = list(
        PlannedAbsence.objects.filter(
            student=student, cancelled_at__isnull=True,
            from_date__lte=max(dates), to_date__gte=min(dates),
        ).prefetch_related("decisions__subject", "decisions__reviewed_by", "attachments")
    )
    if not planned:
        return {}
    cover = {}
    for session in sessions:
        for absence in planned:
            if not (absence.from_date <= session.session_date <= absence.to_date):
                continue
            match = next((d for d in absence.decisions.all()
                          if d.subject_id == session.subject_id), None)
            if match is not None:
                cover[session.id] = (absence, match)
                break
    return cover
