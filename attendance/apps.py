from django.apps import AppConfig


class AttendanceConfig(AppConfig):
    default_auto_field = "django_mongodb_backend.fields.ObjectIdAutoField"
    name = "attendance"
    verbose_name = "Attendance"
