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
