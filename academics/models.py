from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils.text import slugify

from accounts.models import Discipline, Institute
from core.enums import (
    Degree,
    RowStatus,
    SubjectType,
    revoked_field,
    status_field,
)


class Department(models.Model):
    institute = models.ForeignKey(Institute, on_delete=models.CASCADE, related_name="departments")
    name = models.CharField(max_length=120)
    code = models.CharField(max_length=20)
    # Which broad field this department belongs to, and therefore which of the
    # institute's affiliations governs it. Subjects, batches and students all
    # inherit it from here rather than carrying their own copy: a department is
    # engineering or it is pharmacy, and letting a subject disagree with its
    # own department would be a contradiction with no right answer.
    #
    # Blank on purpose, and blank by default. Every department that existed
    # before this field did has no answer on file, and guessing one would
    # silently lock an institute out of its own subjects — see
    # `academics.curriculum.is_read_only`. Unset reads as "nobody affiliates
    # this", which is exactly how those departments behaved yesterday.
    discipline = models.CharField(
        max_length=12, choices=Discipline.choices, blank=True, db_index=True,
        help_text="Which affiliation governs this department. Leave blank if "
                  "it is not tied to one.")
    hod = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="heads_department",
    )
    is_active = models.BooleanField(default=True)
    # Lifecycle and revocation — two independent facts, two fields. See
    # core/enums.py for why they are not one derived value.
    status = status_field()
    # The catalogue entry this was adopted from, or null.
    #
    # `source` is the whole test for "is this the university's to define" —
    # the link *is* the claim, so there is no second flag to fall out of step
    # with it. Null means the institute owns it: an autonomous department, or
    # one grandfathered from before the catalogue existed.
    source = models.ForeignKey(
        "academics.UniversityDepartment", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="adoptions")
    # Created by the institute in an affiliated discipline back when that was
    # allowed. Kept editable — locking it would strand any college with
    # attendance running against it — and flagged so a screen can explain why
    # one department behaves differently from the one beside it.
    is_legacy = models.BooleanField(default=False, editable=False)
    is_revoked = revoked_field()
    # Set when a discipline removal switched this row off, and only when it
    # was active at the time. It is the memory that makes a restore return each
    # row to what it was rather than turning everything on: a cohort that
    # graduated in 2023 and one hidden by the removal are indistinguishable
    # without it, and the first attempt at this restored both.
    #
    # Cleared as soon as the row is restored, so it never describes anything
    # but the removal currently in force.
    archived_with_discipline = models.BooleanField(default=False, editable=False)
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
        from core.enums import RowStatus

        # status is the source of truth; is_active mirrors it.
        # See the identical note on Department.save.
        # `is_active` decides between running and archived; `status` adds the
        # INVITED distinction on top. One direction, so the two can never
        # disagree — the first version let `status` win and a form that ticked
        # Active on an archived row was silently overruled by its own status.
        #
        # Code that archives in bulk uses `.update()`, which skips this, so
        # those call sites set both columns explicitly.
        if not self.is_active:
            self.status = RowStatus.ARCHIVED
        elif self.status == RowStatus.ARCHIVED:
            self.status = RowStatus.ACTIVE
        self.code = slugify(self.code).upper().replace("-", "")[:20]
        # Revocation follows the discipline, so a department that moves between
        # disciplines updates the flag on itself. Without this it is right only
        # until somebody edits a department.
        if self.institute_id:
            from accounts.models import InstituteAffiliation

            held = set(InstituteAffiliation.objects
                       .filter(institute_id=self.institute_id)
                       .values_list("discipline", flat=True))
            self.is_revoked = bool(self.discipline) and self.discipline not in held
        super().save(*args, **kwargs)
        if self.pk:
            # And on everything inside it.
            from .curriculum import cascade_revoked

            cascade_revoked(self)

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
    # Set when this row came from an affiliating university's curriculum
    # rather than from the institute itself. Null means the institute made it
    # and owns it — which is every row that existed before the university tier,
    # and every row in an autonomous institute.
    #
    # It is a plain nullable FK rather than a separate "shared curriculum"
    # table because the pushed rows have to *be* ordinary batches: attendance,
    # enrolment, allocation and every report already join through them, and a
    # parallel type would mean teaching all of that about a second one.
    owner_university = models.ForeignKey(
        "accounts.University", on_delete=models.CASCADE, null=True, blank=True,
        related_name="curriculum_batches")
    label = models.CharField(max_length=12, help_text="e.g. 2022-26")
    start_year = models.PositiveIntegerField()
    end_year = models.PositiveIntegerField()
    is_active = models.BooleanField(default=True)
    # Lifecycle and revocation — two independent facts, two fields. See
    # core/enums.py for why they are not one derived value.
    status = status_field()
    source = models.ForeignKey(
        "academics.UniversityBatch", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="adoptions")
    is_revoked = revoked_field()
    # Set when a discipline removal switched this row off, and only when it
    # was active at the time. It is the memory that makes a restore return each
    # row to what it was rather than turning everything on: a cohort that
    # graduated in 2023 and one hidden by the removal are indistinguishable
    # without it, and the first attempt at this restored both.
    #
    # Cleared as soon as the row is restored, so it never describes anything
    # but the removal currently in force.
    archived_with_discipline = models.BooleanField(default=False, editable=False)
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

    def save(self, *args, **kwargs):
        """
        `is_active` mirrors `status`, and `status` is the source of truth.

        `is_active` is kept because a hundred queries filter on it — batches,
        enrolments, reports, exports. Deriving it here means there is exactly
        one writer, so the two can never drift; a caller that flips either one
        gets both.
        """
        from core.enums import RowStatus

        # `is_active` decides between running and archived; `status` adds the
        # INVITED distinction on top. One direction, so the two can never
        # disagree — the first version let `status` win and a form that ticked
        # Active on an archived row was silently overruled by its own status.
        #
        # Code that archives in bulk uses `.update()`, which skips this, so
        # those call sites set both columns explicitly.
        if not self.is_active:
            self.status = RowStatus.ARCHIVED
        elif self.status == RowStatus.ARCHIVED:
            self.status = RowStatus.ACTIVE
        # Revocation is the department's fact; a row inside it carries a copy
        # so it can be filtered and counted without a join. Inherited on every
        # save, so a subject created inside a revoked department is revoked
        # from the moment it exists.
        if self.department_id:
            self.is_revoked = self.department.is_revoked

        if "update_fields" in kwargs and kwargs["update_fields"] is not None:
            fields = set(kwargs["update_fields"])
            if fields & {"status", "is_active"}:
                fields |= {"status", "is_active"}
                kwargs["update_fields"] = sorted(fields)
        super().save(*args, **kwargs)


