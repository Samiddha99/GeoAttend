from django.contrib.auth.base_user import BaseUserManager

from core.utils import normalise_email


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create(self, email, password, **extra):
        if not email:
            raise ValueError("An email address is required.")
        email = normalise_email(email)
        user = self.model(email=email, **extra)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra):
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create(email, password, **extra)

    def create_superuser(self, email, password=None, **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        extra.setdefault("is_active", True)
        extra.setdefault("role", self.model.Role.HEAD)
        extra.setdefault("registration_completed", True)
        if extra["is_staff"] is not True or extra["is_superuser"] is not True:
            raise ValueError("Superuser must have is_staff=True and is_superuser=True.")
        return self._create(email, password, **extra)

    # convenience querysets ------------------------------------------------- #
    def teachers(self):
        return self.filter(role=self.model.Role.TEACHER)

    def students(self):
        return self.filter(role=self.model.Role.STUDENT)

    def hods(self):
        return self.filter(role=self.model.Role.HOD)
