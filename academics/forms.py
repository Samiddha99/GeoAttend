from django import forms
from django.core.exceptions import ValidationError

from accounts.forms import BootstrapMixin
from core.utils import normalise_email, parse_batch_label

from .models import (
    Batch,
    Department,
    Subject,
    UniversityBatch,
    UniversityDepartment,
    UniversitySubject,
)


class UniversityDepartmentForm(BootstrapMixin, forms.ModelForm):
    """
    A department the university publishes, for one of its disciplines.

    The discipline choices are narrowed to what this university actually grants
    affiliation in — publishing into a discipline nobody can adopt from would
    create a row that exists for nobody. Narrowed on the form and not only in
    the template, because the template decides what is *offered* and a posted
    value still has to be refused.
    """

    class Meta:
        model = UniversityDepartment
        fields = ["discipline", "name", "code", "status"]
        help_texts = {
            "code": "How institutes' copies are matched to this entry, so it "
                    "cannot change once anybody is running it.",
        }

    def __init__(self, *args, disciplines=None, **kwargs):
        super().__init__(*args, **kwargs)
        if disciplines is not None:
            self.fields["discipline"].choices = [("", "Choose…")] + [
                (d["value"], d["label"]) for d in disciplines]
        self.fields["discipline"].required = True

    def clean_code(self):
        from django.utils.text import slugify

        return slugify(self.cleaned_data["code"]).upper().replace("-", "")[:20]


class UniversityBatchForm(BootstrapMixin, forms.ModelForm):
    """
    A cohort the university publishes under one of its own departments.

    `start_year` and `end_year` are derived from the label rather than asked
    for twice — "2022-26" already says both, and two fields that can disagree
    with a third are two chances to be wrong.
    """

    class Meta:
        model = UniversityBatch
        fields = ["department", "label", "status"]
        help_texts = {
            "label": "Format 2022-26. Institutes' copies are matched on it, so "
                     "it cannot change once anybody is running it.",
        }

    def __init__(self, *args, departments=None, **kwargs):
        super().__init__(*args, **kwargs)
        if departments is not None:
            self.fields["department"].queryset = departments

    def clean_label(self):
        from core.utils import parse_batch_label

        label = (self.cleaned_data["label"] or "").strip()
        if parse_batch_label(label) is None:
            raise ValidationError("Use the format 2022-26.")
        return label

    def clean(self):
        cleaned = super().clean()
        from core.utils import parse_batch_label

        parsed = parse_batch_label(cleaned.get("label") or "")
        if parsed:
            # `(start, end, normalised)` — the third element is the label in
            # canonical form, so "2022-2026" and "2022-26" store identically
            # and the uniqueness constraint sees them as one.
            cleaned["start_year"], cleaned["end_year"], cleaned["label"] = parsed
        return cleaned

    def save(self, commit=True):
        entry = super().save(commit=False)
        entry.label = self.cleaned_data["label"]
        entry.start_year = self.cleaned_data["start_year"]
        entry.end_year = self.cleaned_data["end_year"]
        if commit:
            entry.save()
        return entry


class HodEmailForm(BootstrapMixin, forms.Form):
    """
    Just the HoD address.

    Used when an institute edits a department its affiliating university
    defines: the name, code and discipline are not its to change, so they are
    not validated either. A `DepartmentForm` there would refuse the request over
    a discipline the institute never chose and cannot choose — see
    `academics.views.api_department_save`.
    """

    hod_email = forms.EmailField(
        label="HoD email", required=False,
        help_text="An invitation link is emailed to this address.")

    def clean_hod_email(self):
        return normalise_email(self.cleaned_data.get("hod_email", ""))


class DepartmentForm(BootstrapMixin, forms.ModelForm):
    hod_email = forms.EmailField(
        label="HoD email", required=False,
        help_text="An invitation link is emailed to this address.",
    )

    class Meta:
        model = Department
        fields = ["name", "code", "discipline", "is_active"]
        labels = {"is_active": "Active"}

    def __init__(self, *args, user=None, institute=None, **kwargs):
        """
        Narrow the discipline choices to the ones this actor governs.

        Done on the form, not only in the template, because the template only
        decides what is *offered*. A posted discipline the institute does not
        hold autonomously has to be refused, and a ModelChoice-style narrowing
        is the one place that cannot be forgotten.

        The blank option is gone: a department with no discipline is governed by
        nobody, which is right for the rows that predate the column and wrong as
        something new to create. Editing such a row therefore asks for one —
        which is the only moment anyone knows the answer.
        """
        super().__init__(*args, **kwargs)
        field = self.fields["discipline"]
        field.required = True
        field.help_text = ("Which of your disciplines this department sits in. "
                           "Only the ones you hold autonomously are listed — "
                           "the rest are your affiliating university's to "
                           "define.")
        if user is not None:
            from .curriculum import selectable_disciplines

            allowed = selectable_disciplines(user, institute)
            field.choices = [("", "Choose…")] + [(d["value"], d["label"])
                                                 for d in allowed]
            if not allowed:
                field.help_text = (
                    "You have no autonomous discipline, so there is no "
                    "department you can create here. Add one under Profile & "
                    "security, or ask your affiliating university.")

    def clean_hod_email(self):
        return normalise_email(self.cleaned_data.get("hod_email", ""))