class Section(models.Model):
    """
    A subdivision of one batch — "A", "B", "CSE-1".

    **Why a record rather than a text field on the student.** `class_roll` is a
    label: nobody ever asks for the list of class rolls. A section is a *thing*
    a college manages — it gets renamed, retired, and eventually gets its own
    timetable. As free text, "A", "a" and "A " are three sections in the filter
    dropdown and there is nowhere to correct that; as a row, the filter cannot
    contain a typo because it is built from what exists.

    **Scoped to a batch, not to a department.** Section A of 2022-26 and section
    A of 2023-27 are different groups of people who happen to share a letter.
    Hanging them off the department instead would make one row mean both, and
    the first cohort to graduate would take the other's students with it.

    **State is not duplicated from the batch.** A section carries `is_active` so
    a college can retire "C" when the intake shrinks, but it does not carry
    `is_revoked` or `archived_with_discipline`. Those flow down the department →
    batch → student chain, and a section is not on that chain: it holds no
    attendance, no enrolment and no figures of its own. A section under an
    archived batch is already unreachable, because its students are. Adding it
    to the cascade would mean a sixth model in every discipline-removal path
    for no question anybody asks.
    """

    batch = models.ForeignKey(Batch, on_delete=models.CASCADE,
                              related_name="sections")
    name = models.CharField(max_length=20, help_text='e.g. "A"')
    is_active = models.BooleanField(default=True)
    status = status_field()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["batch", "name"],
                                    name="uniq_section_per_batch"),
        ]

    def __str__(self):
        return f"{self.batch.label} · {self.name}"

    @property
    def student_count(self):
        return self.students.count()

    def save(self, *args, **kwargs):
        """`is_active` decides, `status` follows — the one-writer rule again."""
        from core.enums import RowStatus

        if not self.is_active:
            self.status = RowStatus.ARCHIVED
        elif self.status == RowStatus.ARCHIVED:
            self.status = RowStatus.ACTIVE
        if "update_fields" in kwargs and kwargs["update_fields"] is not None:
            fields = set(kwargs["update_fields"])
            if fields & {"status", "is_active"}:
                fields |= {"status", "is_active"}
                kwargs["update_fields"] = sorted(fields)
        super().save(*args, **kwargs)


# Moved to core/enums.py so the university's catalogue can use the same lists
# without a circular import — `academics.catalogue` is imported by this module.
# Re-exported here because a hundred call sites already say
# `from .models import Degree`.
__all_shared_choices__ = (SubjectType, Degree)  # imported above; kept in the
# module namespace so `from .models import Degree` keeps working.


