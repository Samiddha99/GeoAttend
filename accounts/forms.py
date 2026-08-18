from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.utils.text import slugify

from academics import reference
from core.utils import clean_object_id, normalise_email

from .models import Discipline, Institute, University

User = get_user_model()

# The sentinel an institute posts for "we award our own degrees in this
# discipline". A string rather than an empty value so that "autonomous" and
# "did not answer" stay distinguishable — the difference is a claim versus a
# gap, and the model keeps them apart too.
AUTONOMOUS = "AUTONOMOUS"


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
class PlaceMixin(forms.Form):
    """
    State and district, validated as a pair.

    Separately they are two harmless strings; together they are a claim. A form
    that checked them independently would happily accept "Kerala / Bhopal",
    which is the sort of thing nobody notices until a report is grouped by
    state.
    """

    state = forms.CharField(label="State / Union Territory", max_length=60)
    district = forms.CharField(label="District", max_length=80)

    def clean_state(self):
        state = (self.cleaned_data.get("state") or "").strip()
        if state not in reference.all_states():
            raise ValidationError("Choose a state or union territory from the list.")
        return state

    def clean(self):
        cleaned = super().clean()
        state, district = cleaned.get("state"), (cleaned.get("district") or "").strip()
        # Only complain about the district if the state itself was fine —
        # otherwise a bad state produces two errors for one mistake.
        if state and district and not reference.is_valid_place(state, district):
            self.add_error("district", f"{district} is not a district of {state}.")
        return cleaned


class InstituteSignupForm(BootstrapMixin, PasswordPairMixin, PlaceMixin, forms.Form):
    institute_name = forms.CharField(label="Institute name", max_length=200)
    institute_code = forms.CharField(
        label="Institute code", max_length=30,
        help_text="Short unique code, e.g. NIT-DGP",
    )
    institute_email = forms.EmailField(label="Official institute email")
    phone = forms.CharField(label="Institute phone", max_length=20, required=False)
    website = forms.URLField(label="Website", required=False)
    address = forms.CharField(label="Address", widget=forms.Textarea(attrs={"rows": 2}), required=False)

    disciplines = forms.MultipleChoiceField(
        label="Discipline(s) offered", choices=Discipline.choices,
        widget=forms.CheckboxSelectMultiple)

    head_name = forms.CharField(label="Your full name", max_length=150)
    head_email = forms.EmailField(label="Your (head) email — this is your login")
    head_phone = forms.CharField(label="Your phone", max_length=20, required=False)

    password1 = PasswordPairMixin.password1
    password2 = PasswordPairMixin.password2

    def clean(self):
        """
        Resolve one affiliating body per chosen discipline.

        The affiliation fields are not declared: they are named
        `affiliation_ENGG`, one per discipline, and only the chosen ones are
        read. Declaring six fields and ignoring five would put five spurious
        "this field is required" errors on a form where they mean nothing.

        A body must actually grant affiliation *for that discipline* — posting
        an agriculture university under Engineering is refused rather than
        accepted and quietly filed wrong.
        """
        cleaned = super().clean()
        chosen = cleaned.get("disciplines") or []
        affiliations = {}
        for discipline in chosen:
            raw = (self.data.get(f"affiliation_{discipline}") or "").strip()
            label = dict(Discipline.choices)[discipline]
            if not raw:
                self.add_error("disciplines",
                               f"Choose an affiliating body for {label}, "
                               "or mark it Autonomous.")
                continue
            if raw == AUTONOMOUS:
                affiliations[discipline] = None
                continue
            # A junk id must not reach the query. `pk=""` is not "no match" —
            # ObjectIdAutoField raises on it, which would surface as a 500 on a
            # public signup form rather than a field error.
            university_id = clean_object_id(raw)
            university = University.objects.filter(
                pk=university_id, is_active=True, grants_affiliation=True,
                disciplines__discipline=discipline).first() if university_id else None
            if university is None:
                self.add_error(
                    "disciplines",
                    f"That body does not grant affiliation for {label}.")
                continue
            affiliations[discipline] = university
        cleaned["affiliations"] = affiliations
        return cleaned

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


