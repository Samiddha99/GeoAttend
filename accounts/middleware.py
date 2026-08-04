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
