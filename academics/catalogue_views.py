"""
The university's own Departments screen.

A separate module from `academics/views.py` because it operates on a different
layer. Everything there is an institute's real rows — students attend those
classes. Everything here is the catalogue: templates a university publishes and
institutes adopt. Sharing a file would mean every function starting by working
out which of the two it was dealing with.

The university does not touch any institute's departments from here. It
publishes; the institute adopts. That direction is the whole point of the
change — see academics/catalogue.py.
"""
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404, render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from accounts.models import ActivityLog, Discipline, User
from core.decorators import role_required
from core.enums import RowStatus
from core.http import fail, form_errors, ok

from . import catalogue
from .forms import UniversityDepartmentForm

UNIVERSITY = User.Role.UNIVERSITY


def _disciplines_of(user):
    """
    The disciplines this university may publish for.

    Only the ones it actually grants affiliation in. Publishing a pharmacy
    department when nobody can adopt it would be a row that exists for nobody.
    """
    if user.university_id is None:
        return []
    held = set(user.university.disciplines.values_list("discipline", flat=True))
    return [{"value": value, "label": label}
            for value, label in Discipline.choices if value in held]


@role_required(UNIVERSITY)
@ensure_csrf_cookie
def departments_page(request):
    disciplines = _disciplines_of(request.user)
    return render(request, "academics/university_departments.html", {
        "disciplines": disciplines,
        # An empty list is a real state and the page says so rather than
        # showing a dropdown with nothing in it: a university that grants no
        # affiliation has nothing to publish and no institutes to publish to.
        "can_publish": bool(disciplines),
        "university": request.user.university,
    })


def _row(entry, adoptions):
    return {
        "id": str(entry.id),
        "name": entry.name,
        "code": entry.code,
        "discipline": entry.discipline,
        "discipline_label": entry.get_discipline_display(),
        "status": entry.status,
        "revoked": False,          # a catalogue entry has no discipline to lose
        "batches": entry.batches.exclude(status=RowStatus.ARCHIVED).count(),
        "subjects": entry.subjects.exclude(status=RowStatus.ARCHIVED).count(),
        # How many colleges are running it. The number that tells a university
        # whether editing this is a small change or a large one.
        "adoptions": adoptions.get(entry.pk, 0),
    }


@role_required(UNIVERSITY)
@require_GET
def api_departments(request):
    from django.db.models import Count

    from .models import Department

    entries = list(catalogue.published_departments(request.user.university)
                   .prefetch_related("batches", "subjects"))
    adoptions = dict(
        Department.objects.filter(source__in=entries)
        .values_list("source_id")
        .annotate(n=Count("id"))
        .values_list("source_id", "n"))
    return ok({"rows": [_row(e, adoptions) for e in entries]})


@role_required(UNIVERSITY)
@require_POST
def api_department_save(request, pk=None):
    """
    Publish a department, or correct one already published.

    A code change is refused once anybody has adopted it. The code is the key
    the adopted copies were matched on, so changing it would orphan every one
    of them — they would keep pointing at this entry while claiming a code it
    no longer has, and the next propagation would create a second department
    beside each of the originals. The name is free to change; that is what a
    rename actually is.
    """
    entry = (get_object_or_404(
        catalogue.published_departments(request.user.university), pk=pk)
        if pk else None)
    # Read *before* the form is validated. A ModelForm writes the cleaned
    # values onto its instance during `_post_clean`, so after `is_valid()` the
    # entry already carries the new code and comparing the two would always
    # find them equal — the guard below silently never fired.
    original_code = entry.code if entry is not None else None

    form = UniversityDepartmentForm(request.POST, instance=entry,
                                    disciplines=_disciplines_of(request.user))
    if not form.is_valid():
        return fail("Please correct the highlighted fields.", form_errors(form))

    if entry is not None and form.cleaned_data["code"] != original_code:
        from .models import Department

        adopted = Department.objects.filter(source=entry).count()
        if adopted:
            return fail(
                f"{adopted} institute(s) already run this department, so its "
                "code cannot change — their copies were matched on it. Rename "
                "it instead, or publish a new department and archive this one.",
                {"code": "In use by an institute."})

    entry = form.save(commit=False)
    entry.university = request.user.university
    try:
        with transaction.atomic():
            entry.save()
    except IntegrityError:
        # The uniqueness is (university, discipline, code) and the form cannot
        # check it: `university` is set here, after validation, so Django has
        # no complete row to test. Caught rather than moved into the form,
        # because two requests can still race past any check — the constraint
        # is the only thing that actually decides.
        return fail(
            "You already publish a department with that code for this "
            "discipline.", {"code": "Already published."})

    # Whatever is published under it reaches every adopter — that is what makes
    # "the institute can see the subjects" true without anyone synchronising.
    counts = catalogue.propagate(entry)
    ActivityLog.log(request, action="CATALOGUE_DEPARTMENT_SAVED",
                    detail=f"{entry.discipline} {entry.code}")
    message = "Department published."
    if counts["batches"] or counts["subjects"]:
        message += (f" {counts['batches']} batch(es) and "
                    f"{counts['subjects']} subject(s) sent to the institutes "
                    "running it.")
    return ok({"id": str(entry.id)}, message=message)


