from django.apps import AppConfig


class FeedbackConfig(AppConfig):
    default_auto_field = "django_mongodb_backend.fields.ObjectIdAutoField"
    name = "feedback"
    verbose_name = "Class feedback"
