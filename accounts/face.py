"""
Face enrolment: turning three uploaded frames into three verified templates.

Everything here runs on the server, and that is the whole point. The capture
page does its own pose tracking so the student gets useful live feedback, but
none of what the browser reports is trusted — a student who wants to cheat can
post three photographs of anyone they like and claim any pose they please. So
every frame is re-detected, re-posed and re-checked here, and the browser's
opinion is never read.

The model is InsightFace (SCRFD detection + ArcFace embeddings), loaded lazily:
importing it costs a few hundred megabytes of resident memory, which a web
worker should not pay for until someone actually enrols.
"""
import logging
import math
import os
import warnings

from django.conf import settings

log = logging.getLogger("geoattend")

# Which InsightFace pack to load. `buffalo_s` is the small one — noticeably
# lighter on a shared CPU, which is what this runs on. `buffalo_l` is more
# accurate and worth switching to on better hardware; the model name is stored
# with every enrolment so a change is detectable rather than silent.
MODEL_PACK = "buffalo_s"

_app = None


class FaceError(Exception):
    """
    A frame that cannot be enrolled, with a message worth showing.

    `detail` carries the numbers behind the refusal — the measured angle, say —
    for the log only. The student gets the sentence; whoever is debugging gets
    the reading that produced it, which is the difference between "it keeps
    refusing" and "it measured 51° and the ceiling is 45".
    """

    def __init__(self, message, code="FACE_ERROR", **detail):
        super().__init__(message)
        self.message = message
        self.code = code
        self.detail = detail


def _conf(key, default):
    return settings.FACE.get(key, default)


def get_app():
    """
    The InsightFace analyser, built once per process.

    Not built at import time: the first enrolment of the day pays for it, and a
    deployment that never enrols anyone never pays at all.
    """
    global _app
    if _app is None:
        try:
            from insightface.app import FaceAnalysis
        except ImportError as exc:  # pragma: no cover - depends on deployment
            raise FaceError(
                "Face recognition is not available on this server.",
                "MODEL_UNAVAILABLE") from exc
        # InsightFace's alignment code calls scikit-image's `estimate()`, which
        # is deprecated, and the warning fires on every face it processes —
        # enough noise to bury our own log lines. Silenced narrowly, by message
        # and by the module that raises it, so a FutureWarning from anywhere
        # else still gets through. Nothing to fix on our side: it is their call
        # into their dependency, and editing site-packages would be undone by
        # the next install.
        warnings.filterwarnings(
            "ignore", category=FutureWarning,
            message=r".*`estimate` is deprecated.*",
            module=r"insightface\.utils\.face_align",
        )
        app = FaceAnalysis(name=_conf("MODEL_PACK", MODEL_PACK),
                           providers=["CPUExecutionProvider"])
        # det_size is the trade-off dial: smaller is faster and misses small or
        # far-away faces. An enrolment selfie fills the frame, so 480 is ample.
        app.prepare(ctx_id=-1, det_size=(480, 480))
        _app = app
    return _app


def decode_image(data):
    """Bytes to an RGB array, without trusting the declared content type."""
    import io

    import numpy as np
    from PIL import Image, UnidentifiedImageError

    try:
        image = Image.open(io.BytesIO(data))
        image.verify()                      # cheap structural check
        image = Image.open(io.BytesIO(data))    # verify() exhausts the file
        image = image.convert("RGB")
    except (UnidentifiedImageError, OSError) as exc:
        raise FaceError("That capture is not a readable image.", "BAD_IMAGE") from exc

    max_side = int(_conf("MAX_IMAGE_SIDE", 1600))
    if max(image.size) > max_side:
        # A phone can hand over an 8 MP frame. Nothing downstream benefits, and
        # detection on a huge image is slow for no gain in accuracy.
        ratio = max_side / max(image.size)
        image = image.resize((int(image.width * ratio), int(image.height * ratio)))
    return np.asarray(image)