@role_required(UNIVERSITY)
@require_POST
def api_department_toggle(request, pk):
    """
    Archive or restore a published department.

    Archiving withdraws it from the adoption list; it does **not** reach into
    the colleges already running it. Their department, their students and their
    attendance carry on. A university retiring a syllabus is not asking to
    close a working department mid-term, and there is no way to undo that if it
    were wrong.
    """
    entry = get_object_or_404(
        catalogue.published_departments(request.user.university), pk=pk)
    entry.status = (RowStatus.ACTIVE if entry.status == RowStatus.ARCHIVED
                    else RowStatus.ARCHIVED)
    entry.save(update_fields=["status"])

    from .models import Department

    running = Department.objects.filter(source=entry).count()
    ActivityLog.log(request, action="CATALOGUE_DEPARTMENT_TOGGLED",
                    detail=f"{entry.code} -> {entry.status}")
    if entry.status == RowStatus.ARCHIVED:
        message = "Withdrawn from the adoption list."
        if running:
            message += (f" The {running} institute(s) already running it are "
                        "untouched.")
    else:
        message = "Published again — institutes can adopt it."
    return ok({"status": entry.status}, message=message)


@role_required(UNIVERSITY)
@require_POST
def api_department_delete(request, pk):
    """
    Remove a published department outright.

    Only while nobody runs it. Once a college has adopted it, deleting would
    cut the link and silently hand them a department they think is the
    university's — archiving says the same thing honestly.
    """
    entry = get_object_or_404(
        catalogue.published_departments(request.user.university), pk=pk)

    from .models import Department

    running = Department.objects.filter(source=entry).count()
    if running:
        return fail(
            f"{running} institute(s) are running this department. Archive it "
            "instead — that withdraws it from the adoption list and leaves "
            "their copies alone.")
    name = entry.name
    entry.delete()
    ActivityLog.log(request, action="CATALOGUE_DEPARTMENT_DELETED", detail=name)
    return ok(message=f"{name} removed from the catalogue.")


# --------------------------------------------------------------------------- #
#  Batches
# --------------------------------------------------------------------------- #
@role_required(UNIVERSITY)
@ensure_csrf_cookie
def batches_page(request):
    entries = list(catalogue.published_departments(request.user.university)
                   .exclude(status=RowStatus.ARCHIVED))
    return render(request, "academics/university_batches.html", {
        "departments": entries,
        # A cohort has to belong to a department, so there is nothing to add
        # until one is published. The page says that rather than offering an
        # empty dropdown.
        "can_publish": bool(entries),
    })


@role_required(UNIVERSITY)
@require_GET
def api_batches(request):
    from django.db.models import Count

    from .models import Batch, UniversityBatch

    published = (UniversityBatch.objects
                 .filter(department__university=request.user.university)
                 .select_related("department"))
    rows = list(published)
    running = dict(
        Batch.objects.filter(source__in=rows)
        .values_list("source_id").annotate(n=Count("id"))
        .values_list("source_id", "n"))
    return ok({"rows": [{
        "id": str(b.id),
        "label": b.label,
        "start_year": b.start_year,
        "end_year": b.end_year,
        "department": b.department.name,
        "department_code": b.department.code,
        "department_id": str(b.department_id),
        "discipline": b.department.discipline,
        "discipline_label": b.department.get_discipline_display(),
        "status": b.status,
        "revoked": False,
        # How many colleges are running this cohort. The number that says
        # whether archiving it is a small change or a large one.
        "adoptions": running.get(b.pk, 0),
    } for b in rows]})


