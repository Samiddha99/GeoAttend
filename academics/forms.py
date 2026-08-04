from django import forms

from accounts.forms import BootstrapMixin
from core.utils import normalise_email, parse_batch_label

from .models import Batch, Department, Subject


class DepartmentForm(BootstrapMixin, forms.ModelForm):
    hod_email = forms.EmailField(
        label="HoD email", required=False,
        help_text="An invitation link is emailed to this address.",
    )

    class Meta:
        model = Department
        fields = ["name", "code"]

    def clean_hod_email(self):
        return normalise_email(self.cleaned_data.get("hod_email", ""))


class SubjectForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = Subject
        fields = ["code", "name", "semester", "credits", "is_active"]


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
    email = forms.EmailField(label="Teacher email")
    full_name = forms.CharField(label="Full name", max_length=150, required=False)
    phone = forms.CharField(label="Mobile", max_length=20, required=False)

    def clean_email(self):
        return normalise_email(self.cleaned_data["email"])


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
