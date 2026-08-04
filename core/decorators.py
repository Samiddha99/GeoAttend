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
