"""
Device binding for student accounts.

**What this is, honestly.** There is no hardware attestation here. The signature
is computed *by the browser* (canvas render + screen geometry + timezone +
language + UA) and posted to the server, so the server cannot tell a real phone
from a spoofed one. What it gives you is *trust on first use*: whichever device
a student first uses becomes "their" device, and every later device is refused
until staff release the binding.

That is genuinely useful against the common case — a student handing their login
to a friend so the friend can mark them present — because the friend's phone
produces a different signature. It is **not** proof of identity, and a student
comfortable with browser dev tools can forge the value. Treat it as a deterrent
layered under the geo-fence, which is the control that actually requires a body
in the room.

Two places consult the binding:

* :func:`login_gate` — refuses a *sign-in* from an unrecognised device
  (``ATTENDANCE["ENFORCE_LOGIN_DEVICE_LOCK"]``).
* ``attendance.services.mark_attendance`` — refuses a *mark*
  (``ATTENDANCE["ENFORCE_DEVICE_LOCK"]``).

Both compare the same `User.device_id`, so a student has exactly one device.
Staff release it from Manage → Students; see `accounts.services.unlink_device`.
"""
from django.conf import settings

from core.utils import device_fingerprint

BLOCK_MESSAGE = (
    "This account is registered to a different device. For security, {name} can only "
    "be used from the device it was first signed in on. If you have lost or changed "
    "your phone, ask your teacher or department office to unlink it — it takes them "
    "a few seconds."
)


def fingerprint_for(request, client_hash=""):
    """The signature for the device making this request."""
    return device_fingerprint(request, client_hash or request.POST.get("device_hash", ""))


def is_locked_role(user):
    """
    Only students are device-locked.

    Staff routinely work from a desktop, a laptop and a phone; binding them would
    lock a HoD out of their own institute for no security gain, since staff never
    mark their own attendance.
    """
    return user.is_student


def login_gate(user, request, client_hash=""):
    """
    Decide whether this sign-in may proceed.

    Returns ``(fingerprint, error)``. ``error`` is None when the login is allowed;
    the caller binds the fingerprint afterwards via ``user.bind_device()``, which
    is a no-op once a device is already recorded.
    """
    fingerprint = fingerprint_for(request, client_hash)

    if not settings.ATTENDANCE["ENFORCE_LOGIN_DEVICE_LOCK"]:
        return fingerprint, None
    if not is_locked_role(user):
        return fingerprint, None
    if not user.device_id:
        return fingerprint, None            # first use — this device becomes theirs
    if fingerprint == user.device_id:
        return fingerprint, None

    return fingerprint, BLOCK_MESSAGE.format(name=settings.SITE_NAME)
