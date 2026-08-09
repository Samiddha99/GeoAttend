from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils.text import slugify

from accounts.models import Institute


class Department(models.Model):
    institute = models.ForeignKey(Institute, on_delete=models.CASCADE, related_name="departments")
    name = models.CharField(max_length=120)
    code = models.CharField(max_length=20)
    hod = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="heads_department",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["institute", "code"], name="uniq_dept_code_per_institute"),
            models.UniqueConstraint(fields=["institute", "name"], name="uniq_dept_name_per_institute"),
        ]

    def __str__(self):
        return f"{self.name} ({self.code})"

    def save(self, *args, **kwargs):
        self.code = slugify(self.code).upper().replace("-", "")[:20]
        super().save(*args, **kwargs)

    @property
    def hod_status(self):
        if self.hod and self.hod.registration_completed:
            return "active"
        if self.hod:
            return "invited"
        return "vacant"


class Batch(models.Model):
    """An admission cohort, e.g. 2022-26."""

    department = models.ForeignKey(Department, on_delete=models.CASCADE, related_name="batches")
    label = models.CharField(max_length=12, help_text="e.g. 2022-26")
    start_year = models.PositiveIntegerField()
    end_year = models.PositiveIntegerField()
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-start_year", "label"]
        constraints = [
            models.UniqueConstraint(fields=["department", "label"], name="uniq_batch_per_dept"),
        ]
        verbose_name_plural = "Batches"

    def __str__(self):
        return f"{self.label} · {self.department.code}"

    @property
    def student_count(self):
        return self.students.count()


class SubjectType(models.TextChoices):
    """
    How a subject is taught.

    Stored as a short code rather than a free string so that grouping a
    dropdown and filtering a report agree on what the categories are. Kept
    deliberately coarse — a lab and a lecture behave differently for
    attendance; a seminar and a workshop mostly do not, and both land in
    Other rather than growing the list.
    """

    THEORY = "THEORY", "Theory"
    PRACTICAL = "PRACTICAL", "Practical"
    OTHER = "OTHER", "Other"


class Subject(models.Model):
    department = models.ForeignKey(Department, on_delete=models.CASCADE, related_name="subjects")
    code = models.CharField(max_length=20)
    name = models.CharField(max_length=150)
    # Defaulted rather than required at the database level: every subject that
    # already exists predates this field and is a lecture course, so Theory is
    # the honest backfill. The *form* still makes the choice explicit.
    subject_type = models.CharField(
        max_length=12, choices=SubjectType.choices, default=SubjectType.THEORY,
        verbose_name="Subject type",
    )
    semester = models.PositiveSmallIntegerField(
        default=1, validators=[MinValueValidator(1), MaxValueValidator(12)]
    )
    credits = models.PositiveSmallIntegerField(default=4)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # Left alone on purpose. Ordering by `subject_type` would sort the
        # stored codes alphabetically — OTHER, PRACTICAL, THEORY — which is not
        # the order anyone wants to read. Grouping is done in Python against
        # SubjectType.choices, which is declared in the order we mean.
        ordering = ["semester", "code"]
        constraints = [
            models.UniqueConstraint(fields=["department", "code"], name="uniq_subject_per_dept"),
        ]

    def __str__(self):
        return f"{self.code} — {self.name}"

    def save(self, *args, **kwargs):
        self.code = self.code.strip().upper()
        super().save(*args, **kwargs)


class TeacherAssignment(models.Model):
    """Which teacher teaches which subject to which batch."""

    teacher = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="assignments",
        limit_choices_to={"role": "TEACHER"},
    )
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE, related_name="assignments")
    batch = models.ForeignKey(Batch, on_delete=models.CASCADE, related_name="assignments")
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="assignments_made",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["batch__label", "subject__code"]
        constraints = [
            models.UniqueConstraint(
                fields=["teacher", "subject", "batch"], name="uniq_teacher_subject_batch"
            ),
        ]

    def __str__(self):
        return f"{self.teacher} · {self.subject.code} · {self.batch.label}"


