from django.apps import AppConfig


class DashboardConfig(AppConfig):
    default_auto_field = "django_mongodb_backend.fields.ObjectIdAutoField"
    name = "dashboard"
    verbose_name = "Dashboard & Reports"