class Subject(models.Model):
    department = models.ForeignKey(Department, on_delete=models.CASCADE, related_name="subjects")
    # Set when this row came from an affiliating university's curriculum
    # rather than from the institute itself. Null means the institute made it
    # and owns it — which is every row that existed before the university tier,
    # and every row in an autonomous institute.
    #
    # It is a plain nullable FK rather than a separate "shared curriculum"
    # table because the pushed rows have to *be* ordinary subjects: attendance,
    # enrolment, allocation and every report already join through them, and a
    # parallel type would mean teaching all of that about a second one.
    owner_university = models.ForeignKey(
        "accounts.University", on_delete=models.CASCADE, null=True, blank=True,
        related_name="curriculum_subjects")
    code = models.CharField(max_length=20)
    name = models.CharField(max_length=150)
    # Defaulted for the same reason as `subject_type`: every subject that
    # already exists predates this field, and Bachelor is the honest backfill
    # for an undergraduate-first institute. The *form* still makes it explicit.
    degree = models.CharField(
        max_length=12, choices=Degree.choices, default=Degree.BACHELOR,
        db_index=True, verbose_name="Degree",
    )
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
    # Lifecycle and revocation — two independent facts, two fields. See
    # core/enums.py for why they are not one derived value.
    status = status_field()
    source = models.ForeignKey(
        "academics.UniversitySubject", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="adoptions")
    is_revoked = revoked_field()
    # Set when a discipline removal switched this row off, and only when it
    # was active at the time. It is the memory that makes a restore return each
    # row to what it was rather than turning everything on: a cohort that
    # graduated in 2023 and one hidden by the removal are indistinguishable
    # without it, and the first attempt at this restored both.
    #
    # Cleared as soon as the row is restored, so it never describes anything
    # but the removal currently in force.
    archived_with_discipline = models.BooleanField(default=False, editable=False)
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
        from core.enums import RowStatus

        # status is the source of truth; is_active mirrors it.
        # See the identical note on Department.save.
        # `is_active` decides between running and archived; `status` adds the
        # INVITED distinction on top. One direction, so the two can never
        # disagree — the first version let `status` win and a form that ticked
        # Active on an archived row was silently overruled by its own status.
        #
        # Code that archives in bulk uses `.update()`, which skips this, so
        # those call sites set both columns explicitly.
        if not self.is_active:
            self.status = RowStatus.ARCHIVED
        elif self.status == RowStatus.ARCHIVED:
            self.status = RowStatus.ACTIVE
        # Revocation is the department's fact; a row inside it carries a copy
        # so it can be filtered and counted without a join. Inherited on every
        # save, so a subject created inside a revoked department is revoked
        # from the moment it exists.
        if self.department_id:
            self.is_revoked = self.department.is_revoked

        self.code = self.code.strip().upper()
        super().save(*args, **kwargs)


class TeacherAssignment(models.Model):
    """
    Which teacher teaches which subject, to which group of students.

    **The group is (batch, section).** A batch alone was the group until
    sections existed; now a cohort of 180 may be taught in three rooms by three
    people, and an allocation that could only say "2022-26" put all three in
    front of all 180.

    **Department is not stored, and deliberately.** `subject.department` and
    `batch.department` already say it — a third copy is a third thing to keep in
    step, and the one that drifts is the one somebody trusts. The screens still
    *show* it, and the picker still walks department → batch → section →
    subject; it is derived at the point of display rather than duplicated at
    rest. See `academics.allocation`.

    **`section` is nullable, and null is not "unknown".** It means *the whole
    batch* — which is what every allocation meant before sections, and what a
    college that does not divide its cohorts still means today. That is why the
    migration adding this column needs no backfill: every existing row already
    says the right thing.
    """

    teacher = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="assignments",
        limit_choices_to={"role": "TEACHER"},
    )
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE, related_name="assignments")
    batch = models.ForeignKey(Batch, on_delete=models.CASCADE, related_name="assignments")
    # `CASCADE` rather than `SET_NULL`, unlike the student's link to a section.
    # Deleting a section must not delete the people in it — but an allocation
    # *to* that section is about a group that no longer exists, and quietly
    # widening it to the whole batch would put a teacher in front of students
    # nobody assigned them.
    section = models.ForeignKey(
        "academics.Section", on_delete=models.CASCADE, null=True, blank=True,
        related_name="assignments",
        help_text="Leave empty for the whole batch.")
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="assignments_made",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["batch__label", "section__name", "subject__code"]
        constraints = [
            # **One plain constraint, and null is a value.**
            #
            # The first attempt used two *partial* constraints keyed on
            # `section__isnull`, because in SQL `NULL != NULL` — so a composite
            # unique over a nullable column would let `(t, s, b, NULL)` be
            # inserted twice without colliding. MongoDB refused to build them:
            # it has no `isnull` lookup in an index.
            #
            # It also does not need one. MongoDB indexes a missing or null
            # field as a value, so two whole-batch rows for the same teacher and
            # subject *do* collide under a plain compound unique index. The
            # backend gives for free what SQL needed a partial index to say.
            #
            # Worth knowing when reading the tests: they run on sqlite, where
            # this is the weaker SQL rule and a duplicate whole-batch row would
            # slip past the database. Nothing creates one — `set_allocations`
            # goes through `update_or_create` keyed on all four columns, and
            # `resolve_pairs` rejects a payload that names the same group twice
            # — but the *database* backstop for that one case exists only in
            # production.
            models.UniqueConstraint(
                fields=["teacher", "subject", "batch", "section"],
                name="uniq_teacher_subject_batch_section"),
        ]

    def __str__(self):
        where = f"{self.batch.label}"
        if self.section_id:
            where += f" · {self.section.name}"
        return f"{self.teacher} · {self.subject.code} · {where}"

    @property
    def department(self):
        """Derived, never stored — see the class docstring."""
        return self.subject.department


