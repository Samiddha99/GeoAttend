from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.utils.text import slugify

from core.utils import normalise_email

from .models import Institute

User = get_user_model()


class BootstrapMixin:
    """Adds Bootstrap classes + placeholders to every widget."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            widget = field.widget
            css = "form-control"
            if isinstance(widget, forms.Select):
                css = "form-select"
            elif isinstance(widget, forms.CheckboxInput):
                css = "form-check-input"
            widget.attrs["class"] = (widget.attrs.get("class", "") + " " + css).strip()
            widget.attrs.setdefault("placeholder", field.label or name.replace("_", " ").title())


class PasswordPairMixin:
    password1 = forms.CharField(
        label="Password", widget=forms.PasswordInput, min_length=8,
        help_text="At least 8 characters, not entirely numeric.",
    )
    password2 = forms.CharField(label="Confirm password", widget=forms.PasswordInput)

    def clean_password1(self):
        pwd = self.cleaned_data["password1"]
        validate_password(pwd)
        return pwd

    def clean(self):
        data = super().clean()
        if data.get("password1") and data.get("password2") and data["password1"] != data["password2"]:
            self.add_error("password2", "The two passwords do not match.")
        return data


# --------------------------------------------------------------------------- #
#  Step 1 — Head of Institute creates the institute
# --------------------------------------------------------------------------- #
class InstituteSignupForm(BootstrapMixin, PasswordPairMixin, forms.Form):
    institute_name = forms.CharField(label="Institute name", max_length=200)
    institute_code = forms.CharField(
        label="Institute code", max_length=30,
        help_text="Short unique code, e.g. NIT-DGP",
    )
    institute_email = forms.EmailField(label="Official institute email")
    phone = forms.CharField(label="Institute phone", max_length=20, required=False)
    website = forms.URLField(label="Website", required=False)
    address = forms.CharField(label="Address", widget=forms.Textarea(attrs={"rows": 2}), required=False)

    head_name = forms.CharField(label="Your full name", max_length=150)
    head_email = forms.EmailField(label="Your (head) email — this is your login")
    head_phone = forms.CharField(label="Your phone", max_length=20, required=False)

    password1 = PasswordPairMixin.password1
    password2 = PasswordPairMixin.password2

    def clean_institute_code(self):
        code = slugify(self.cleaned_data["institute_code"]).upper()
        if Institute.objects.filter(code__iexact=code).exists():
            raise ValidationError("An institute with this code already exists.")
        return code

    def clean_institute_email(self):
        email = normalise_email(self.cleaned_data["institute_email"])
        if Institute.objects.filter(email__iexact=email).exists():
            raise ValidationError("An institute is already registered with this email.")
        return email

    def clean_head_email(self):
        email = normalise_email(self.cleaned_data["head_email"])
        if User.objects.filter(email__iexact=email).exists():
            raise ValidationError("An account with this email already exists. Try signing in.")
        return email


class OTPForm(BootstrapMixin, forms.Form):
    email = forms.EmailField(widget=forms.HiddenInput)
    code = forms.CharField(label="6-digit code", min_length=6, max_length=6)

    def clean_code(self):
        code = self.cleaned_data["code"].strip()
        if not code.isdigit():
            raise ValidationError("The code must be 6 digits.")
        return code


# --------------------------------------------------------------------------- #
#  Invitation acceptance (HoD / Teacher / Student)
# --------------------------------------------------------------------------- #
class InviteAcceptForm(BootstrapMixin, PasswordPairMixin, forms.Form):
    full_name = forms.CharField(label="Full name", max_length=150)
    phone = forms.CharField(label="Mobile number", max_length=20, required=False)
    password1 = PasswordPairMixin.password1
    password2 = PasswordPairMixin.password2


class LoginForm(BootstrapMixin, forms.Form):
    email = forms.EmailField(label="Email address")
    password = forms.CharField(label="Password", widget=forms.PasswordInput)
    remember = forms.BooleanField(label="Keep me signed in", required=False)


class ForgotPasswordForm(BootstrapMixin, forms.Form):
    email = forms.EmailField(label="Registered email")


class ResetPasswordForm(BootstrapMixin, PasswordPairMixin, forms.Form):
    email = forms.EmailField(widget=forms.HiddenInput)
    code = forms.CharField(min_length=6, max_length=6)
    password1 = PasswordPairMixin.password1
    password2 = PasswordPairMixin.password2


class ProfileForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = User
        fields = ["full_name", "phone"]


class ChangePasswordForm(BootstrapMixin, PasswordPairMixin, forms.Form):
    current_password = forms.CharField(label="Current password", widget=forms.PasswordInput)
    password1 = PasswordPairMixin.password1
    password2 = PasswordPairMixin.password2

    def __init__(self, user, *args, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)

    def clean_current_password(self):
        pwd = self.cleaned_data["current_password"]
        if not self.user.check_password(pwd):
            raise ValidationError("Your current password is incorrect.")
        return pwd
