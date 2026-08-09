"""Role gates used by every view in the project."""
from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied

from core.http import fail, is_ajax


def _deny(request, message):
    if is_ajax(request):
        return fail(message, status=403)
    raise PermissionDenied(message)


def role_required(*roles):
    """Allow only the listed roles.  Works for both AJAX and normal requests."""

    def decorator(view):
        @wraps(view)
        def _wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                if is_ajax(request):
                    return fail("Please sign in to continue.", status=401, login_required=True)
                return redirect_to_login(request.get_full_path())
            if roles and request.user.role not in roles:
                return _deny(request, "This area is restricted to: %s." % ", ".join(roles))
            return view(request, *args, **kwargs)

        return _wrapped

    return decorator


GUARDIAN_REFUSAL = (
    "Guardian accounts can view a student's record but cannot change anything."
)


def guardian_readonly(view):
    """
    Refuse a guardian anything that is not a plain read.

    Every student *write* endpoint is already closed to guardians by
    `role_required(STUDENT)` — GUARDIAN is simply not in the list. This is for
    the endpoints deliberately widened to `role_required(STUDENT, GUARDIAN)` so
    a guardian can see the screen: it makes the read-only promise a property of
    the endpoint rather than of whoever remembered to keep the view free of
    side effects.

    Method-based rather than a per-view flag because the distinction the app
    already makes is GET reads, POST writes. A GET with side effects would slip
    through, but there is not one here and one would be a bug on its own terms.
    """

    @wraps(view)
    def _wrapped(request, *args, **kwargs):
        user = request.user
        if (getattr(user, "is_authenticated", False)
                and getattr(user, "is_guardian", False)
                and request.method not in ("GET", "HEAD", "OPTIONS")):
            return _deny(request, GUARDIAN_REFUSAL)
        return view(request, *args, **kwargs)

    return _wrapped


def deny_guardian_page(view):
    """
    Keep guardians off a page entirely, not just off its buttons.

    For screens whose whole subject is something a guardian does not have — a
    password, a bound device, a face enrolment. `guardian_readonly` would let
    the GET through and only refuse the POST, which means rendering a settings
    page where every control is inert. Better to say no at the door.
    """

    @wraps(view)
    def _wrapped(request, *args, **kwargs):
        user = request.user
        if getattr(user, "is_authenticated", False) and getattr(user, "is_guardian", False):
            if is_ajax(request):
                return fail(GUARDIAN_REFUSAL, status=403)
            from django.shortcuts import redirect

            return redirect("dashboard:home")
        return view(request, *args, **kwargs)

    return _wrapped


def ajax_login_required(view):
    @wraps(view)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            if is_ajax(request):
                return fail("Please sign in to continue.", status=401, login_required=True)
            from django.contrib.auth.views import redirect_to_login as _r

            return _r(request.get_full_path())
        return view(request, *args, **kwargs)

    return _wrapped
