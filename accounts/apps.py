from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = "django_mongodb_backend.fields.ObjectIdAutoField"
    name = "accounts"
    verbose_name = "Accounts & Identity"

    def ready(self):
        # Registers the face-configuration checks. Imported here rather than at
        # module level so `manage.py check` picks them up without settings
        # being touched at import time.
        from . import checks  # noqa: F401
