from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from core import views as core_views

from django.urls import register_converter
from core.converters import ObjectIdConverter
register_converter(ObjectIdConverter, "oid")

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", core_views.landing, name="landing"),
    # Named in fly.toml's [[http_service.checks]]; without it the platform
    # health check gets a 404 and keeps replacing healthy machines.
    path("health/", core_views.health, name="health"),
    path("auth/", include("accounts.urls")),
    path("app/", include("dashboard.urls")),
    path("manage/", include("academics.urls")),
    path("attendance/", include("attendance.urls")),
    path("alerts/", include("notifications.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.BASE_DIR / "static")

handler403 = "core.views.error_403"
handler404 = "core.views.error_404"
handler500 = "core.views.error_500"