def yaw_of(face):
    """
    Head rotation left/right in degrees, positive when the subject turned to
    their OWN left.

    Deliberately computed from the detector's five keypoints and nothing else.
    InsightFace also exposes `pose` as (pitch, yaw, roll) when the model pack
    includes the 3D landmark model, but its sign convention is not ours, and
    `buffalo_s` does not provide it at all — so using it would mean the meaning
    of "left" silently flipped the day someone switched packs. This is the same
    geometry the capture page uses, so the two always agree.

    In the image frame the subject's left is the larger-x side, so a nose that
    sits right of the midpoint between the eye keypoints means the head has
    turned to the subject's left.
    """
    kps = getattr(face, "kps", None)
    if kps is None or len(kps) < 3:
        raise FaceError("Could not read the position of your head.", "NO_POSE")

    eye_a, eye_b, nose = float(kps[0][0]), float(kps[1][0]), float(kps[2][0])
    # abs() and a midpoint, rather than assuming which keypoint is which eye:
    # the sign then rests only on where the nose is.
    eye_span = abs(eye_b - eye_a)
    if eye_span < 1e-6:
        raise FaceError("Could not read the position of your head.", "NO_POSE")
    midpoint = (eye_a + eye_b) / 2.0
    # -1 (nose over one eye) .. +1 (over the other), scaled to roughly the
    # right number of degrees over the ±45° range we care about. A guide rail,
    # not a measurement instrument.
    offset = (nose - midpoint) / (eye_span / 2.0)
    return float(max(-1.0, min(1.0, offset)) * 45.0)


def _occlusion_report(image, face):
    """
    A cheap look for a mask, sunglasses or a low cap.

    Honest about what it is: contrast heuristics, not a trained classifier.
    Cloth over the mouth and dark lenses over the eyes both flatten a region
    that is normally full of edges, so low local gradient energy there is
    suspicious. It catches the obvious cases and will miss a clear-lensed pair
    of spectacles entirely.

    `FACE["OCCLUSION_MODEL"]` is the hook for doing this properly: point it at
    an ONNX attribute classifier and this heuristic steps aside.
    """
    import numpy as np

    box = [int(v) for v in face.bbox]
    x1, y1, x2, y2 = box
    h, w = image.shape[:2]
    x1, y1 = max(x1, 0), max(y1, 0)
    x2, y2 = min(x2, w), min(y2, h)
    if x2 - x1 < 20 or y2 - y1 < 20:
        return {}

    grey = np.asarray(image[y1:y2, x1:x2]).mean(axis=2)
    height = grey.shape[0]

    def energy(top, bottom):
        band = grey[int(height * top):int(height * bottom)]
        if band.size == 0:
            return 0.0
        gy, gx = np.gradient(band)
        return float(np.hypot(gx, gy).mean())

    eyes = energy(0.20, 0.45)
    mouth = energy(0.62, 0.92)
    overall = energy(0.0, 1.0) or 1.0
    return {
        "eye_energy": eyes / overall,
        "mouth_energy": mouth / overall,
    }


def analyse(data, *, expected_pose=None):
    """
    Read one captured frame and return what the server measured.

    Raises FaceError with a message the student can act on — "move into better
    light" is useful, "validation failed" is not.
    """
    import numpy as np

    image = decode_image(data)
    faces = get_app().get(image)

    if not faces:
        raise FaceError(
            "No face was found in that photo. Move into better light and "
            "fill the frame with your face.", "NO_FACE")
    if len(faces) > 1:
        # Not pedantry: a second person in shot is how a proxy enrolment starts.
        raise FaceError(
            "More than one face is in the photo. Make sure nobody else is in "
            "the frame.", "MULTIPLE_FACES")

    face = faces[0]
    if float(face.det_score) < float(_conf("MIN_DETECT_SCORE", 0.6)):
        raise FaceError(
            "That photo is too blurred or too dark to use. Try again in "
            "better light.", "LOW_QUALITY")

    x1, y1, x2, y2 = face.bbox
    face_height = float(y2 - y1)
    if face_height < float(_conf("MIN_FACE_PX", 110)):
        raise FaceError("Your face is too small in the frame. Move closer.",
                        "FACE_TOO_SMALL")

    yaw = yaw_of(face)
    if expected_pose is not None:
        _check_pose(yaw, expected_pose)

    occlusion = _occlusion_report(image, face)
    _check_occlusion(occlusion)

    embedding = np.asarray(face.normed_embedding, dtype=float)
    return {
        "embedding": [round(float(v), 6) for v in embedding],
        "yaw": round(yaw, 2),
        "detect_score": round(float(face.det_score), 4),
        "face_height": round(face_height, 1),
        "occlusion": occlusion,
    }