class StudentProfile(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="student_profile"
    )
    department = models.ForeignKey(Department, on_delete=models.CASCADE, related_name="students")
    batch = models.ForeignKey(Batch, on_delete=models.CASCADE, related_name="students")
    # Two different rolls, because institutes use two.
    #   class_roll — the number used day to day inside a batch ("14", "CSE-07").
    #   exam_roll  — the university/registration number on the exam admit card.
    # Required at the form and importer level rather than in the database, so
    # rows that predate the split (and the exam roll, which many students only
    # receive later) do not become unsaveable.
    class_roll = models.CharField(max_length=40, blank=True, db_index=True)
    exam_roll = models.CharField(max_length=40, blank=True, db_index=True)
    mobile = models.CharField(max_length=20, blank=True)
    guardian_name = models.CharField(max_length=150, blank=True)
    guardian_mobile = models.CharField(
        max_length=20, blank=True,
        help_text="WhatsApp number used for alerts and for guardian sign-in.",
    )
    # The same number in E.164, derived on save. Guardian sign-in looks a number
    # up here rather than in `guardian_mobile`, because that column holds
    # whatever the spreadsheet contained — "98765 43210", "09876543210" and
    # "+91 98765 43210" are one number and must resolve to one guardian.
    #
    # Stored rather than computed per query so the lookup can use an index, and
    # kept beside the original rather than replacing it so the UI still shows
    # staff the number they typed.
    guardian_mobile_e164 = models.CharField(
        max_length=20, blank=True, db_index=True, editable=False)
    guardian_email = models.EmailField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["class_roll", "user__full_name"]
        # No uniqueness on either roll, by choice: class rolls repeat across
        # batches and institutes reuse them year to year. Nothing looks a
        # student up by roll — email is the identity everywhere — so duplicates
        # are a data-quality question rather than a correctness one.

    def __str__(self):
        return f"{self.user.full_name or self.user.email} ({self.batch.label})"

    def save(self, *args, **kwargs):
        # Derived here rather than in the form, so the importer, the admin and
        # a shell script all keep it in step. A number that cannot be parsed
        # leaves the column blank, which means "no guardian can sign in with
        # this" — the safe reading.
        from notifications.whatsapp import normalise_msisdn

        number, error = normalise_msisdn(self.guardian_mobile)
        self.guardian_mobile_e164 = "" if error else number
        if "update_fields" in kwargs and kwargs["update_fields"] is not None:
            fields = set(kwargs["update_fields"])
            if "guardian_mobile" in fields:
                fields.add("guardian_mobile_e164")
                kwargs["update_fields"] = fields
        super().save(*args, **kwargs)

    @property
    def name(self):
        return self.user.full_name or self.user.email

    @property
    def email(self):
        return self.user.email


class Enrollment(models.Model):
    """A student ↔ subject link.  Attendance requests only reach enrolled students."""

    student = models.ForeignKey(StudentProfile, on_delete=models.CASCADE, related_name="enrollments")
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE, related_name="enrollments")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["subject__code"]
        constraints = [
            models.UniqueConstraint(fields=["student", "subject"], name="uniq_student_subject"),
        ]

    def __str__(self):
        return f"{self.student} → {self.subject.code}"


class ImportJob(models.Model):
    """Record of every student-roster spreadsheet a HoD uploads."""

    class Status(models.TextChoices):
        SUCCESS = "SUCCESS", "Success"
        PARTIAL = "PARTIAL", "Partial"
        FAILED = "FAILED", "Failed"

    department = models.ForeignKey(Department, on_delete=models.CASCADE, related_name="imports")
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="imports"
    )
    file_name = models.CharField(max_length=255)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.SUCCESS)
    total_rows = models.PositiveIntegerField(default=0)
    created_count = models.PositiveIntegerField(default=0)
    updated_count = models.PositiveIntegerField(default=0)
    error_count = models.PositiveIntegerField(default=0)
    report = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.file_name} ({self.status})"
