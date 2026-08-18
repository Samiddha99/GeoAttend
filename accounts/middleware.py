from django.contrib.auth import get_user_model
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone

from core.http import fail, is_ajax

SAFE_PREFIXES = ("/auth/", "/static/", "/media/", "/admin/", "/attendance/mark/")


class ForceProfileCompletionMiddleware:
    """
    A user invited but not yet finished (no usable password / not completed)
    is bounced back to the completion flow instead of roaming the app.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if (
            user is not None
            and user.is_authenticated
            and not user.registration_completed
            and not user.is_superuser
            and not request.path.startswith(SAFE_PREFIXES)
        ):
            return redirect(reverse("accounts:complete_profile"))
        return self.get_response(request)


class ForceFaceEnrolmentMiddleware:
    """
    Hold a student at the capture page until their face is on file.

    A hard gate by choice: no face, no account. That is the strict reading, and
    it is the one that cannot be talked around — but it does mean a student
    whose camera is broken cannot see their own attendance until staff clear
    and re-issue the capture, so support needs to know the reset button exists.

    Runs on the cheap `face_enrolled` flag rather than a join, because this
    fires on every request from every student.
    """

    # /auth/ covers the capture page, its endpoint and logout — a gate that
    # traps the very page it redirects to is a locked door with no handle.
    # /attendance/mark/ is deliberately *not* exempt: an unenrolled student has
    # nothing to verify against, so letting them mark would defeat the point.
    SAFE_PREFIXES = ("/auth/", "/static/", "/media/", "/admin/")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from . import face_service

        user = getattr(request, "user", None)
        if (
            user is not None
            and not request.path.startswith(self.SAFE_PREFIXES)
            and face_service.needs_enrolment(user)
        ):
            if is_ajax(request):
                # An AJAX call must not be answered with a redirect to an HTML
                # page — the jQuery layer would try to parse it as JSON and
                # report a parse error instead of what actually happened.
                return fail(
                    "Please capture your face before using your account.",
                    status=403, code="FACE_REQUIRED",
                    data={"redirect": reverse("accounts:face_capture")},
                )
            return redirect(reverse("accounts:face_capture"))
        return self.get_response(request)


class GuardianChildMiddleware:
    """
    Resolve which child a guardian is looking at, once per request.

    Hung on the user object rather than only on the request so that the
    selectors — which take a `user`, not a `request` — can reach it through
    `accounts.guardians.acting_profile` without every one of them growing a new
    argument.

    Resolved fresh each request on purpose. The session records *which* child
    was chosen; whether that child is still reachable is recomputed from the
    student table, so removing a number from a student record cuts the
    guardian's access on their very next click rather than whenever their
    session happens to expire.

    A guardian whose children have all gone is signed out rather than left on a
    dashboard with nothing in it.
    """

    SAFE_PREFIXES = ("/auth/", "/static/", "/media/", "/admin/")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from .guardians import active_child

        user = getattr(request, "user", None)
        if user is not None and getattr(user, "is_authenticated", False) \
                and getattr(user, "is_guardian", False):
            child = active_child(request)
            user._acting_child = child
            request.guardian_child = child
            if child is None and not request.path.startswith(self.SAFE_PREFIXES):
                if is_ajax(request):
                    return fail(
                        "Your number is no longer linked to a student. Please "
                        "contact the institute.",
                        status=403, code="NO_CHILDREN",
                        data={"redirect": reverse("accounts:guardian_login")},
                    )
                return redirect(reverse("accounts:guardian_logout"))
        return self.get_response(request)


class UniversityFocusMiddleware:
    """
    Resolve which institute a university is looking at, once per request.

    Hung on the user object for the same reason `GuardianChildMiddleware` hangs
    the acting child there: the selectors take a `user`, not a `request`, and
    every scoped query in the project runs through `institutes_for(user)`.
    Putting the answer where that function can already see it makes the filter
    apply to every list, chart and export at once — including ones written
    later, which is the part that matters. The alternative, adding an
    `institute` argument to a dozen selectors and every caller, has one chance
    to be forgotten per call site and fails silently when it is.

    The session stores the *choice*; whether it is still reachable is
    recomputed here on every request, so an institute that leaves this
    university's reach stops being focusable on the next click rather than
    whenever the session happens to expire.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from .scoping import SESSION_INSTITUTE_KEY, institutes_for, set_focus

        user = getattr(request, "user", None)
        if user is not None and getattr(user, "is_authenticated", False) \
                and getattr(user, "is_university", False):
            chosen = request.session.get(SESSION_INSTITUTE_KEY)
            if chosen and not institutes_for(user, focused=False).filter(
                    pk=chosen).exists():
                # Out of reach now — drop it rather than showing an empty
                # dashboard the user cannot explain or clear.
                request.session.pop(SESSION_INSTITUTE_KEY, None)
                chosen = None
            set_focus(user, chosen)
        return self.get_response(request)


class ActivityTrackingMiddleware:
    """Stamp `last_seen` at most once a minute — cheap presence tracking."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            now = timezone.now()
            if not user.last_seen or (now - user.last_seen).total_seconds() > 60:
                # `request.user` is a SimpleLazyObject — go through the real model.
                get_user_model().objects.filter(pk=user.pk).update(last_seen=now)
        return response
