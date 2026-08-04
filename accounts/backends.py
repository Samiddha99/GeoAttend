from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend

from core.utils import normalise_email

UserModel = get_user_model()


class EmailBackend(ModelBackend):
    """Authenticate with email + password (case-insensitive email)."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        email = normalise_email(username or kwargs.get("email") or "")
        if not email or password is None:
            return None
        try:
            user = UserModel.objects.get(email=email)
        except UserModel.DoesNotExist:
            UserModel().set_password(password)  # equalise timing
            return None
        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None
