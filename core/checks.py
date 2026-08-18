"""
Deployment checks for the geo-fence.

Testing on a laptop is awkward: desktops have no GPS chip, so the browser
geolocates from WiFi/IP and reports accuracies in the hundreds of metres. The
practical workaround is to raise ATTENDANCE_MAX_GPS_ACCURACY_M locally — but if
that value follows the code into production it quietly disables the feature the
whole product is built on, and nothing visible breaks. These checks make that
mistake loud instead of silent.

Run automatically by `manage.py check --deploy`, and by `runserver`.
"""
from django.conf import settings
from django.core.checks import Warning, register


W001 = "geoattend.W001"
W002 = "geoattend.W002"
W003 = "geoattend.W003"


@register("geoattend")
def check_geofence_accuracy(app_configs, **kwargs):
    """The accuracy bar has to be tight relative to the fence it guards."""
    conf = getattr(settings, "ATTENDANCE", {}) or {}
    accuracy = conf.get("MAX_GPS_ACCURACY_M")
    radius = conf.get("DEFAULT_RADIUS_M")
    if not accuracy or not radius:
        return []

    issues = []
    # A ±200 m fix inside a 50 m fence cannot tell the classroom from the street.
    # Accepting it does not make attendance more accurate, it makes the check
    # meaningless while still looking like it works.
    if accuracy > radius * 2:
        issues.append(Warning(
            f"ATTENDANCE_MAX_GPS_ACCURACY_M is {accuracy} m but the geo-fence "
            f"radius is only {radius} m.",
            hint=(
                "A fix that imprecise cannot distinguish a student in the room "
                "from one outside the building, so the geo-fence stops meaning "
                "anything. This is usually a leftover from testing on a laptop, "
                "which has no GPS. Set ATTENDANCE_MAX_GPS_ACCURACY_M back to "
                f"{radius} or less for real use, and test on a phone."
            ),
            id=W001,
        ))

    if not settings.DEBUG and accuracy > 100:
        issues.append(Warning(
            f"ATTENDANCE_MAX_GPS_ACCURACY_M is {accuracy} m with DEBUG=False.",
            hint=(
                "Values above 100 m are development conveniences. Confirm this "
                "is deliberate before running in production."
            ),
            id=W002,
        ))
    return issues


@register("geoattend")
def check_collected_static_is_current(app_configs, **kwargs):
    """
    Warn when STATIC_ROOT holds an older build than the source tree.

    WhiteNoise answers /static/ before Django's own handler does, and it
    answers from STATIC_ROOT. So a stale collected copy is served in preference
    to the file on disk, and nothing anywhere says so. The symptom is a
    JavaScript function that plainly exists in the file you are editing and
    does not exist in the browser — which reads as a caching bug, a syntax
    error, anything except what it is.

    That is not a hypothetical. A five-week-old `app.js` was served this way,
    missing every helper added since, and the first sign of it was a
    `TypeError` in a page that had been working minutes earlier.

    In DEBUG the settings now serve straight from the source tree, so this
    check is mainly about the deploy: a build that ships without collectstatic
    ships August's assets against today's templates.
    """
    from pathlib import Path

    root = getattr(settings, "STATIC_ROOT", None)
    sources = getattr(settings, "STATICFILES_DIRS", []) or []
    if not root or not Path(root).exists():
        return []          # nothing collected yet; collectstatic will handle it

    def newest(path):
        path = Path(path)
        if not path.exists():
            return 0
        return max((f.stat().st_mtime for f in path.rglob("*") if f.is_file()),
                   default=0)

    collected = newest(root)
    latest_source = max((newest(d) for d in sources), default=0)
    if latest_source <= collected:
        return []

    return [Warning(
        "STATIC_ROOT holds an older build than your static source files.",
        hint=(
            "WhiteNoise serves /static/ from STATIC_ROOT, so browsers are "
            "getting the collected copy, not the files you edited. Run "
            "`manage.py collectstatic`. "
            + ("(In DEBUG this project serves from the source tree instead, so "
               "this is only about what gets deployed.)" if settings.DEBUG else "")
        ),
        id=W003,
    )]