class UniversitySignupForm(BootstrapMixin, PasswordPairMixin, PlaceMixin,
                           forms.Form):
    """
    A university or examination board registering itself.

    `existing` is how the ~112 shipped bodies get claimed: picking one reuses
    that row rather than creating a near-duplicate. Without it "Anna
    University" and "Anna Univ." would both exist and split their institutes
    between two accounts that cannot see each other.
    """

    existing = forms.CharField(required=False, widget=forms.HiddenInput)
    university_name = forms.CharField(label="University / board name", max_length=200)
    short_name = forms.CharField(label="Short name", max_length=40, required=False,
                                 help_text="e.g. AKTU. Shown where space is tight.")
    university_code = forms.CharField(
        label="Code", max_length=30, help_text="Short unique code, e.g. AKTU")
    university_email = forms.EmailField(label="Official university email")
    phone = forms.CharField(label="Phone", max_length=20, required=False)
    website = forms.URLField(label="Website", required=False)
    address = forms.CharField(label="Address", required=False,
                              widget=forms.Textarea(attrs={"rows": 2}))

    disciplines = forms.MultipleChoiceField(
        label="Discipline(s) you cover", choices=Discipline.choices,
        widget=forms.CheckboxSelectMultiple)
    grants_affiliation = forms.BooleanField(
        label="We affiliate institutes", required=False, initial=True,
        help_text="Institutes will be able to name you as their affiliating "
                  "body when they register. Leave off if you only work with "
                  "institutes you invite yourself.")

    admin_name = forms.CharField(label="Your full name", max_length=150)
    admin_email = forms.EmailField(label="Your email — this is your login")
    admin_phone = forms.CharField(label="Your phone", max_length=20, required=False)

    password1 = PasswordPairMixin.password1
    password2 = PasswordPairMixin.password2

    def _claimed(self):
        """The seeded row being claimed, if any."""
        raw = (self.data.get("existing") or "").strip()
        university_id = clean_object_id(raw) if raw else None
        if not university_id:
            return None
        return University.objects.filter(pk=university_id).first()

    def clean_university_code(self):
        code = slugify(self.cleaned_data["university_code"]).upper()
        clash = University.objects.filter(code__iexact=code)
        claimed = self._claimed()
        if claimed:
            clash = clash.exclude(pk=claimed.pk)
        if clash.exists():
            raise ValidationError("A university with this code already exists.")
        return code

    def clean_university_email(self):
        email = normalise_email(self.cleaned_data["university_email"])
        clash = University.objects.filter(email__iexact=email)
        claimed = self._claimed()
        if claimed:
            clash = clash.exclude(pk=claimed.pk)
        if clash.exists():
            raise ValidationError("A university is already registered with this email.")
        return email

    def clean_university_name(self):
        name = self.cleaned_data["university_name"].strip()
        clash = University.objects.filter(name__iexact=name)
        claimed = self._claimed()
        if claimed:
            clash = clash.exclude(pk=claimed.pk)
        if clash.exists():
            raise ValidationError(
                "That university is already registered. If it is yours, ask "
                "whoever registered it to add you.")
        return name

    def clean_admin_email(self):
        email = normalise_email(self.cleaned_data["admin_email"])
        if User.objects.filter(email__iexact=email).exists():
            raise ValidationError("An account with this email already exists. "
                                  "Try signing in.")
        return email

    def clean(self):
        cleaned = super().clean()
        claimed = self._claimed()
        # A body already claimed cannot be claimed twice — that is a second
        # account for one university, which is the thing `existing` exists to
        # prevent.
        if claimed is not None and claimed.is_claimed:
            raise ValidationError(
                "That university has already been registered.")
        cleaned["existing_university"] = claimed
        return cleaned


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
    """
    What an invited person fills in to activate their account.

    **A teacher is not asked for their name.** It is already on file — their
    institute typed it when inviting them and the KYC check verified it against
    their PAN — and asking again at the one moment nobody is watching would let
    the verified name and the account part company before the account even
    exists. Removed rather than pre-filled and locked, for the same reason as
    `ProfileForm`: a field that is not in the form cannot be posted into it.

    Everybody else types their own, because nothing was verified against it.
    """

    full_name = forms.CharField(label="Full name", max_length=150)
    phone = forms.CharField(label="Mobile number", max_length=20, required=False)
    password1 = PasswordPairMixin.password1
    password2 = PasswordPairMixin.password2

    def __init__(self, *args, role=None, **kwargs):
        super().__init__(*args, **kwargs)
        if role == User.Role.TEACHER:
            self.fields.pop("full_name", None)


class UniversityInstituteInviteForm(BootstrapMixin, PlaceMixin, forms.Form):
    """
    What a university fills in to create an institute and invite its head.

    Only identity and place. Everything else — address, phone, website, the
    head's name and mobile — is asked of the head when they accept, because it
    is theirs to know and the university would only be guessing.

    Affiliation is optional here, and defaults to this university for every
    chosen discipline. A university that does not affiliate can still invite,
    in which case the institute simply has no affiliation rows.
    """

    institute_name = forms.CharField(label="Institute name", max_length=200)
    institute_code = forms.CharField(label="Institute code", max_length=30)
    institute_email = forms.EmailField(label="Official institute email")
    head_email = forms.EmailField(
        label="Head of institute email",
        help_text="The invitation goes here. It becomes their login.")
    disciplines = forms.MultipleChoiceField(
        label="Discipline(s)", choices=Discipline.choices,
        widget=forms.CheckboxSelectMultiple)

    def __init__(self, *args, university=None, **kwargs):
        self.university = university
        super().__init__(*args, **kwargs)

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
        user = User.objects.filter(email__iexact=email).first()
        if user is not None and user.registration_completed:
            raise ValidationError(
                "Somebody already uses this email. Ask them to sign in, or "
                "invite a different address.")
        return email

    def clean(self):
        """
        Attach this university to each discipline it actually covers.

        A university that does not grant affiliation attaches to none of them:
        it is inviting the institute, not affiliating it, and recording an
        affiliation it does not offer would be a claim nobody made.
        """
        cleaned = super().clean()
        chosen = cleaned.get("disciplines") or []
        affiliations = {}
        if self.university is not None and self.university.grants_affiliation:
            covered = set(self.university.disciplines.values_list(
                "discipline", flat=True))
            for discipline in chosen:
                affiliations[discipline] = (
                    self.university.pk if discipline in covered else None)
        else:
            affiliations = {d: None for d in chosen}
        cleaned["affiliations"] = affiliations
        return cleaned