class UniversitySubjectForm(BootstrapMixin, forms.ModelForm):
    """
    A paper the university publishes, under one department and semester.

    The department queryset is narrowed by the caller to this university's own
    catalogue, so the form cannot be posted into somebody else's — the check
    lives here rather than in the view because a dropdown is not a permission.
    """

    class Meta:
        model = UniversitySubject
        fields = ["department", "semester", "code", "name", "degree",
                  "subject_type", "credits", "status"]

    def __init__(self, *args, departments=None, **kwargs):
        super().__init__(*args, **kwargs)
        if departments is not None:
            self.fields["department"].queryset = departments
        # Same reasoning as SubjectForm: both carry a model default so old rows
        # stay valid, and both are required here so the person choosing sees
        # what they are choosing.
        for name in ("degree", "subject_type"):
            field = self.fields[name]
            field.required = True
            field.choices = [choice for choice in field.choices if choice[0]]

    def clean_code(self):
        return (self.cleaned_data["code"] or "").strip().upper()


class SubjectForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = Subject
        fields = ["code", "name", "degree", "subject_type", "semester",
                  "credits", "is_active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Both carry a model default (Bachelor, Theory) so that existing rows
        # stay valid, and both are required here so a person filling the form
        # chooses rather than inheriting a default they never saw. Required on
        # the form, not on the column — and the blank option stripped in case a
        # future `blank=True` puts one back.
        for name in ("degree", "subject_type"):
            field = self.fields[name]
            field.required = True
            field.choices = [choice for choice in field.choices if choice[0]]


class BatchForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = Batch
        fields = ["label", "is_active"]

    def clean_label(self):
        parsed = parse_batch_label(self.cleaned_data["label"])
        if parsed is None:
            raise forms.ValidationError("Use the format 2022-26.")
        start, end, label = parsed
        self.cleaned_data["start_year"] = start
        self.cleaned_data["end_year"] = end
        return label

    def save(self, commit=True):
        obj = super().save(commit=False)
        obj.start_year = self.cleaned_data["start_year"]
        obj.end_year = self.cleaned_data["end_year"]
        if commit:
            obj.save()
        return obj


class TeacherInviteForm(BootstrapMixin, forms.Form):
    """
    Inviting a teacher.

    `full_name` is required here although it is optional elsewhere: the PAN
    check matches the number against a name and a date of birth, so an invite
    with no name has nothing to check. It was optional before because an
    invitation could carry just an address and let the person fill the rest in
    themselves — that is no longer possible for a teacher.
    """

    email = forms.EmailField(label="Teacher email")
    full_name = forms.CharField(label="Full name (as on the PAN)",
                                max_length=150)
    phone = forms.CharField(label="Mobile", max_length=20, required=False)
    pan_number = forms.CharField(
        label="PAN", max_length=10,
        help_text="Fixed once saved. One teacher may run at one institute at "
                  "a time, and this is how that is checked.")
    date_of_birth = forms.DateField(
        label="Date of birth", widget=forms.DateInput(attrs={"type": "date"}),
        help_text="Fixed once saved. Checked against the PAN.")

    def clean_email(self):
        return normalise_email(self.cleaned_data["email"])

    def clean_pan_number(self):
        """
        Shape only. Availability and the KYC call live in `accounts.pan`,
        because the edit path needs both and a form cannot be asked from there.
        """
        from accounts.pan import PanError, check_format

        try:
            return check_format(self.cleaned_data["pan_number"])
        except PanError as exc:
            raise forms.ValidationError(str(exc)) from exc

    def clean_date_of_birth(self):
        from django.utils import timezone

        dob = self.cleaned_data["date_of_birth"]
        today = timezone.now().date()
        if dob >= today:
            raise forms.ValidationError("That date is in the future.")
        # Eighteen is the floor for holding a PAN at all, and a hundred catches
        # the year typed as 1925 when 1985 was meant — the two mistakes a date
        # field actually collects.
        age = today.year - dob.year - (
            (today.month, today.day) < (dob.month, dob.day))
        if age < 18:
            raise forms.ValidationError("A teacher cannot be under eighteen. "
                                        "Check the year.")
        if age > 100:
            raise forms.ValidationError("Check the year — that is over a "
                                        "hundred years ago.")
        return dob


class StudentEditForm(BootstrapMixin, forms.Form):
    full_name = forms.CharField(max_length=150)
    mobile = forms.CharField(max_length=20, required=False)
    class_roll = forms.CharField(max_length=40, required=True)
    exam_roll = forms.CharField(max_length=40, required=False)
    batch_id = forms.IntegerField()
    guardian_name = forms.CharField(max_length=150, required=False)
    guardian_mobile = forms.CharField(
        max_length=20, label="Guardian mobile (WhatsApp)",
        help_text="Receives low-attendance alerts.",
    )
    guardian_email = forms.EmailField(required=False)

    def clean_mobile(self):
        from .importer import clean_phone

        raw = self.cleaned_data.get("mobile", "")
        if not raw:
            return ""
        cleaned, error = clean_phone(raw)
        if error:
            raise forms.ValidationError(f"That mobile number {error}.")
        return cleaned

    def clean_guardian_mobile(self):
        from .importer import clean_phone

        cleaned, error = clean_phone(self.cleaned_data.get("guardian_mobile", ""))
        if error:
            raise forms.ValidationError(f"The guardian mobile {error}.")
        return cleaned
