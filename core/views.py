from django.http import HttpResponse
from django.shortcuts import redirect, render


def landing(request):
    return render(request, "core/landing.html")


def health(request):
    """
    Liveness probe for the platform's health check.

    Deliberately does nothing: no database, no template, no session. A probe
    that touches Mongo turns a slow query into a restart loop, which is a far
    worse outage than the one it was meant to catch. It answers "is this
    process serving HTTP", and nothing more.
    """
    return HttpResponse("ok", content_type="text/plain")


def error_403(request, exception=None):
    return render(request, "errors/403.html", status=403)


def error_404(request, exception=None):
    return render(request, "errors/404.html", status=404)


def error_500(request):
    return render(request, "errors/500.html", status=500)
