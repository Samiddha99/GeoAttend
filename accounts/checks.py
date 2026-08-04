"""
Deploy-time checks for the face pipeline.

These run as part of `manage.py check`, which `migrate` runs too — so on Fly
they fire in the release step, before any machine takes traffic. That is the
point: a misconfigured liveness model used to surface as a student standing in
a classroom watching a camera that would never accept them.
"""
import os

from django.conf import settings
from django.core.checks import Error, Warning, register


@register()
def face_configuration(app_configs, **kwargs):
    """Would live face marking actually work as configured?"""
    conf = getattr(settings, "FACE", {})
    problems = []

    live_on = conf.get("ENABLED", True) and conf.get("LIVE_ENABLED", True)
    if not live_on:
        # Nothing below matters — students fall back to the geo-only flow.
        return problems

    if conf.get("ANTISPOOF_REQUIRED", True):
        path = conf.get("ANTISPOOF_MODEL") or ""
        if not path:
            problems.append(Warning(
                "Live face marking is on but no liveness model is configured, "
                "so every attempt will be refused.",
                hint="Set FACE_ANTISPOOF_MODEL to an ONNX file, or set "
                     "FACE_LIVE_ENABLED=False until you have one.",
                id="face.W001",
            ))
        elif not os.path.exists(path):
            # An Error, not a Warning: this configuration is broken rather than
            # incomplete. Someone believed they had switched liveness on.
            problems.append(Error(
                f"FACE_ANTISPOOF_MODEL points at {path}, which does not exist.",
                hint="Copy the model into the image at that path, correct the "
                     "setting, or set FACE_LIVE_ENABLED=False.",
                id="face.E001",
            ))

    if conf.get("MATCH_MIN", 0.42) < 0.3:
        problems.append(Warning(
            f"FACE_MATCH_MIN is {conf.get('MATCH_MIN')}, low enough that "
            "different people are likely to match each other.",
            hint="Measure genuine and impostor pairs on your own students "
                 "before lowering this.",
            id="face.W002",
        ))
    return problems