class StudentProfile(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="student_profile"
    )
    department = models.ForeignKey(Department, on_delete=models.CASCADE, related_name="students")
    batch = models.ForeignKey(Batch, on_delete=models.CASCADE, related_name="students")
    # Which section of that batch. Nullable, and deliberately so: every student
    # who predates this has none, and making it required would leave those rows
    # unsaveable — the same reasoning as `class_roll` below and `pan_number` on
    # User.
    #
    # `SET_NULL` rather than `CASCADE`: deleting a section must not delete the
    # people in it. They become unsectioned, which is a state the screens
    # already handle because it is what every existing student looks like.
    #
    # The section must belong to the student's own batch. That is not
    # expressible as a database constraint across two tables here, so it is
    # enforced in `academics.sections.assert_in_batch`, which every write path
    # calls.
    section = models.ForeignKey(
        "academics.Section", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="students")
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
    # Lifecycle and revocation — two independent facts, two fields. See
    # core/enums.py for why they are not one derived value.
    status = status_field()
    is_revoked = revoked_field()
    # Set when a discipline removal switched this row off, and only when it
    # was active at the time. It is the memory that makes a restore return each
    # row to what it was rather than turning everything on: a cohort that
    # graduated in 2023 and one hidden by the removal are indistinguishable
    # without it, and the first attempt at this restored both.
    #
    # Cleared as soon as the row is restored, so it never describes anything
    # but the removal currently in force.
    archived_with_discipline = models.BooleanField(default=False, editable=False)
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
        from core.enums import RowStatus

        # status is the source of truth; is_active mirrors it.
        # See the identical note on Department.save.
        # `is_active` decides between running and archived; `status` adds the
        # INVITED distinction on top. One direction, so the two can never
        # disagree — the first version let `status` win and a form that ticked
        # Active on an archived row was silently overruled by its own status.
        #
        # Code that archives in bulk uses `.update()`, which skips this, so
        # those call sites set both columns explicitly.
        if not self.is_active:
            self.status = RowStatus.ARCHIVED
        elif self.status == RowStatus.ARCHIVED:
            self.status = RowStatus.ACTIVE
        # Revocation is the department's fact; a row inside it carries a copy
        # so it can be filtered and counted without a join. Inherited on every
        # save, so a subject created inside a revoked department is revoked
        # from the moment it exists.
        if self.department_id:
            self.is_revoked = self.department.is_revoked

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

    # **The institute, not a department.** A roster used to be uploaded one
    # department at a time, from a dropdown; it carries a Department column now
    # and one file can span the whole college. Recording a single department
    # would mean picking a winner among the six a file touched — the ones it
    # actually reached are listed in `report["departments"]`.
    institute = models.ForeignKey(
        "accounts.Institute", on_delete=models.CASCADE, null=True, blank=True,
        related_name="imports")
    # Kept and nullable for the jobs uploaded before the column existed, whose
    # single department is a real fact worth not throwing away. Nothing writes
    # it any more.
    department = models.ForeignKey(Department, on_delete=models.CASCADE,
                                   null=True, blank=True,
                                   related_name="imports")
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


# The university's catalogue. Defined in academics/catalogue.py — kept apart
# because it is a different layer with different rules — and re-exported here
# so `academics.models` remains the single import path the rest of the project
# already uses.
from .catalogue import (  # noqa: E402,F401  (circular by necessity)
    UniversityBatch,
    UniversityDepartment,
    UniversitySubject,
)
