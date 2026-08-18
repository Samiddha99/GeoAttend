"""
The university's catalogue: departments, batches and subjects it publishes for
the institutes it affiliates.

**Why this is a separate set of models.** A `Department` belongs to an
`Institute` — that FK is not nullable and around a thousand queries walk
through it. A university's department belongs to no institute; it is a
*template* that many institutes run their own copy of. Making `institute`
optional to squeeze both meanings into one table would put a null check into
every one of those queries, and the first one anybody forgot would silently
mix a template into a college's real data.

So there are two layers, and the direction between them is one way:

    UniversityDepartment  ──adopted by──▶  Department   (institute's own)
      └ UniversityBatch                      └ Batch
      └ UniversitySubject                    └ Subject

The institute's rows stay ordinary rows. Attendance, enrolment, reports and
exports keep working unchanged, which is the whole reason for materialising
rather than sharing.

**What "adopting" means.** An institute holding an affiliated discipline picks
a published department; a real `Department` is created for it, linked back by
`source`, and everything the university has published under that entry is
copied in. Later additions reach every adopter automatically — the university's
syllabus is one thing, not a per-college negotiation.

**Grandfathered rows.** Departments that existed before this — created by the
institute in an affiliated discipline, which was allowed then — have no
`source` and stay the institute's to edit. They are marked `is_legacy` so a
screen can say why one department behaves differently from the one beside it.
Locking them would strand any college with attendance running against them.
"""
from django.db import models, transaction

from accounts.models import Discipline, University
from core.enums import Degree, RowStatus, SubjectType, status_field


class UniversityDepartment(models.Model):
    """
    A department a university publishes, for one discipline.

    Keyed by (university, discipline, code) rather than by name: an institute
    adopting "CSE" and a university renaming it to "Computer Science &
    Engineering" should be the same department, and the code is the thing both
    sides actually agree on.
    """

    university = models.ForeignKey(University, on_delete=models.CASCADE,
                                   related_name="catalogue_departments")
    discipline = models.CharField(max_length=12, choices=Discipline.choices,
                                  db_index=True)
    name = models.CharField(max_length=120)
    code = models.CharField(max_length=20)
    status = status_field()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["discipline", "code"]
        constraints = [
            models.UniqueConstraint(
                fields=["university", "discipline", "code"],
                name="uniq_catalogue_dept"),
        ]

    def __str__(self):
        return f"{self.code} — {self.name} ({self.get_discipline_display()})"


class UniversityBatch(models.Model):
    """A cohort the university publishes under one of its departments."""

    department = models.ForeignKey(UniversityDepartment,
                                   on_delete=models.CASCADE,
                                   related_name="batches")
    label = models.CharField(max_length=12, help_text="e.g. 2022-26")
    start_year = models.PositiveIntegerField()
    end_year = models.PositiveIntegerField()
    status = status_field()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-start_year", "label"]
        constraints = [
            models.UniqueConstraint(fields=["department", "label"],
                                    name="uniq_catalogue_batch"),
        ]

    def __str__(self):
        return f"{self.department.code} {self.label}"


class UniversitySubject(models.Model):
    """A paper the university publishes, for one department and semester."""

    department = models.ForeignKey(UniversityDepartment,
                                   on_delete=models.CASCADE,
                                   related_name="subjects")
    code = models.CharField(max_length=20)
    name = models.CharField(max_length=150)
    degree = models.CharField(max_length=12, choices=Degree.choices,
                              default=Degree.BACHELOR)
    subject_type = models.CharField(max_length=12, choices=SubjectType.choices,
                                    default=SubjectType.THEORY)
    semester = models.PositiveSmallIntegerField(default=1)
    credits = models.PositiveSmallIntegerField(default=4)
    status = status_field()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["semester", "code"]
        constraints = [
            models.UniqueConstraint(fields=["department", "code"],
                                    name="uniq_catalogue_subject"),
        ]

    def __str__(self):
        return f"{self.code} — {self.name}"


# --------------------------------------------------------------------------- #
#  Who may see and publish what
# --------------------------------------------------------------------------- #
def published_departments(university, discipline=None):
    """Everything this university has published, optionally for one discipline."""
    if university is None:
        return UniversityDepartment.objects.none()
    qs = UniversityDepartment.objects.filter(university=university)
    return qs.filter(discipline=discipline) if discipline else qs


def choices_for(institute, discipline):
    """
    The catalogue entries an institute may adopt for one discipline.

    Empty when the discipline is autonomous — there is no university to publish
    one — which is exactly the case where the institute types the name itself.
    Empty is a real answer here, not a failure.
    """
    from accounts.models import InstituteAffiliation

    affiliation = InstituteAffiliation.objects.filter(
        institute=institute, discipline=discipline).select_related(
        "university").first()
    if affiliation is None or affiliation.university_id is None:
        return UniversityDepartment.objects.none()
    return published_departments(affiliation.university, discipline).exclude(
        status=RowStatus.ARCHIVED)