@role_required(UNIVERSITY)
@require_POST
def api_batch_save(request, pk=None):
    """
    Publish a cohort under one of this university's departments.

    Saving propagates: every institute running that department gets the batch,
    read-only. That is what makes "the institute can see the batches" true
    without anybody synchronising anything.

    The label cannot change once a college runs it — the copies were matched on
    it, exactly as departments are matched on their code, and changing it would
    leave each college with two cohorts where it had one.
    """
    from .forms import UniversityBatchForm
    from .models import Batch, UniversityBatch

    entry = get_object_or_404(
        UniversityBatch.objects.filter(
            department__university=request.user.university),
        pk=pk) if pk else None
    # Read before validating: a ModelForm writes cleaned values onto its
    # instance, so afterwards old and new always compare equal.
    original_label = entry.label if entry is not None else None

    form = UniversityBatchForm(
        request.POST, instance=entry,
        departments=catalogue.published_departments(request.user.university))
    if not form.is_valid():
        return fail("Please correct the highlighted fields.", form_errors(form))

    if entry is not None and form.cleaned_data["label"] != original_label:
        running = Batch.objects.filter(source=entry).count()
        if running:
            return fail(
                f"{running} institute(s) are running this cohort, so its label "
                "cannot change — their copies were matched on it. Publish a new "
                "batch and archive this one instead.",
                {"label": "In use by an institute."})

    try:
        with transaction.atomic():
            entry = form.save()
    except IntegrityError:
        return fail("That department already has a batch with that label.",
                    {"label": "Already published."})

    counts = catalogue.propagate(entry.department)
    ActivityLog.log(request, action="CATALOGUE_BATCH_SAVED",
                    detail=f"{entry.department.code} {entry.label}")
    message = "Batch published."
    if counts["batches"]:
        message += f" Added at {counts['batches']} institute(s)."
    return ok({"id": str(entry.id)}, message=message)


@role_required(UNIVERSITY)
@require_POST
def api_batch_toggle(request, pk):
    """
    Archive or restore a published cohort — everywhere at once.

    Unlike a department, this **does** reach the colleges running it. A cohort
    is the university's own record of who is enrolled in what year; archiving
    it centrally and leaving twelve colleges still running it would mean the
    same batch label meant two different things depending on who was looking.

    Nothing is deleted. The students, their attendance and their history are
    all still there, and restoring brings them back.
    """
    from .models import UniversityBatch

    entry = get_object_or_404(
        UniversityBatch.objects.filter(
            department__university=request.user.university), pk=pk)
    entry.status = (RowStatus.ACTIVE if entry.status == RowStatus.ARCHIVED
                    else RowStatus.ARCHIVED)
    entry.save(update_fields=["status"])
    catalogue.propagate(entry.department)

    from .models import Batch

    running = Batch.objects.filter(source=entry).count()
    ActivityLog.log(request, action="CATALOGUE_BATCH_TOGGLED",
                    detail=f"{entry.label} -> {entry.status}")
    verb = "Archived" if entry.status == RowStatus.ARCHIVED else "Restored"
    message = f"{verb} at {running} institute(s). Nothing was deleted."
    return ok({"status": entry.status}, message=message)


@role_required(UNIVERSITY)
@require_POST
def api_batch_delete(request, pk):
    """Remove a published cohort — only while nobody runs it."""
    from .models import Batch, UniversityBatch

    entry = get_object_or_404(
        UniversityBatch.objects.filter(
            department__university=request.user.university), pk=pk)
    running = Batch.objects.filter(source=entry).count()
    if running:
        return fail(
            f"{running} institute(s) are running this cohort — it may hold "
            "students and attendance. Archive it instead.")
    label = entry.label
    entry.delete()
    ActivityLog.log(request, action="CATALOGUE_BATCH_DELETED", detail=label)
    return ok(message=f"{label} removed from the catalogue.")


# --------------------------------------------------------------------------- #
#  Subjects
# --------------------------------------------------------------------------- #
@role_required(UNIVERSITY)
@ensure_csrf_cookie
def subjects_page(request):
    entries = list(catalogue.published_departments(request.user.university)
                   .exclude(status=RowStatus.ARCHIVED))
    from core.enums import Degree, SubjectType

    return render(request, "academics/university_subjects.html", {
        "departments": entries,
        "can_publish": bool(entries),
        # Twelve rather than eight: a five-year integrated programme runs to
        # ten, and a template that cannot express one quietly pushes the
        # institute back to its own subjects.
        "semesters": range(1, 13),
        "degrees": Degree.choices,
        "subject_types": SubjectType.choices,
    })


@role_required(UNIVERSITY)
@require_GET
def api_subjects(request):
    from django.db.models import Count

    from .models import Subject, UniversitySubject

    published = (UniversitySubject.objects
                 .filter(department__university=request.user.university)
                 .select_related("department"))
    rows = list(published)
    running = dict(
        Subject.objects.filter(source__in=rows)
        .values_list("source_id").annotate(n=Count("id"))
        .values_list("source_id", "n"))
    return ok({"rows": [{
        "id": str(s.id),
        "code": s.code,
        "name": s.name,
        "semester": s.semester,
        "credits": s.credits,
        "degree": s.degree,
        "degree_label": s.get_degree_display(),
        "subject_type": s.subject_type,
        "subject_type_label": s.get_subject_type_display(),
        "department": s.department.name,
        "department_code": s.department.code,
        "department_id": str(s.department_id),
        "discipline": s.department.discipline,
        "discipline_label": s.department.get_discipline_display(),
        "status": s.status,
        "revoked": False,
        "adoptions": running.get(s.pk, 0),
    } for s in rows]})