class InstituteInviteAcceptForm(BootstrapMixin, PasswordPairMixin, forms.Form):
    """
    What the head of an *invited* institute may fill in.

    Deliberately narrower than the self-registration form. The university has
    already said what this institute is called and where it is, and letting the
    invitee change either would let an institute walk out from under the
    university that created it. Everything here is the institute's own business:
    how to contact it, who runs it, and a password.

    Note there is no state or district field — not disabled, absent. A disabled
    input is a suggestion; an absent one cannot be posted at all.
    """

    institute_email = forms.EmailField(label="Official institute email")
    phone = forms.CharField(label="Institute phone", max_length=20, required=False)
    website = forms.URLField(label="Website", required=False)
    address = forms.CharField(label="Address", required=False,
                              widget=forms.Textarea(attrs={"rows": 2}))

    full_name = forms.CharField(label="Head of institute — full name", max_length=150)
    phone_head = forms.CharField(label="Head of institute — mobile",
                                 max_length=20, required=False)

    password1 = PasswordPairMixin.password1
    password2 = PasswordPairMixin.password2

    def __init__(self, *args, institute=None, **kwargs):
        self.institute = institute
        super().__init__(*args, **kwargs)

    def clean_institute_email(self):
        email = normalise_email(self.cleaned_data["institute_email"])
        clash = Institute.objects.filter(email__iexact=email)
        if self.institute is not None:
            clash = clash.exclude(pk=self.institute.pk)
        if clash.exists():
            raise ValidationError("An institute is already registered with this email.")
        return email


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
    """
    The two things an account may change about itself.

    **`full_name` is removed entirely for a teacher, not disabled.** A disabled
    field still renders; a *removed* one cannot be written by a ModelForm no
    matter what the request body contains. Marking it read-only in the markup
    would leave the endpoint accepting a posted name, which is the half-fix
    that reads as done and is not — see `accounts.identity.may_edit_own_name`
    for why a teacher's name is not theirs to change.
    """

    class Meta:
        model = User
        fields = ["full_name", "phone"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from .identity import may_edit_own_name

        # `self.instance` is the signed-in user on both paths that build this
        # form, so the rule needs no extra argument threaded through them.
        if not may_edit_own_name(self.instance):
            self.fields.pop("full_name", None)


class InstituteIdentityForm(BootstrapMixin, forms.ModelForm):
    """
    The institute's name and official email.

    Uniqueness is left to the model's own constraints rather than re-checked
    here: two institutes sharing a name is exactly the near-duplicate problem
    the seeded-university list exists to avoid, and the database is the only
    place that can answer it without a race.
    """

    class Meta:
        model = Institute
        fields = ["name", "code", "email", "phone", "website", "address"]
        labels = {"email": "Official institute email"}
        help_texts = {
            "email": "The address on your letterhead. Notifications go to the "
                     "head's login, not here.",
            # Worth saying out loud. The code is what appears in exports and in
            # the Institute column, so changing it after a term's data exists
            # makes old spreadsheets and new ones disagree about the same place.
            "code": "Used in exports and reports. Changing it will not match "
                    "spreadsheets already downloaded under the old one.",
        }


class UniversityIdentityForm(BootstrapMixin, forms.ModelForm):
    """The same for a university's own record."""

    class Meta:
        model = University
        fields = ["name", "short_name", "code", "email", "phone", "website",
                  "address"]
        labels = {"email": "Official university email"}
        help_texts = {
            "code": "Used in exports and reports. Changing it will not match "
                    "spreadsheets already downloaded under the old one.",
        }


class HeadLoginForm(BootstrapMixin, forms.Form):
    """
    Move a head's login to a different address.

    A bare email field rather than a ModelForm on User: the only thing anyone
    may change here is the address, and a ModelForm would put every other field
    on the user one typo away from being written.
    """

    email = forms.EmailField(label="Head's login email")

    def __init__(self, *args, user=None, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        taken = User.objects.filter(email__iexact=email)
        if self.user is not None:
            taken = taken.exclude(pk=self.user.pk)
        if taken.exists():
            raise ValidationError("Another account already uses that address.")
        return email


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