def _check_pose(yaw, expected_pose):
    """
    The head must actually be where the instruction asked it to be.

    Under- and over-rotation get different codes on purpose. One message
    covering both ("turn a little to your left — not too far") tells the
    student nothing about which way to correct, and tells whoever reads the log
    even less.
    """
    front_max = float(_conf("FRONT_MAX_YAW", 12))
    turn_min = float(_conf("TURN_MIN_YAW", 12))
    turn_max = float(_conf("TURN_MAX_YAW", 55))

    if expected_pose == "FRONT":
        if abs(yaw) > front_max:
            raise FaceError("Look straight at the camera for this one.",
                            "POSE_NOT_FRONT", yaw=yaw)
        return

    if expected_pose in ("LEFT", "RIGHT"):
        side = "left" if expected_pose == "LEFT" else "right"
        # Positive yaw is a turn to the subject's left, so measuring "toward
        # the side we asked for" makes both cases read the same way.
        towards = yaw if expected_pose == "LEFT" else -yaw
        if towards < 0:
            raise FaceError(
                f"That is the wrong way — please turn to your {side}.",
                "POSE_WRONG_SIDE", yaw=yaw)
        if towards < turn_min:
            raise FaceError(
                f"Turn your head a little further to your {side}.",
                "POSE_NOT_ENOUGH", yaw=yaw)
        # Past this the far side of the face is hidden and the embedding from
        # it is worth less than the one it replaces.
        if towards > turn_max:
            raise FaceError(
                f"That is turned too far. Come back toward the camera a little.",
                "POSE_TOO_FAR", yaw=yaw)
        return

    raise FaceError("Unknown capture step.", "BAD_POSE")


def _check_occlusion(report):
    if not report:
        return
    if report.get("eye_energy", 1.0) < float(_conf("MIN_EYE_ENERGY", 0.55)):
        raise FaceError(
            "Your eyes are covered. Please remove sunglasses, glasses or a cap "
            "and try again.", "EYES_COVERED")
    if report.get("mouth_energy", 1.0) < float(_conf("MIN_MOUTH_ENERGY", 0.45)):
        raise FaceError(
            "Your mouth and nose are covered. Please remove your mask and try "
            "again.", "FACE_COVERED")


def cosine(a, b):
    """Similarity between two already-normalised embeddings."""
    dot = sum(x * y for x, y in zip(a, b))
    # normed_embedding is unit length, so the dot product is the cosine — but
    # not if someone hands us a raw vector, so normalise defensively.
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def check_same_person(results):
    """
    All three captures must be the same face.

    Without this, "front", "left" and "right" can be three different people —
    which is the single cheapest way to poison an enrolment, and it costs one
    comparison to rule out.
    """
    threshold = float(_conf("SAME_PERSON_MIN", 0.45))
    poses = list(results)
    for i in range(len(poses)):
        for j in range(i + 1, len(poses)):
            score = cosine(results[poses[i]]["embedding"],
                           results[poses[j]]["embedding"])
            if score < threshold:
                log.warning("Enrolment rejected: %s vs %s similarity %.3f",
                            poses[i], poses[j], score)
                raise FaceError(
                    "Those photos do not look like the same person. Please "
                    "capture all three yourself, one after the other.",
                    "DIFFERENT_PEOPLE")


# --------------------------------------------------------------------------- #
#  Live verification: one video frame against a student's stored vectors
# --------------------------------------------------------------------------- #
_live_app = None
_antispoof = None


def get_live_app():
    """
    A second analyser tuned for the live path.

    Same models, smaller detection input. An enrolment photo is inspected once
    and can afford 480px; a video frame is inspected many times a minute and the
    face already fills it, because the browser crops to the face before sending.
    Detection cost scales with that number, and it is the difference between
    keeping up with a class and falling behind it.
    """
    global _live_app
    if _live_app is None:
        from insightface.app import FaceAnalysis

        warnings.filterwarnings(
            "ignore", category=FutureWarning,
            message=r".*`estimate` is deprecated.*",
            module=r"insightface\.utils\.face_align",
        )
        app = FaceAnalysis(name=_conf("MODEL_PACK", MODEL_PACK),
                           providers=["CPUExecutionProvider"])
        app.prepare(ctx_id=-1, det_size=(int(_conf("LIVE_DET_SIZE", 320)),) * 2)
        _live_app = app
    return _live_app


