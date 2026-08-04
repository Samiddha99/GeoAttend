from django.contrib.auth import get_user_model
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone

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