@role_required(UNIVERSITY)
@require_POST
def api_subject_save(request, pk=None):
    """
    Publish a paper, or correct one already published.

    Saving propagates to every institute running the department, so a syllabus
    correction reaches the colleges without anybody re-entering it.

    The code cannot change once a college teaches it: the copies were matched
    on it, and a rename would leave each college with two subjects where it had
    one — the second empty, while the attendance sat in the first. The name,
    credits and semester are all free to change, which is what a syllabus
    revision actually consists of.
    """
    from .forms import UniversitySubjectForm
    from .models import Subject, UniversitySubject

    entry = get_object_or_404(
        UniversitySubject.objects.filter(
            department__university=request.user.university),
        pk=pk) if pk else None
    original_code = entry.code if entry is not None else None

    form = UniversitySubjectForm(
        request.POST, instance=entry,
        departments=catalogue.published_departments(request.user.university))
    if not form.is_valid():
        errors = form_errors(form)
        # The (department, code) constraint reads as a *non-field* error, which
        # the modal shows in its footer and never against the box the person
        # has to change. Re-keyed so the highlight lands on the code itself.
        if "already exists" in str(errors.get("__all__", "")):
            errors = {"code": "That department already has a subject with "
                              "this code."}
        return fail("Please correct the highlighted fields.", errors)

    if entry is not None and form.cleaned_data["code"] != original_code:
        running = Subject.objects.filter(source=entry).count()
        if running:
            return fail(
                f"{running} institute(s) teach this subject, so its code "
                "cannot change — their copies were matched on it. Publish a "
                "new subject and archive this one instead.",
                {"code": "In use by an institute."})

    try:
        with transaction.atomic():
            entry = form.save()
    except IntegrityError:
        return fail("That department already has a subject with that code.",
                    {"code": "Already published."})

    counts = catalogue.propagate(entry.department)
    ActivityLog.log(request, action="CATALOGUE_SUBJECT_SAVED",
                    detail=f"{entry.department.code} {entry.code}")
    message = "Subject published."
    if counts["subjects"]:
        message += f" Added at {counts['subjects']} institute(s)."
    return ok({"id": str(entry.id)}, message=message)


@role_required(UNIVERSITY)
@require_POST
def api_subject_toggle(request, pk):
    """
    Archive or restore a published paper — everywhere at once.

    Like a batch and unlike a department. A paper withdrawn from the syllabus
    but still being taught at nine colleges is not a withdrawn paper, and the
    marks would have nowhere to go at the end of it.

    Nothing is deleted: the enrolments, sessions and attendance stay exactly
    where they are, and restoring brings them all back into view.
    """
    from .models import Subject, UniversitySubject

    entry = get_object_or_404(
        UniversitySubject.objects.filter(
            department__university=request.user.university), pk=pk)
    entry.status = (RowStatus.ACTIVE if entry.status == RowStatus.ARCHIVED
                    else RowStatus.ARCHIVED)
    entry.save(update_fields=["status"])
    catalogue.propagate(entry.department)

    running = Subject.objects.filter(source=entry).count()
    ActivityLog.log(request, action="CATALOGUE_SUBJECT_TOGGLED",
                    detail=f"{entry.code} -> {entry.status}")
    verb = "Archived" if entry.status == RowStatus.ARCHIVED else "Restored"
    return ok({"status": entry.status},
              message=f"{verb} at {running} institute(s). Nothing was deleted.")


@role_required(UNIVERSITY)
@require_POST
def api_subject_delete(request, pk):
    """Remove a published paper — only while nobody teaches it."""
    from .models import Subject, UniversitySubject

    entry = get_object_or_404(
        UniversitySubject.objects.filter(
            department__university=request.user.university), pk=pk)
    running = Subject.objects.filter(source=entry).count()
    if running:
        return fail(
            f"{running} institute(s) teach this subject — it may hold "
            "enrolments and attendance. Archive it instead.")
    code = entry.code
    entry.delete()
    ActivityLog.log(request, action="CATALOGUE_SUBJECT_DELETED", detail=code)
    return ok(message=f"{code} removed from the catalogue.")
