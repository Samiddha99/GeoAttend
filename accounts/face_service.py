"""
Enrolling and clearing a student's face, with the rules in one place.

The view's whole job is to hand over three files; every decision about whether
they are acceptable lives here, so the same guarantees hold whatever calls it.
"""
import logging

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from . import face as face_engine
from .face import FaceError
from .models import ActivityLog, FaceEnrolment, FaceSample

log = logging.getLogger("geoattend")

# The order matters: it is the order the student is asked to do them in, and
# the order the results are reported back.
POSES = ("FRONT", "LEFT", "RIGHT")


def is_enabled():
    return bool(settings.FACE.get("ENABLED", True))


def read_upload(upload, pose):
    """Pull the bytes out of one upload, with a size ceiling."""
    max_bytes = int(settings.FACE.get("MAX_IMAGE_MB", 5)) * 1024 * 1024
    if upload is None:
        raise FaceError(f"The {pose.lower()} photo is missing.", "MISSING_IMAGE")
    if upload.size > max_bytes:
        raise FaceError("That photo is too large. Try again.", "IMAGE_TOO_LARGE")
    data = upload.read()
    upload.seek(0)
    return data


@transaction.atomic
def enrol(*, user, uploads, request=None):
    """
    Store three verified angles of one student's face.

    `uploads` maps pose name to an uploaded file. Every frame is analysed
    before anything is written: a half-finished enrolment would set the user's
    flag on a face the server never validated, and the flag is what the gate
    trusts.
    """
    if not is_enabled():
        raise FaceError("Face enrolment is turned off.", "DISABLED")
    if not user.is_student:
        # Staff do not mark attendance, so there is nothing for their face to
        # protect and no reason to hold it.
        raise FaceError("Only students enrol a face.", "NOT_A_STUDENT")
    if user.face_enrolled:
        raise FaceError(
            "Your face is already on file. Ask your department to clear it if "
            "it needs to be captured again.", "ALREADY_ENROLLED")

    results, payloads = {}, {}
    for pose in POSES:
        data = read_upload(uploads.get(pose), pose)
        try:
            results[pose] = face_engine.analyse(data, expected_pose=pose)
        except FaceError as exc:
            # Which of the three failed is the first thing anyone asks.
            exc.detail["pose"] = pose
            raise
        payloads[pose] = data

    # Three photos of three different people would otherwise sail through:
    # each one is a valid face at a valid angle.
    face_engine.check_same_person(results)

    FaceEnrolment.objects.filter(user=user).delete()
    enrolment = FaceEnrolment.objects.create(
        user=user, model_name=settings.FACE.get("MODEL_PACK", ""))
    for pose in POSES:
        measured = results[pose]
        sample = FaceSample(
            enrolment=enrolment, pose=pose,
            embedding=measured["embedding"],
            yaw=measured["yaw"],
            detect_score=measured["detect_score"],
        )
        sample.image.save(f"{pose.lower()}.jpg", ContentFile(payloads[pose]), save=False)
        sample.save()

    user.face_enrolled = True
    user.save(update_fields=["face_enrolled"])

    ActivityLog.log(request, actor=user, action="FACE_ENROLLED",
                    detail=f"{len(POSES)} angles captured")
    return enrolment


@transaction.atomic
def clear(*, user, actor, reason="", request=None):
    """
    Let a student capture their face again.

    Staff-only, deliberately. A student who can clear their own enrolment can
    re-enrol with a friend's face an hour before class, which would leave the
    whole feature decorative. The images go; the audit row stays, so "who
    allowed this, and why" has an answer later.
    """
    enrolment = FaceEnrolment.objects.filter(user=user).first()
    if enrolment is None and not user.face_enrolled:
        return False

    if enrolment is not None:
        for sample in enrolment.samples.all():
            # Remove the image from storage, not just the row pointing at it.
            sample.image.delete(save=False)
        enrolment.samples.all().delete()
        enrolment.reset_by = actor
        enrolment.reset_at = timezone.now()
        enrolment.reset_reason = (reason or "")[:200]
        enrolment.save(update_fields=["reset_by", "reset_at", "reset_reason"])

    user.face_enrolled = False
    user.save(update_fields=["face_enrolled"])
    ActivityLog.log(request, actor=actor, action="FACE_CLEARED",
                    detail=f"{user.email}{' · ' + reason if reason else ''}")
    return True


def needs_enrolment(user):
    """
    Is this user being held at the capture page?

    Only students, only once they have a password — someone who has not
    finished registration has a different unfinished step to do first, and
    bouncing them between two gates would trap them.
    """
    return bool(
        is_enabled()
        and getattr(user, "is_authenticated", False)
        and user.is_student
        and user.registration_completed
        and not user.face_enrolled
        and not user.is_superuser
    )