def is_governed(department):
    """
    Is this department the university's to define?

    True when it was adopted from a catalogue. A grandfathered department — one
    the institute created back when that was allowed — is not, and neither is
    an autonomous one. `source` is the whole test: the link *is* the claim.
    """
    return getattr(department, "source_id", None) is not None


# --------------------------------------------------------------------------- #
#  Adoption and propagation
# --------------------------------------------------------------------------- #
@transaction.atomic
def adopt(*, institute, entry, hod_email=None, actor=None):
    """
    Give an institute its own copy of a published department.

    Idempotent: adopting twice returns the existing department rather than
    failing, because the button that calls this is one a person can double
    click and the second click should not be an error.

    Everything already published under the entry comes with it. Later additions
    arrive through `propagate`, so an institute never has to check whether it
    is up to date.
    """
    from .models import Department

    department = Department.objects.filter(
        institute=institute, source=entry).first()
    if department is None:
        department = Department.objects.create(
            institute=institute, source=entry, name=entry.name,
            code=entry.code, discipline=entry.discipline,
            status=RowStatus.ACTIVE)
    propagate(entry, institutes=[institute])
    if hod_email:
        from .services import assign_hod

        assign_hod(department, hod_email, actor=actor)
    return department


@transaction.atomic
def propagate(entry, institutes=None):
    """
    Copy the entry's batches and subjects into every institute that adopted it.

    Runs on adoption and again whenever the university publishes something new,
    so "the institute can see the subjects" is true without anybody
    synchronising anything by hand.

    Matching is by natural key — label for a batch, code for a subject — so
    running it twice updates rather than duplicates. A row the institute
    already had under that key is *not* overwritten: it belongs to them, and
    quietly seizing it would take its attendance history with it.
    """
    from .models import Batch, Department, Subject

    departments = Department.objects.filter(source=entry)
    if institutes is not None:
        departments = departments.filter(institute__in=institutes)

    created = {"batches": 0, "subjects": 0}
    for department in departments:
        for batch in entry.batches.all():
            row, made = Batch.objects.get_or_create(
                department=department, label=batch.label,
                defaults={"start_year": batch.start_year,
                          "end_year": batch.end_year,
                          "status": batch.status,
                          "source": batch})
            if made:
                created["batches"] += 1
            elif row.source_id == batch.pk:
                # Ours to keep in step. One the institute made itself has no
                # source and is left exactly as it is.
                row.start_year = batch.start_year
                row.end_year = batch.end_year
                row.status = batch.status
                row.is_active = batch.status != RowStatus.ARCHIVED
                row.save(update_fields=["start_year", "end_year", "status",
                                        "is_active"])
        for subject in entry.subjects.all():
            row, made = Subject.objects.get_or_create(
                department=department, code=subject.code,
                defaults={"name": subject.name, "degree": subject.degree,
                          "subject_type": subject.subject_type,
                          "semester": subject.semester,
                          "credits": subject.credits,
                          "status": subject.status,
                          "source": subject})
            if made:
                created["subjects"] += 1
            elif row.source_id == subject.pk:
                for field in ("name", "degree", "subject_type", "semester",
                              "credits", "status"):
                    setattr(row, field, getattr(subject, field))
                row.is_active = subject.status != RowStatus.ARCHIVED
                row.save()
    return created


def propagate_everywhere(university):
    """Re-sync every entry this university publishes. For a bulk correction."""
    totals = {"batches": 0, "subjects": 0}
    for entry in published_departments(university):
        counts = propagate(entry)
        for key in totals:
            totals[key] += counts[key]
    return totals


@transaction.atomic
def release(institute, discipline):
    """
    Cut an institute's rows loose from the catalogue for one discipline.

    Called when the affiliation ends — the university delinked, or the
    discipline was removed. The `source` links are cleared and the departments
    marked legacy, which hands them back to the institute.

    **Not doing this was the alternative and it is worse.** The link is what
    makes a row read-only, so leaving it would freeze the college's syllabus to
    a university that no longer awards its degrees and has no reason to log in
    again. The rows themselves are untouched: attendance, enrolment and history
    all continue, they simply become the institute's to edit — which is what
    they now are.

    The university's own catalogue is not touched. It goes on publishing to
    whoever else adopted it.
    """
    from .models import Batch, Department, Subject

    departments = Department.objects.filter(
        institute=institute, discipline=discipline).exclude(source=None)
    ids = list(departments.values_list("id", flat=True))
    if not ids:
        return 0
    # Ids first: `source__department__university` would be a cross-collection
    # lookup inside an update, which MongoDB refuses.
    Subject.objects.filter(department_id__in=ids).exclude(source=None).update(source=None)
    Batch.objects.filter(department_id__in=ids).exclude(source=None).update(source=None)
    Department.objects.filter(id__in=ids).update(source=None, is_legacy=True)
    return len(ids)