def get_antispoof():
    """
    The passive liveness model, or None if none is configured.

    Deliberately not bundled: anti-spoofing models carry their own licences and
    their own input conventions, so the path, the input size and which output
    means "real" all come from settings. See FACE["ANTISPOOF_*"].
    """
    global _antispoof
    if _antispoof is None:
        path = _conf("ANTISPOOF_MODEL", "")
        if not path:
            return None
        if not os.path.exists(path):
            # Loud and specific. onnxruntime's own NoSuchFile arrives from four
            # frames deep with the model path buried in it, once per frame;
            # this says the one thing worth knowing.
            raise FaceError(
                "The liveness model is missing on this server.",
                "ANTISPOOF_MISSING", path=path)
        import onnxruntime

        _antispoof = onnxruntime.InferenceSession(
            path, providers=["CPUExecutionProvider"])
    return _antispoof


def liveness_score(image, face):
    """
    How likely this is a real face in front of the camera rather than a picture
    of one.

    Returns None when no model is configured — which the caller must treat as
    "unknown", never as "fine". A photograph held up to the lens satisfies every
    other check in this file, so silently scoring it 1.0 would be the single
    most misleading thing this module could do.
    """
    session = get_antispoof()
    if session is None:
        return None

    import numpy as np

    size = int(_conf("ANTISPOOF_INPUT", 80))
    x1, y1, x2, y2 = [int(v) for v in face.bbox]
    h, w = image.shape[:2]
    # A little context around the face: most of these models are trained on a
    # loose crop, and the give-away artefacts (a screen bezel, paper edge, moiré)
    # live just outside the face box.
    pad = int(max(x2 - x1, y2 - y1) * float(_conf("ANTISPOOF_PAD", 0.4)))
    crop = image[max(y1 - pad, 0):min(y2 + pad, h), max(x1 - pad, 0):min(x2 + pad, w)]
    if crop.size == 0:
        return None

    from PIL import Image as PILImage

    patch = PILImage.fromarray(crop).resize((size, size))
    array = np.asarray(patch, dtype=np.float32) / 255.0
    array = np.transpose(array, (2, 0, 1))[None, ...]      # NCHW

    inputs = {session.get_inputs()[0].name: array}
    output = session.run(None, inputs)[0][0]
    exp = np.exp(output - np.max(output))
    probabilities = exp / exp.sum()
    return float(probabilities[int(_conf("ANTISPOOF_REAL_INDEX", 1))])


def match_frame(data, embeddings):
    """
    Compare one live frame against a student's stored vectors.

    Returns a verdict dict rather than raising: on the live path a frame that
    cannot be used is completely ordinary — the student blinked, looked away,
    walked under a light — and each one is simply the cue to send another.
    Exceptions are for things that should stop the attempt, and none of these
    should.
    """
    image = decode_image(data)
    faces = get_live_app().get(image)

    if not faces:
        return {"state": "no_face", "hint": "Hold your face in the frame."}
    if len(faces) > 1:
        return {"state": "many_faces",
                "hint": "More than one face in shot — make sure you are alone."}

    face = max(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))
    if float(face.det_score) < float(_conf("LIVE_MIN_DETECT_SCORE", 0.5)):
        return {"state": "unclear", "hint": "Too dark or too blurred — find better light."}
    if float(face.bbox[3] - face.bbox[1]) < float(_conf("LIVE_MIN_FACE_PX", 80)):
        return {"state": "too_far", "hint": "Move a little closer."}

    live = liveness_score(image, face)
    if live is not None and live < float(_conf("ANTISPOOF_MIN", 0.6)):
        # Not phrased as an accusation: a dim room and a real face can land here
        # too, and most people seeing this will not be cheating.
        return {"state": "not_live", "score": round(live, 3),
                "hint": "That does not look like a live face. Hold the camera up "
                        "to yourself rather than to a photo or a screen."}

    vector = [float(v) for v in face.normed_embedding]
    best = max((cosine(vector, stored) for stored in embeddings), default=0.0)
    threshold = float(_conf("MATCH_MIN", 0.42))
    return {
        "state": "matched" if best >= threshold else "no_match",
        "score": round(best, 3),
        "threshold": threshold,
        "liveness": round(live, 3) if live is not None else None,
        "hint": "" if best >= threshold else "Look straight at the camera.",
    }
