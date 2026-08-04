from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django_mongodb_backend.fields.ObjectIdAutoField"
    name = "core"
    verbose_name = "Core"

    def ready(self):
        from . import checks  # noqa: F401  (registers the geo-fence checks)
