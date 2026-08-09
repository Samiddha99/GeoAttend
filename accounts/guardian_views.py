"""
Guardian sign-in endpoints.

Kept out of accounts/views.py because the rules are different enough that
mixing them invites a mistake: there is no password, no invitation, no device
binding, and the only credential is a code sent to a number we read out of a
student record.

**On not confirming whether a number is known.** `api_guardian_start` returns
the same response whether or not the number matches a student. Answering
honestly would turn the endpoint into a lookup service: try numbers, learn
which families attend this institute. The cost is a guardian who mistypes their
number waits for a message that never comes — which is why the response says so
in as many words.
"""
import logging

from django.conf import settings
from django.contrib.auth import login, logout
from django.core.exceptions import ValidationError
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

from core.http import client_ip, fail, ok
from notifications.whatsapp import normalise_msisdn

from .guardians import (
    account_for_number,
    children_for_number,
    choose_child,
    send_login_code,
)
from .models import ActivityLog, PhoneOTP

log = logging.getLogger("geoattend")

OTP_SESSION_KEY = "guardian_otp_id"

# Said whether or not the number is on file. See the module docstring.
SENT_MESSAGE = (
    "If that number is on a student's record, a sign-in code is on its way to "
    "it on WhatsApp. It expires in {ttl} minutes."
)


@ensure_csrf_cookie
def guardian_login_page(request):
    return render(request, "accounts/guardian_login.html", {
        "ttl": getattr(settings, "PHONE_OTP_TTL_MINUTES", settings.OTP_TTL_MINUTES),
    })


def pending_otp(request):
    """
    The code this browser is part-way through, or None.

    Same care as the signup OTP: a tampered or stale session can hold something
    that is not a valid id, and ObjectIdAutoField raises rather than simply not
    matching. That should read as "start again", not a 500.
    """
    otp_id = request.session.get(OTP_SESSION_KEY)
    if not otp_id:
        return None
    try:
        return PhoneOTP.objects.filter(
            id=otp_id, purpose=PhoneOTP.Purpose.GUARDIAN_LOGIN).first()
    except (ValidationError, ValueError, TypeError):
        request.session.pop(OTP_SESSION_KEY, None)
        return None


@require_POST
def api_guardian_start(request):
    """Send a code — or appear to, if the number is not on any record."""
    ttl = getattr(settings, "PHONE_OTP_TTL_MINUTES", settings.OTP_TTL_MINUTES)
    number, error = normalise_msisdn(request.POST.get("mobile", ""))
    if error:
        # A number that is not a number at all is a typo, not an enumeration
        # attempt, and saying so saves a guardian waiting for nothing.
        return fail("Please enter a valid mobile number, including the country "
                    "code if you are outside India.")

    quiet = ok({"ttl": ttl, "mobile": number}, message=SENT_MESSAGE.format(ttl=ttl))
    if not children_for_number(number).exists():
        ActivityLog.objects.create(
            action="GUARDIAN_OTP_UNKNOWN", detail=number,
            ip=client_ip(request) or None)
        return quiet

    otp, code = PhoneOTP.issue(number)
    request.session[OTP_SESSION_KEY] = str(otp.id)
    result = send_login_code(number, code)
    if not result.ok:
        # Worth being honest about: the number *is* on file, so this is our
        # failure, not theirs, and silence would leave them retrying forever.
        log.error("guardian OTP send failed for %s: %s", number, result.error)
        return fail("We could not send the code just now. Please try again in "
                    "a moment.", status=502, code="SEND_FAILED")

    ActivityLog.objects.create(action="GUARDIAN_OTP_SENT", detail=number,
                               ip=client_ip(request) or None)
    data = {"ttl": ttl, "mobile": number}
    if settings.DEBUG:
        data["debug_code"] = code
    return ok(data, message=SENT_MESSAGE.format(ttl=ttl))


@require_POST
def api_guardian_resend(request):
    otp = pending_otp(request)
    if otp is None:
        return fail("Your sign-in session expired. Please start again.", status=410)
    code, error = otp.resend()
    if error:
        return fail(error, status=429, code="RATE_LIMITED")
    result = send_login_code(otp.mobile, code)
    if not result.ok:
        log.error("guardian OTP resend failed for %s: %s", otp.mobile, result.error)
        return fail("We could not send the code just now. Please try again in "
                    "a moment.", status=502, code="SEND_FAILED")
    data = {"sends_left": max(otp.max_sends - otp.sends, 0)}
    if settings.DEBUG:
        data["debug_code"] = code
    return ok(data, message="A new code is on its way.")


@require_POST
def api_guardian_verify(request):
    otp = pending_otp(request)
    if otp is None:
        return fail("Your sign-in session expired. Please start again.", status=410)

    good, message = otp.verify(request.POST.get("code", ""))
    if not good:
        ActivityLog.objects.create(action="GUARDIAN_OTP_FAILED", detail=otp.mobile,
                                   ip=client_ip(request) or None)
        return fail(message)

    # Re-read the children *after* verifying rather than trusting the list from
    # when the code was sent. A student could have been deactivated in between,
    # and the code must not be a key to a door that has since closed.
    children = list(children_for_number(otp.mobile))
    if not children:
        return fail("That number is no longer linked to a student. Please "
                    "contact the institute.", status=403, code="NO_CHILDREN")

    first = children[0]
    user = account_for_number(
        otp.mobile,
        institute=first.department.institute,
        name=first.guardian_name or "Guardian",
    )
    if not user.is_active:
        return fail("This guardian account has been deactivated. Please "
                    "contact the institute.", status=403)

    # The backend has to be named, because authenticate() was never called —
    # there is no password to authenticate against.
    #
    # It must be a backend that is actually *configured*. Django writes this
    # path into the session, and on the next request `auth.get_user()` ignores
    # any path not listed in AUTHENTICATION_BACKENDS and hands back an
    # AnonymousUser instead. Naming a real class that happens not to be
    # installed — ModelBackend, say, when the project runs EmailBackend — gets
    # you a sign-in that appears to work and a bounce to the login page on the
    # very next click.
    login(request, user, backend=settings.AUTHENTICATION_BACKENDS[0])
    request.session.pop(OTP_SESSION_KEY, None)
    choose_child(request, first.id)
    ActivityLog.log(request, action="GUARDIAN_LOGIN",
                    detail=f"{otp.mobile} · {len(children)} student(s)")
    return ok({"redirect": reverse("dashboard:home"),
               "children": len(children)},
              message=f"Signed in. Showing {first.name}.")


@require_POST
def api_guardian_switch_child(request):
    """Point the session at a different child."""
    user = request.user
    if not user.is_authenticated or not user.is_guardian:
        return fail("Not signed in as a guardian.", status=403)
    if not choose_child(request, request.POST.get("student") or ""):
        # Either not their child or no longer reachable. One message for both:
        # distinguishing them tells a caller whether a student id exists.
        return fail("That student is not linked to your number.", status=403)
    return ok({"redirect": reverse("dashboard:home")})


def guardian_logout(request):
    ActivityLog.log(request, action="GUARDIAN_LOGOUT")
    logout(request)
    return redirect("accounts:guardian_login")
