import logging

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.utils.cache import add_never_cache_headers

from core.http import fail, is_ajax

log = logging.getLogger("geoattend")


class NoStoreMiddleware:
    """
    Keep pages behind the login out of the browser's cache.

    Django sends no Cache-Control of its own, so a browser falls back to
    heuristic caching and is free to keep a copy of any page it has seen. The
    visible symptom is that after signing out, asking for the sign-in page is
    answered from that cache — with the previous user's dashboard, or with the
    redirect the sign-in page issued while they were still signed in. Nothing
    is wrong with the session: the request never reaches the server.

    On a shared or lab machine that is not merely stale, it is one student
    reading another's attendance.

    Static and media files are deliberately left alone. WhiteNoise fingerprints
    static filenames, so those *should* be cached hard and forever.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self._exempt = tuple(
            p for p in (settings.STATIC_URL, getattr(settings, "MEDIA_URL", None)) if p
        )

    def __call__(self, request):
        response = self.get_response(request)
        if request.path.startswith(self._exempt):
            return response
        # Sets no-cache, no-store, must-revalidate, private and an Expires in
        # the past. no-store is the one that matters: it also keeps the page
        # out of the back/forward cache, so the Back button cannot resurrect
        # a signed-in screen.
        add_never_cache_headers(response)
        return response


class AjaxExceptionMiddleware:
    """
    Turn unhandled exceptions raised during an AJAX request into a predictable
    JSON envelope, so the jQuery layer never has to parse an HTML error page.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exception):
        if not is_ajax(request):
            return None
        if isinstance(exception, Http404):
            return fail("Not found.", status=404)
        if isinstance(exception, PermissionDenied):
            return fail("You do not have permission to do that.", status=403)
        if isinstance(exception, ValidationError):
            return fail("; ".join(exception.messages), status=400)
        log.exception("Unhandled AJAX exception: %s", exception)
        return fail("Unexpected server error. Please try again.", status=500)
