import json

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Prefetch, Q
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.utils.timezone import localtime
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from accounts.emails import send_invitation
from accounts import face_service
from accounts.models import (
    ActivityLog,
    Discipline,
    FaceEnrolment,
    FaceSample,
    Invitation,
)
from accounts.services import invite_user, unlink_device
from accounts import pan as pan_rules
from accounts import suspension
from accounts.suspension import may_suspend as can_suspend
from notifications.whatsapp import normalise_msisdn
from core.decorators import guardian_readonly, role_required
from core.enums import RowStatus
from core.http import fail, form_errors, ok
from core.utils import clean_object_id, clean_object_ids, parse_date

from . import catalogue
from .curriculum import (
    STATE_ARCHIVED,
    department_states,
    sync_revoked,
    effective_state,
    live_departments,
    own_departments,
    reactivate_department_contents,
    may_define_department,
    selectable_disciplines,
    assert_writable,
    is_read_only,
)
from .services import HodError
from .forms import BatchForm, DepartmentForm, HodEmailForm, StudentEditForm, SubjectForm, TeacherInviteForm
from .importer import (
    build_roster_workbook,
    build_template_workbook,
    import_students,
    read_rows,
)
from . import allocation
from . import sections
from .models import (
    Batch,
    Degree,
    Department,
    Enrollment,
    ImportJob,
    Section,
    Subject,
    SubjectType,
    TeacherAssignment,
)
from .selectors import (
    all_students_for,
    semester_options,
    batches_for,
    current_department,
    departments_for,
    manageable_department_ids,
    students_qs_for,
    subjects_for,
    teachers_for,
    visible_teachers_for,
)

HEAD, HOD, TEACHER, STUDENT, GUARDIAN, UNIVERSITY = (
    "HEAD", "HOD", "TEACHER", "STUDENT", "GUARDIAN", "UNIVERSITY")


def dial(raw):
    """
    Everything a click-to-call / click-to-WhatsApp link needs.

    `tel:` is happy with +E.164; wa.me wants the same digits with no '+'.
    Normalising here rather than in the browser means a number stored as
    "98765 43210" still produces a working link.
    """
    number, error = normalise_msisdn(raw)
    return {
        "raw": raw or "",
        "tel": number,
        "wa": number.lstrip("+") if number else "",
        "error": error or "",
    }


def _scoped_department(request, dept_id=None):
    """Resolve the department the request is allowed to act on."""
    qs = departments_for(request.user)
    if dept_id:
        return get_object_or_404(qs, pk=dept_id)
    dept = qs.first()
    if dept is None:
        raise Http404("No department is linked to your account.")
    return dept


# --------------------------------------------------------------------------- #
#  Pages
# --------------------------------------------------------------------------- #
@role_required(HEAD, UNIVERSITY)
@ensure_csrf_cookie
def departments_page(request):
    institute = _target_institute(request)
    # Only what this actor governs — see academics/curriculum.py. An empty list
    # is a real answer: an institute with no autonomous discipline has no
    # department of its own to create, and the page says so instead of showing
    # a dropdown with nothing in it.
    allowed = selectable_disciplines(request.user, institute)
    from accounts.models import Discipline

    return render(request, "academics/departments.html", {
        "form": DepartmentForm(user=request.user, institute=institute),
        "disciplines": allowed,
        # The filter offers every discipline that could appear in the table,
        # which is wider than what can be created: departments in affiliated
        # disciplines are listed here too, just not editable.
        "filter_disciplines": [{"value": v, "label": l}
                               for v, l in Discipline.choices],
        "can_add": bool(allowed),
    })


@role_required(HEAD, HOD, UNIVERSITY)
@ensure_csrf_cookie
def subjects_page(request):
    return render(request, "academics/subjects.html", {
        # Two lists, because they answer two questions. A *form* offers live
        # departments only: filing a new subject under an archived one would
        # create a row that is archived the moment it exists. A *filter* offers
        # all of them, because the table it filters contains archived and
        # revoked rows and leaving their departments out makes those rows
        # unfindable.
        # And only departments the institute runs itself. Papers for an
        # adopted department are published by the university; listing it here
        # would offer a choice `api_subject_save` refuses. See
        # academics/curriculum.own_departments.
        "form": SubjectForm(),
        "departments": live_departments(
            own_departments(departments_for(request.user))),
        "can_add": (not request.user.is_university and live_departments(
            own_departments(departments_for(request.user))).exists()),
        "filter_departments": departments_for(request.user).order_by("name"),
    })


@role_required(HEAD, HOD, UNIVERSITY)
@ensure_csrf_cookie
def batches_page(request):
    # The dropdown offers only departments the institute runs itself. Batches
    # in an adopted department come from the university's catalogue, and
    # `api_batch_save` refuses one created here — listing them would be
    # offering a choice that comes back as an error. The *filter* above the
    # table still shows every department, because you look at what you cannot
    # edit. See academics/curriculum.own_departments.
    mine = live_departments(own_departments(departments_for(request.user)))
    return render(request, "academics/batches.html", {
        "form": BatchForm(),
        "departments": mine,
        # False for a university too. This page shows the colleges' own rows;
        # a university adds cohorts on its catalogue screen.
        "can_add": not request.user.is_university and mine.exists(),
        "filter_departments": departments_for(request.user).order_by("name"),
    })


@role_required(HEAD, HOD, TEACHER, STUDENT, GUARDIAN, UNIVERSITY)
@guardian_readonly
@ensure_csrf_cookie
def teachers_page(request):
    # Everyone on staff sees the whole institute's teachers; who may *edit* a
    # given row is decided per row by api_teachers, and enforced independently
    # by the write endpoints. `can_manage` here only means "can edit anyone
    # at all", which is what decides whether the invite modal exists.
    can_manage = request.user.role in (HEAD, HOD)
    # "Directory mode": a read-only list with no manage controls and no
    # personal numbers. Guardians get the same treatment as students, because
    # the reasons for it — a teacher who has left, a private mobile — do not
    # change with who is doing the reading.
    is_student = request.user.role in (STUDENT, GUARDIAN)
    # A university has no institute of its own, so the filter lists span every
    # institute it reaches instead.
    from accounts.scoping import institutes_for

    institutes = institutes_for(request.user)
    # Filters span the institute now that the table does. They deliberately do
    # not come from api_lookups: for a teacher that endpoint returns only the
    # subjects they personally teach, which would be useless for finding a
    # colleague in another department.
    return render(request, "academics/teachers.html", {
        "form": TeacherInviteForm() if can_manage else None,
        # Live only: inviting a teacher into an archived department would
        # hand them an account with nothing in it.
        "departments": live_departments(departments_for(request.user)),
        "can_manage": can_manage,
        "is_student": is_student,
        # Staff mobile numbers are not published to students by default. Names,
        # departments and subjects make a useful directory; handing every
        # student a teacher's personal number and a WhatsApp button is a
        # separate decision, and not one that can be walked back once made.
        "show_mobile": not is_student,
        # Every department, archived ones included — see subjects_page. The
        # *invite* dropdown above is the live-only one.
        "filter_departments": Department.objects.filter(
            institute__in=institutes).order_by("name"),
        "filter_subjects": Subject.objects.filter(
            department__institute__in=institutes, is_active=True).order_by("code"),
        # Only the semesters that exist in scope, each once — see
        # academics.selectors.semester_options for the trap in that query.
        "semesters": semester_options(request.user),
    })


def _students_page(request, *, wide):
    """
    Shared by both student screens.

    wide=False  "My students" — only the classes this person is responsible
                for. For a head or HoD that is already their whole scope, so
                this is the only screen they get.
    wide=True   "Students" — every student in the institute. A teacher sees
                this read-only apart from unlinking a device.
    """
    staff = request.user.role in (HOD, HEAD, UNIVERSITY)
    from accounts.scoping import institutes_for

    return render(request, "academics/students.html", {
        # The import modal's target list: live only.
        "departments": (live_departments(Department.objects.filter(
            institute__in=institutes_for(request.user))).order_by("name")
            if wide else live_departments(departments_for(request.user))),
        # The toolbar filter: everything, so an archived department's students
        # can still be found.
        "filter_departments": (
            Department.objects.filter(
                institute__in=institutes_for(request.user)).order_by("name")
            if wide else departments_for(request.user).order_by("name")),
        # Both are head/HoD only. They are separate flags because they are
        # separate ideas: importing a roster and editing a row. Teachers get
        # neither on either screen.
        "can_import": staff,
        "can_edit": staff,
        # The whole point of the wide screen for a teacher: unlink a device for
        # a student who is not in their own classes.
        "can_unlink_device": True,
        # Live sections only, for the same reason the department filter differs
        # from the department dropdown: a filter offering a retired section
        # returns an empty table and reads as a bug. The *column* still shows a
        # retired section's name against whoever is in one.
        "filter_sections": sections.for_user(request.user),
        "wide": wide,
        "page_heading": "Students" if wide else "My students",
    })


@role_required(HEAD, HOD, TEACHER, UNIVERSITY)
@ensure_csrf_cookie
def students_page(request):
    return _students_page(request, wide=request.user.role in (HOD, HEAD, UNIVERSITY))


@role_required(HEAD, HOD, TEACHER, UNIVERSITY)
@ensure_csrf_cookie
def all_students_page(request):
    """
    Institute-wide directory.

    Only teachers are linked here — a head or HoD already sees their whole
    scope on `students` — but they are allowed in rather than served a
    confusing 403 if they land on the URL.
    """
    return _students_page(request, wide=True)


# --------------------------------------------------------------------------- #
#  Departments + HoD invitations  (Head only)
# --------------------------------------------------------------------------- #
@role_required(HEAD, UNIVERSITY)
@require_GET
def api_department_options(request):
    """
    What the Add-a-department modal offers, per discipline.

    One call, all disciplines, rather than a round trip each time the picker
    changes: an institute holds a handful, and a dropdown that waits on the
    network to populate is a dropdown people click twice.

    `catalogue` is empty for an autonomous discipline, and that emptiness *is*
    the instruction — the modal switches to name-and-code when it sees it,
    rather than being told separately which mode to be in.
    """
    institute = _target_institute(request)
    if institute is None:
        return ok({"disciplines": []})

    labels = dict(Discipline.choices)
    rows = []
    for affiliation in institute.affiliations.select_related("university"):
        entries = catalogue.choices_for(institute, affiliation.discipline)
        rows.append({
            "value": affiliation.discipline,
            "label": labels.get(affiliation.discipline, affiliation.discipline),
            "autonomous": affiliation.university_id is None,
            "university": (affiliation.university.short_name
                           or affiliation.university.name)
                          if affiliation.university_id else "",
            "catalogue": [{"id": str(e.id), "name": e.name, "code": e.code,
                           "subjects": e.subjects.count(),
                           "batches": e.batches.count(),
                           # Already running it, so the picker can say so
                           # instead of letting somebody adopt twice and
                           # wonder why nothing changed.
                           "adopted": Department.objects.filter(
                               institute=institute, source=e).exists()}
                          for e in entries],
        })
    order = {value: i for i, (value, _) in enumerate(Discipline.choices)}
    rows.sort(key=lambda r: order.get(r["value"], len(order)))
    return ok({"disciplines": rows})


@role_required(HEAD, UNIVERSITY)
@require_GET
def api_departments(request):
    qs = (departments_for(request.user)
          .select_related("hod", "institute")
          .order_by("name"))
    # One query for the whole page rather than one per row: governance is a
    # lookup from (institute, discipline) and every row here shares an
    # institute in all but the university's case.
    departments = list(qs)
    from accounts.models import InstituteAffiliation

    holders = {
        (a.institute_id, a.discipline): a.university
        for a in InstituteAffiliation.objects.filter(
            institute__in={d.institute_id for d in departments}
        ).select_related("university")
    }
    governors = {d.pk: holders.get((d.institute_id, d.discipline))
                 for d in departments if d.discipline}
    states = department_states(departments)
    counts = _department_counts(departments)

    rows = [{
        "id": d.id,
        "name": d.name,
        "code": d.code,
        "hod_name": d.hod.full_name if d.hod else "",
        "hod_email": d.hod.email if d.hod else "",
        "hod_status": d.hod_status,
        "subject_count": counts[d.pk]["subjects"],
        "batch_count": counts[d.pk]["batches"],
        "student_count": counts[d.pk]["students"],
        "teacher_count": counts[d.pk]["teachers"],
        "is_active": d.is_active,
        # Only a university ever renders this — GA.instituteCol() returns null
        # for everyone else — but it is always sent, so the column never has to
        # care who is asking.
        "institute": d.institute.code,
        "institute_name": d.institute.name,
        "discipline": d.discipline,
        "discipline_label": d.get_discipline_display(),
        # Two separate rights — see api_department_save. `can_define` drives the
        # edit and delete controls; the HoD stays changeable either way.
        "can_define": (governors.get(d.pk).pk == request.user.university_id
                       if governors.get(d.pk) is not None
                       else not request.user.is_university),
        "governed_by": (governors[d.pk].short_name or governors[d.pk].name)
                       if governors.get(d.pk) else "",
        "status": _row_status(d),
        "state": effective_state(d),
        "revoked": d.is_revoked,
    } for d in departments]
    return ok({"rows": rows})


@role_required(HEAD, UNIVERSITY)
@require_POST
def api_department_save(request, pk=None):
    """
    Create or update one institute's department.

    **Creating is discipline-first, and what happens next depends on it.**

    * An *affiliated* discipline: the institute picks one of the departments
      its university publishes and supplies a HoD email. It cannot type a name
      — the name and code belong to the university, and a college inventing its
      own would make its copy disagree with every other running the same
      syllabus.
    * An *autonomous* discipline: there is no university to publish one, so the
      institute writes the name and code itself.

    **Editing keeps two rights apart.** Defining a department — name, code,
    discipline — follows whether it was adopted. Running it, meaning who leads
    it, stays with the institute whatever the affiliation: a university setting
    a syllabus has no view on which of the institute's staff heads the office,
    and taking that away would leave an affiliated college unable to replace a
    departing HoD.

    So an institute editing an adopted department is allowed through with
    everything except the HoD ignored rather than refused — refusing the whole
    request would block the one change it is entitled to make.
    """
    instance = get_object_or_404(departments_for(request.user), pk=pk) if pk else None
    if instance is None:
        return _create_department(request)

    may_define = may_define_department(request.user, instance)
    was_revoked = instance.is_revoked

    if may_define:
        form = DepartmentForm(request.POST, instance=instance,
                              user=request.user,
                              institute=_target_institute(request, instance))
    else:
        # Only the HoD is in play, so only the HoD is validated. Running the
        # full form here would reject the request on a discipline the institute
        # is not allowed to choose and never asked to change — refusing the one
        # edit it *is* entitled to make.
        form = HodEmailForm(request.POST)
    if not form.is_valid():
        return fail("Please correct the highlighted fields.", form_errors(form))

    reactivated = None
    try:
        with transaction.atomic():
            if may_define:
                dept = form.save(commit=False)
                dept.institute = _target_institute(request, instance)
                dept.save()
                sync_revoked(dept.institute)
                dept.refresh_from_db()
                if was_revoked and not dept.is_revoked:
                    reactivated = reactivate_department_contents(
                        dept, actor=request.user)
                    dept.refresh_from_db()
            else:
                dept = Department.objects.get(pk=instance.pk)
            invited = _set_hod(dept, form.cleaned_data.get("hod_email"),
                               request.user)
    except HodError as exc:
        return fail(str(exc), {"hod_email": str(exc)})
    except IntegrityError:
        return fail("A department with that name or code already exists.")

    ActivityLog.log(request, action="DEPARTMENT_SAVED", detail=dept.name)
    return ok({"id": dept.id, "reactivated": reactivated},
              message=_saved_message(reactivated, invited,
                                     form.cleaned_data.get("hod_email")))


def _create_department(request):
    """
    The discipline-first create path.

    Split out because creating and editing now ask different questions:
    creating decides *where the department comes from*, editing only ever
    changes what is already there.
    """
    discipline = (request.POST.get("discipline") or "").strip()
    institute = _target_institute(request)
    if institute is None:
        return fail("Choose an institute first.")
    if discipline not in Discipline.values:
        return fail("Choose a discipline.", {"discipline": "This field is required."})

    held = {a.discipline: a for a in institute.affiliations.all()}
    affiliation = held.get(discipline)
    if affiliation is None:
        return fail(
            "Your institute does not teach that discipline. Add it under "
            "Profile & security first.", {"discipline": "Not on your record."})

    hod_email = (request.POST.get("hod_email") or "").strip()

    if affiliation.university_id is not None:
        # Affiliated: adopt one of the university's, do not invent one.
        entry_id = clean_object_id(request.POST.get("catalogue_entry") or "")
        entry = catalogue.choices_for(institute, discipline).filter(
            pk=entry_id).first() if entry_id else None
        if entry is None:
            return fail(
                "Choose one of the departments your university publishes for "
                "this discipline.",
                {"catalogue_entry": "This field is required."})
        try:
            with transaction.atomic():
                department = catalogue.adopt(institute=institute, entry=entry,
                                             actor=request.user)
                invited = _set_hod(department, hod_email, request.user)
        except HodError as exc:
            return fail(str(exc), {"hod_email": str(exc)})
        ActivityLog.log(request, action="DEPARTMENT_ADOPTED",
                        detail=f"{institute.name}: {entry.code}")
        message = f"{entry.name} added, with everything your university publishes for it."
        if invited:
            message += f" Invitation emailed to {hod_email}."
        return ok({"id": department.id}, message=message)

    # Autonomous: nobody publishes one, so the institute writes it.
    form = DepartmentForm(request.POST, user=request.user, institute=institute)
    if not form.is_valid():
        return fail("Please correct the highlighted fields.", form_errors(form))
    try:
        with transaction.atomic():
            department = form.save(commit=False)
            department.institute = institute
            department.save()
            invited = _set_hod(department, hod_email, request.user)
    except HodError as exc:
        return fail(str(exc), {"hod_email": str(exc)})
    except IntegrityError:
        return fail("A department with that name or code already exists.")
    ActivityLog.log(request, action="DEPARTMENT_SAVED", detail=department.name)
    message = "Department added."
    if invited:
        message += f" Invitation emailed to {hod_email}."
    return ok({"id": department.id}, message=message)


def _set_hod(department, email, actor):
    from .services import assign_hod

    _, invited = assign_hod(department, email, actor=actor)
    return invited


def _saved_message(reactivated, invited, hod_email):
    if reactivated is not None:
        touched = ", ".join(f"{n} {noun}" for noun, n in (
            ("subjects", reactivated["subjects"]),
            ("batches", reactivated["batches"]),
            ("students", reactivated["students"]),
            ("teachers", reactivated["teachers"])) if n)
        message = ("Department reactivated"
                   + (f", along with {touched}." if touched else ".")
                   + " Archive anything that should stay hidden.")
    else:
        message = "Department saved."
    if invited:
        message += f" Invitation emailed to {hod_email}."
    return message


# --------------------------------------------------------------------------- #
#  Restored after a refactor removed them by accident.
#
#  These three sat between `api_department_save` and `_department_counts`,
#  and a replacement that sliced between those two names took them with it.
#  Nothing failed at import — the URLconf resolves lazily — so the first
#  sign was a 500 on a page nobody had opened since.
# --------------------------------------------------------------------------- #
@role_required(HEAD)
@require_POST
def api_department_delete(request, pk):
    dept = get_object_or_404(departments_for(request.user), pk=pk)
    if dept.students.exists() or dept.subjects.exists():
        # Archiving rather than deleting: the rows underneath carry attendance,
        # and `status` is the source of truth that `is_active` mirrors.
        dept.status = RowStatus.ARCHIVED
        dept.is_active = False
        dept.save(update_fields=["status", "is_active"])
        return ok(message="Department archived (it still holds academic records).")
    name = dept.name
    dept.delete()
    ActivityLog.log(request, action="DEPARTMENT_DELETED", detail=name)
    return ok(message="Department removed.")


@role_required(HEAD, HOD)
@require_GET
def api_invitations(request):
    qs = Invitation.objects.filter(institute=request.user.institute).select_related("department")
    if request.user.is_hod:
        qs = qs.filter(department=request.user.department)
    role = request.GET.get("role")
    if role:
        qs = qs.filter(role=role)
    status = request.GET.get("status")
    if status:
        qs = qs.filter(status=status)
    rows = [{
        "id": i.id, "email": i.email, "full_name": i.full_name, "role": i.role,
        "department": i.department.name if i.department else "",
        "status": "EXPIRED" if (i.status == "PENDING" and i.is_expired) else i.status,
        "created_at": i.created_at.strftime("%d %b %Y, %H:%M"),
        "expires_at": i.expires_at.strftime("%d %b %Y, %H:%M"),
        "sent_count": i.sent_count,
    } for i in qs[:500]]
    return ok({"rows": rows})


@role_required(HEAD, HOD)
@require_POST
def api_invitation_revoke(request, pk):
    inv = get_object_or_404(
        Invitation.objects.filter(institute=request.user.institute), pk=pk
    )
    if inv.status == Invitation.Status.ACCEPTED:
        return fail("That invitation has already been accepted.")
    inv.status = Invitation.Status.REVOKED
    inv.save(update_fields=["status"])
    return ok(message=f"Invitation to {inv.email} revoked.")


@role_required(HEAD, HOD)
@require_POST
def api_invitation_resend(request, pk):
    inv = get_object_or_404(
        Invitation.objects.filter(institute=request.user.institute), pk=pk
    )
    if request.user.is_hod and inv.department_id != request.user.department_id:
        return fail("You can only manage invitations in your own department.", status=403)
    if inv.status == Invitation.Status.ACCEPTED:
        return fail("That invitation has already been accepted.")
    inv.refresh_token()
    send_invitation(inv)
    return ok(message=f"Invitation re-sent to {inv.email}.")


@role_required(HEAD, HOD, TEACHER, STUDENT)
@require_GET
def api_subjects(request):
    qs = subjects_for(request.user).select_related(
        "department", "department__institute",
        "source__department__university")
    dept_id = request.GET.get("department")
    if dept_id:
        qs = qs.filter(department_id=dept_id)
    batch_id = request.GET.get("batch")
    if batch_id:
        qs = qs.filter(assignments__batch_id=batch_id).distinct()
    # Unknown values are ignored rather than matched: a stale bookmark should
    # show everything, not an empty list that looks like "no subjects".
    subject_type = (request.GET.get("subject_type") or "").strip().upper()
    if subject_type in SubjectType.values:
        qs = qs.filter(subject_type=subject_type)
    degree = (request.GET.get("degree") or "").strip().upper()
    if degree in Degree.values:
        qs = qs.filter(degree=degree)
    qs = qs.annotate(
        teacher_count=Count(
            "assignments__teacher",
            filter=Q(assignments__batch__is_active=True, assignments__is_active=True),
            distinct=True),
        # Enrolled = active enrolments held by students of a live batch.
        student_count=Count(
            "enrollments",
            filter=Q(enrollments__is_active=True,
                     enrollments__student__batch__is_active=True),
            distinct=True),
    )
    subjects = list(qs)
    rows = [{
        "id": s.id, "code": s.code, "name": s.name, "semester": s.semester,
        "subject_type": s.subject_type,
        "subject_type_label": s.get_subject_type_display(),
        "degree": s.degree,
        "degree_label": s.get_degree_display(),
        "credits": s.credits, "department": s.department.name,
        "department_id": s.department_id,
        "department_code": s.department.code,
        "is_active": s.is_active,
        "teacher_count": s.teacher_count, "student_count": s.student_count,
        # The code, not the name — the column is an identifier and a full name
        # wraps. The name rides along for the tooltip.
        "institute": s.department.institute.code,
        "institute_name": s.department.institute.name,
        "discipline": s.department.discipline,
        "discipline_label": s.department.get_discipline_display(),
        # Status and revocation are independent — see core/enums.py.
        "status": _row_status(s),
        "state": effective_state(s),
        "revoked": s.is_revoked,
        # Adopted from the university's catalogue, so theirs to change.
        "owner": (s.source.department.university.short_name
                  or s.source.department.university.name) if s.source_id else "",
        "read_only": is_read_only(s, request.user),
    } for s in subjects]
    return ok({"rows": rows})


def _department_counts(departments, states=None):
    """
    How many live subjects, batches, students and teachers a department holds.

    **One rule for every department, whatever its own state.** A count reads
    `status` and nothing else — never `is_revoked`, never the department's
    status. That is the fix for the bug this whole split exists to solve: a
    revoked department reported *0 students* because "revoked" had overwritten
    "active" on the way to the screen, so counting active students found none.
    The department was full and the number said empty.

    An archived department is the same case in a different disguise. It was
    given a second rule ("count everything") to work around the same conflation,
    and that rule is gone too: its students are still active students, so they
    are still counted, and the number matches the table beside it.

    **Four separate queries, on purpose.** These were four filtered `Count`
    annotations on one queryset — four unrelated reverse relations walked in a
    single statement. On MongoDB that is a lookup-and-unwind per relation and
    the counts came back multiplied by each other's rows.
    """
    from django.db.models import Count

    from accounts.models import User as UserModel

    from core.enums import RowStatus

    from .models import Batch, StudentProfile, Subject

    ids = [d.pk for d in departments]
    blank = {"subjects": 0, "batches": 0, "students": 0, "teachers": 0}
    out = {pk: dict(blank) for pk in ids}
    if not ids:
        return out

    def tally(queryset, key):
        for row in (queryset.exclude(status=RowStatus.ARCHIVED)
                    .filter(department_id__in=ids)
                    .values("department_id").annotate(n=Count("id"))):
            out[row["department_id"]][key] = row["n"]

    tally(Subject.objects.all(), "subjects")
    tally(Batch.objects.all(), "batches")
    # A student also needs a live cohort. That is not a relabelling — the
    # archived batch is a different fact about a different row — and the
    # Students table hides them for the same reason, so the number matches
    # what is on screen beneath it.
    tally(StudentProfile.objects.filter(batch__is_active=True), "students")
    tally(UserModel.objects.filter(role=UserModel.Role.TEACHER), "teachers")
    return out


def _row_status(row):
    """
    The status string a table renders for one row.

    `REVOKED` when the flag is set, whatever the status underneath — that is
    the fact which explains the row, and the pill has to lead with it. Anything
    else is the row's own status, unmodified. Nothing here consults the
    department: a student in an archived department is still an active student,
    and saying otherwise is what made every count wrong.
    """
    from core.enums import REVOKED_KEY, SUSPENDED_KEY

    # Suspension leads even over revocation. A revoked row is explained by its
    # discipline, which the person can see in the next column; a suspension is
    # a decision about this one person and is the only thing on the row that
    # somebody has to act on.
    if getattr(row, "is_suspended", False):
        return SUSPENDED_KEY
    if getattr(row, "is_revoked", False):
        return REVOKED_KEY
    return row.status


def _target_institute(request, instance=None):
    """
    Which institute a department belongs to.

    A university has none of its own, so it uses the one it is focused on (or
    the row's, when editing). `request.user.institute` alone was None for a
    university and produced a department attached to nothing.
    """
    if instance is not None:
        return instance.institute
    if request.user.is_university:
        from accounts.scoping import active_institute

        return active_institute(request)
    return request.user.institute


def _refuse_activation_in_a_dead_department(department, wants_active):
    """
    Message to refuse with, or None.

    A row cannot be made active inside an archived or revoked department. Not
    because its status would be overruled — statuses stand on their own now —
    but because the row would then claim to be running inside something that is
    not, and every screen would have to explain the contradiction.

    Deactivating is always allowed: nothing about a dead department makes
    switching a row *off* incoherent.
    """
    from core.enums import RowStatus

    if not wants_active or department is None:
        return None
    if department.is_revoked:
        return ("That department's discipline is no longer on your record, so "
                "nothing in it can be made active. Put the department back "
                "into a live discipline first.")
    if department.status == RowStatus.ARCHIVED:
        return ("That department is archived, so nothing in it can be made "
                "active. Restore the department first, or choose another one.")
    return None


def _department_for_save(request, instance):
    """
    Which department a subject or batch should end up in.

    Explicit or unchanged, never guessed. `_scoped_department` falls back to
    the *first* department in scope when nothing is posted, which is the right
    default for a create and completely wrong for an edit — a HoD saving a
    subject from a form that has no department field would silently move it.
    """
    posted = (request.POST.get("department") or "").strip()
    if posted:
        return _scoped_department(request, posted)
    if instance is not None:
        return instance.department
    return _scoped_department(request)


@role_required(HEAD, HOD, UNIVERSITY)
@require_POST
def api_subject_save(request, pk=None):
    instance = get_object_or_404(Subject, pk=pk, department__in=departments_for(request.user)) if pk else None

    # A university writes the curriculum, not one institute's copy of it.
    # Checked before the form so that an institute editing a row it does not
    # own is refused on the rule rather than on a validation error.
    if instance is not None:
        try:
            assert_writable(instance, request.user)
        except PermissionError as exc:
            return fail(str(exc), status=403)

    # Creating one *inside* an adopted department is refused too — the twin of
    # the batch rule. The university publishes that department's papers; a
    # college adding its own beside them would teach a subject nobody else
    # running the same syllabus has, and the marks would have nowhere to go.
    dept = _department_for_save(request, instance)
    if instance is None and dept is not None and dept.source_id is not None:
        return fail(
            "Your affiliating university publishes the subjects for this "
            "department. Add subjects under a department you run yourself.",
            {"department": "Set by your university."}, status=403)

    form = SubjectForm(request.POST, instance=instance)
    if not form.is_valid():
        return fail("Please correct the highlighted fields.", form_errors(form))
    refusal = _refuse_activation_in_a_dead_department(
        dept, form.cleaned_data.get("is_active"))
    if refusal:
        return fail(refusal, {"is_active": refusal})
    subject = form.save(commit=False)
    # Editing may now move a subject between departments. It could not before,
    # which left a subject stranded in a revoked department with no way out.
    # `is_active` comes straight from the form's checkbox: ticked means active
    # on arrival, unticked means archived — whatever state it was in before.
    subject.department = dept
    try:
        subject.save()
    except IntegrityError:
        return fail("A subject with that code already exists in this department.")
    ActivityLog.log(request, action="SUBJECT_SAVED", detail=str(subject))
    return ok({"id": subject.id}, message="Subject saved.")


@role_required(HEAD, HOD, UNIVERSITY)
@require_POST
def api_subject_delete(request, pk):
    subject = get_object_or_404(Subject, pk=pk, department__in=departments_for(request.user))
    try:
        assert_writable(subject, request.user)
    except PermissionError as exc:
        return fail(str(exc), status=403)
    # The push-model branch that used to sit here — "remove it from every
    # institute at once" — is gone. A university withdraws a paper from its
    # catalogue now, and the change reaches the colleges from there. This
    # endpoint only ever deals with one college's own row, which
    # `assert_writable` above has already established it may touch.
    if subject.assignments.exists() or subject.enrollments.exists():
        subject.is_active = False
        subject.save(update_fields=["is_active"])
        return ok(message="Subject deactivated (it is linked to teachers or students).")
    subject.delete()
    return ok(message="Subject removed.")


# --------------------------------------------------------------------------- #
#  Batches
# --------------------------------------------------------------------------- #
@role_required(HEAD, HOD, TEACHER, STUDENT, UNIVERSITY)
@require_GET
def api_batches(request):
    # The only screen that shows archived batches — it is where they are revived.
    qs = batches_for(request.user, include_inactive=True).select_related(
        "department", "department__institute", "source__department__university"
    ).annotate(n_students=Count("students", distinct=True))
    dept_id = request.GET.get("department")
    if dept_id:
        qs = qs.filter(department_id=dept_id)
    batches = list(qs)
    states = department_states({b.department for b in batches})
    rows = [{
        "id": b.id, "label": b.label, "start_year": b.start_year, "end_year": b.end_year,
        "department": b.department.name, "department_id": b.department_id,
        "department_code": b.department.code,
        "student_count": b.n_students, "is_active": b.is_active,
        "institute": b.department.institute.code,
        "institute_name": b.department.institute.name,
        "discipline": b.department.discipline,
        "discipline_label": b.department.get_discipline_display(),
        # Who publishes it, read from the catalogue link rather than the old
        # push-model stamp — the link *is* the claim now.
        "owner": (b.source.department.university.short_name
                  or b.source.department.university.name) if b.source_id else "",
        "read_only": is_read_only(b, request.user),
        "status": _row_status(b),
        "state": effective_state(b),
        "revoked": b.is_revoked,
    } for b in batches]
    return ok({"rows": rows})


@role_required(HEAD, HOD, UNIVERSITY)
@require_POST
def api_batch_save(request, pk=None):
    instance = get_object_or_404(Batch, pk=pk, department__in=departments_for(request.user)) if pk else None
    if instance is not None:
        try:
            assert_writable(instance, request.user)
        except PermissionError as exc:
            return fail(str(exc), status=403)
    # Creating one *inside* an adopted department is refused too. The
    # university publishes that department's cohorts; a college adding its own
    # beside them would produce a batch nobody else running the same syllabus
    # has, which is the disagreement the catalogue exists to prevent.
    dept = _department_for_save(request, instance)
    if instance is None and dept is not None and dept.source_id is not None:
        return fail(
            "Your affiliating university publishes the cohorts for this "
            "department. Add batches under a department you run yourself.",
            {"department": "Set by your university."}, status=403)
    form = BatchForm(request.POST, instance=instance)
    if not form.is_valid():
        return fail("Please correct the highlighted fields.", form_errors(form))
    refusal = _refuse_activation_in_a_dead_department(
        dept, form.cleaned_data.get("is_active"))
    if refusal:
        return fail(refusal, {"is_active": refusal})
    batch = form.save(commit=False)
    batch.department = dept
    batch.start_year = form.cleaned_data["start_year"]
    batch.end_year = form.cleaned_data["end_year"]
    try:
        batch.save()
    except IntegrityError:
        return fail("That batch already exists in this department.")
    return ok({"id": batch.id}, message="Batch saved.")


@role_required(HEAD, HOD, UNIVERSITY)
@require_POST
def api_batch_toggle(request, pk):
    """
    Archive or restore a batch.

    Archiving hides the cohort — its students, sessions, records and every
    statistic derived from them — across the whole application. Nothing is
    deleted, so restoring brings all of it straight back.
    """
    batch = get_object_or_404(Batch, pk=pk, department__in=departments_for(request.user))
    try:
        assert_writable(batch, request.user)
    except PermissionError as exc:
        return fail(str(exc), status=403)
    # Archiving everywhere is the catalogue's job now — see
    # `catalogue_views.api_batch_toggle`, which does reach every college
    # running the cohort. Here it is one college's own batch.
    batch.is_active = not batch.is_active
    batch.status = RowStatus.ACTIVE if batch.is_active else RowStatus.ARCHIVED
    batch.save(update_fields=["is_active", "status"])
    ActivityLog.log(request, action="BATCH_TOGGLED",
                    detail=f"{batch.label} {'restored' if batch.is_active else 'archived'}")
    students = batch.students.count()
    if batch.is_active:
        message = f"{batch.label} restored — its {students} student(s) are visible again."
    else:
        message = (f"{batch.label} archived. Its {students} student(s) and all their "
                   "records are now hidden everywhere.")
    return ok({"is_active": batch.is_active}, message=message)


@role_required(HEAD, HOD, UNIVERSITY)
@require_POST
def api_batch_delete(request, pk):
    batch = get_object_or_404(Batch, pk=pk, department__in=departments_for(request.user))
    try:
        assert_writable(batch, request.user)
    except PermissionError as exc:
        return fail(str(exc), status=403)
    if batch.students.exists():
        batch.is_active = False
        batch.save(update_fields=["is_active"])
        return ok(message="Batch archived (students are still linked to it).")
    batch.delete()
    return ok(message="Batch removed.")


# --------------------------------------------------------------------------- #
#  Teachers & assignments
# --------------------------------------------------------------------------- #
def _assignment_rows(teacher):
    """Allocations to archived batches are hidden along with the batch."""
    return [{
        "id": a.id,
        "subject_id": a.subject_id,
        "subject": f"{a.subject.code} — {a.subject.name}",
        "subject_type": a.subject.subject_type,
        "degree": a.subject.degree,
        # For the Semester filter. Read from the allocation rather than the
        # teacher, who has no semester of their own — the same reading the
        # subject-type and degree filters beside it already use.
        "semester": a.subject.semester,
        "batch_id": a.batch_id,
        "batch": a.batch.label,
        # Blank means the whole batch — see academics/allocation.py. The chip
        # prints "2022-26" for that and "2022-26 · A" for a section, so the two
        # read differently at a glance.
        "section_id": a.section_id,
        "section": a.section.name if a.section_id else "",
        # Derived, never stored. The screens show it; the row does not keep it.
        "department": a.subject.department.code,
    } for a in teacher.assignments.select_related(
        "subject", "subject__department", "batch", "section").filter(
        is_active=True, batch__is_active=True)]


@role_required(HEAD, HOD, TEACHER, STUDENT, GUARDIAN, UNIVERSITY)
@guardian_readonly
@require_GET
def api_teachers(request):
    # Read scope: staff see the whole institute. Editing is decided per row by
    # `can_edit` below, and enforced independently by the write endpoints.
    qs = visible_teachers_for(request.user).select_related(
        "department", "suspended_by").prefetch_related(
        Prefetch("assignments", queryset=TeacherAssignment.objects.select_related("subject", "batch"))
    )
    dept_id = clean_object_id(request.GET.get("department"))
    if dept_id:
        qs = qs.filter(department_id=dept_id)
    # Invitation ids are only useful for resending, which teachers cannot do —
    # so don't hand them out.
    can_manage = request.user.role in (HEAD, HOD)
    manageable = manageable_department_ids(request.user)   # None = every department
    # Hiding the column client-side while still shipping the number in the JSON
    # would not be hiding it at all, so students never receive it.
    show_mobile = request.user.role not in (STUDENT, GUARDIAN)
    # **The reason never reaches a student or a guardian.** The suspension
    # itself is visible — a teacher who cannot sign in is a fact the directory
    # should not hide — but why somebody was suspended is a staff matter
    # between the university, the institute and the person. Withheld from the
    # payload rather than hidden in the browser, because hiding it client-side
    # would not be hiding it at all: the same mistake the mobile column above
    # was written to avoid.
    # Also gates the PAN and date of birth below. One flag rather than three
    # identical ones, and named for what it decides rather than for the first
    # thing that used it: staff-only detail about a member of staff.
    staff_detail = show_reason = request.user.role not in (STUDENT, GUARDIAN)
    pending = {}
    if can_manage:
        pending = {
            i.email: i.id
            for i in Invitation.objects.filter(role=TEACHER, status=Invitation.Status.PENDING,
                                               institute=request.user.institute)
        }
    teachers = list(qs)
    states = department_states(
        {t.department for t in teachers if t.department_id})
    rows = [{
        "id": t.id,
        "full_name": t.full_name or "(awaiting registration)",
        "email": t.email,
        "phone": t.phone if show_mobile else "",
        "phone_dial": dial(t.phone) if show_mobile else None,
        "department": t.department.name if t.department else "",
        "department_id": t.department_id,
        "institute": t.institute.code if t.institute else "",      # the client filters on this
        "institute_name": t.institute.name if t.institute else "",
        "discipline": t.department.discipline if t.department_id else "",
        "discipline_label": (t.department.get_discipline_display()
                             if t.department_id else ""),
        "status": _row_status(t),
        "state": effective_state(t),
        "revoked": t.is_revoked,
        "is_active": t.is_active,
        # Masked, and only for staff. A PAN is a national identifier; the
        # table needs it to say "this row is identified" and to let somebody
        # spot a duplicate, neither of which needs the whole number.
        "pan": pan_rules.masked(t.pan_number) if staff_detail else "",
        "has_pan": bool(t.pan_number),
        "date_of_birth": (t.date_of_birth.strftime("%d %b %Y")
                          if t.date_of_birth and staff_detail else ""),
        "date_of_birth_value": (t.date_of_birth.isoformat()
                                if t.date_of_birth and staff_detail else ""),
        "suspended": t.is_suspended,
        "suspension_reason": t.suspension_reason if show_reason else "",
        # Date *and* time. "Suspended on 13 Aug" is ambiguous on the day it
        # happens, which is the day somebody is most likely to be asking.
        "suspended_at": (localtime(t.suspended_at).strftime("%d %b %Y, %H:%M")
                         if t.suspended_at else ""),
        "suspended_by": ((t.suspended_by.short_name or t.suspended_by.name)
                         if t.suspended_by_id else ""),
        # Mirrors `suspension.may_suspend`, so the button the browser shows
        # matches what the server will actually accept. False for an institute
        # throughout — this is not their decision to take.
        "can_suspend": can_suspend(request.user, t),
        "can_lift": (request.user.is_university
                     and t.is_suspended
                     and t.suspended_by_id == request.user.university_id),
        # Mirrors what teachers_for() would allow, so the buttons the browser
        # shows match what the server will actually accept.
        # Mirrors the two guards above, so a locked row shows as locked rather
        # than offering buttons that come back 403.
        "can_edit": (can_manage
                     and (manageable is None or t.department_id in manageable)
                     and suspension.may_manage(request.user, t)),
        "frozen": t.is_suspended and not request.user.is_university,
        "invitation_id": pending.get(t.email),
        "assignments": _assignment_rows(t),
    } for t in teachers]
    return ok({"rows": rows})


@role_required(HEAD, HOD, UNIVERSITY)
@require_POST
def api_teacher_invite(request):
    dept = _scoped_department(request, request.POST.get("department") or None)
    form = TeacherInviteForm(request.POST)
    if not form.is_valid():
        return fail("Please correct the highlighted fields.", form_errors(form))
    try:
        pairs = json.loads(request.POST.get("assignments") or "[]")
    except ValueError:
        return fail("Assignments payload is malformed.")
    if not pairs:
        return fail("Assign at least one subject + batch to the teacher.",
                    {"assignments": "Pick at least one subject and batch."})

    # Subject + batch + section, validated against this department in one
    # place — `academics.allocation.resolve_pairs`. Both this endpoint and the
    # edit one below call it, so an allocation cannot mean one thing when a
    # teacher is invited and another when they are edited.
    resolved, pair_error = allocation.resolve_pairs(pairs, dept)
    if pair_error:
        return fail(pair_error, status=403)

    # **Before the account exists, not after.** The gate ends in a call to an
    # external provider, and half-creating a teacher and then discovering their
    # PAN is spoken for would leave a row nobody asked for. `exclude_pk` is not
    # passed: there is no row yet, so every holder counts.
    try:
        pan = pan_rules.assert_can_hold(
            pan=form.cleaned_data["pan_number"],
            name=form.cleaned_data["full_name"],
            date_of_birth=form.cleaned_data["date_of_birth"])
    except pan_rules.PanError as exc:
        return fail(str(exc), {exc.field: str(exc)}, status=403)

    with transaction.atomic():
        user, invitation, _ = invite_user(
            email=form.cleaned_data["email"],
            role=TEACHER,
            institute=request.user.institute,
            department=dept,
            full_name=form.cleaned_data.get("full_name", ""),
            invited_by=request.user,
            payload={"assignments": pairs},
            extra_lines=["Assigned: " + ", ".join(
                f"{subject.code} · {batch.label}"
                + (f" · {section.name}" if section else "")
                for subject, batch, section in resolved
            )],
        )
        if invitation is None and not user.is_teacher:
            return fail("That email already belongs to a non-teacher account.")
        # Recorded on the row the invite created or reused. Reusing a row that
        # already carries a *different* PAN would be re-identifying somebody,
        # so it is refused rather than overwritten.
        if user.pan_number and user.pan_number != pan:
            return fail(
                f"{user.email} is already on file with a different PAN. "
                "Archive that account and add the teacher again if this is a "
                "different person.", {"email": "Already has another PAN."},
                status=403)
        pan_rules.record(user, pan=pan,
                         date_of_birth=form.cleaned_data["date_of_birth"],
                         actor=request.user)
        if form.cleaned_data.get("phone"):
            user.phone = form.cleaned_data["phone"]
            user.save(update_fields=["phone"])
        allocation.set_allocations(user, resolved, actor=request.user)
    ActivityLog.log(request, action="TEACHER_INVITED", detail=user.email)
    note = " Invitation emailed." if invitation else " (Account already active — assignments updated.)"
    return ok({"id": user.id}, message="Teacher saved." + note)


@role_required(HEAD, HOD, UNIVERSITY)
@require_POST
def api_teacher_assignments_save(request, pk):
    """
    Update a teacher: name, mobile, department (head only) and allocations.

    `teachers_for` is the scope gate — a HoD asking for a teacher outside their
    department gets a 404 here, which is what keeps "HoDs manage only their own
    department" true regardless of what the browser sends.
    """
    teacher = get_object_or_404(teachers_for(request.user), pk=pk)
    # A suspended teacher's record is evidence while the sanction stands, and
    # is not the institute's to amend. Checked before anything is read off the
    # request so a refusal cannot depend on what was posted.
    if not suspension.may_manage(request.user, teacher):
        return fail(suspension.manage_refusal(teacher), status=403)
    is_head = request.user.role == HEAD

    dept = teacher.department or _scoped_department(request)
    # Compared as strings so this does not depend on what a primary key looks
    # like. Refusing (rather than quietly ignoring) an unwanted move matters:
    # silently keeping the old department would tell the user it worked.
    requested = (request.POST.get("department") or "").strip()
    if requested and requested != str(dept.pk if dept else ""):
        # Only the head may move a teacher between departments. A HoD doing it
        # would hand the teacher to another HoD and lose the ability to undo it.
        if not is_head:
            return fail("Only the head of the institute can move a teacher "
                        "to another department.", status=403)
        try:
            target = departments_for(request.user).filter(pk=requested).first()
        except (DjangoValidationError, ValueError, TypeError):
            target = None
        if target is None:
            return fail("Please correct the highlighted fields.",
                        {"department": "That department is not one you manage."})
        dept = target

    full_name = (request.POST.get("full_name") or "").strip()
    phone = (request.POST.get("phone") or "").strip()
    if phone:
        normalised, phone_error = normalise_msisdn(phone)
        if phone_error:
            return fail("Please correct the highlighted fields.",
                        {"phone": f"That mobile number {phone_error}."})
        phone = normalised or phone

    # PAN and date of birth are fixed once on file — they are the answer to
    # "who is this person", and a screen that could edit them could move a
    # teacher's history onto somebody else. A row that predates this carries
    # neither, and filling them in is the only way it ever gets one; that path
    # runs the full gate, including the KYC call.
    posted_pan = (request.POST.get("pan_number") or "").strip()
    posted_dob = (request.POST.get("date_of_birth") or "").strip()
    dob = parse_date(posted_dob) if posted_dob else None
    try:
        pan_rules.assert_immutable(teacher, pan=posted_pan, date_of_birth=dob)
        filling_in = posted_pan and not teacher.pan_number
        if filling_in:
            pan_rules.assert_can_hold(
                pan=posted_pan, name=full_name or teacher.full_name,
                date_of_birth=dob, exclude_pk=teacher.pk)
    except pan_rules.PanError as exc:
        return fail(str(exc), {exc.field: str(exc)}, status=403)

    try:
        pairs = json.loads(request.POST.get("assignments") or "[]")
    except ValueError:
        return fail("Assignments payload is malformed.")
    # Resolved before the transaction opens: a bad payload should refuse
    # without having half-written a name change.
    resolved, pair_error = allocation.resolve_pairs(pairs, dept)
    if pair_error:
        return fail(pair_error, status=403)

    with transaction.atomic():
        changed = []
        if full_name and full_name != teacher.full_name:
            teacher.full_name = full_name
            changed.append("full_name")
        if phone != teacher.phone:
            teacher.phone = phone
            changed.append("phone")
        if dept and teacher.department_id != dept.pk:
            teacher.department = dept
            changed.append("department")
        if filling_in:
            teacher.pan_number = pan_rules.normalise(posted_pan)
            teacher.date_of_birth = dob
            changed += ["pan_number", "date_of_birth"]
        if changed:
            teacher.save(update_fields=changed)
        # Anything not resubmitted is retired — see `set_allocations`. That
        # also cleans up after a department move: allocations to the old
        # department's subjects cannot be in `pairs`, since `resolve_pairs`
        # validates against `dept`, so they deactivate rather than lingering as
        # invalid cross-department links.
        allocation.set_allocations(teacher, resolved, actor=request.user)
    ActivityLog.log(request, action="TEACHER_UPDATED", detail=teacher.email)
    return ok({"assignments": _assignment_rows(teacher)}, message="Teacher updated.")


@role_required(HEAD, HOD, UNIVERSITY)
@require_POST
def api_teacher_toggle(request, pk):
    teacher = get_object_or_404(teachers_for(request.user), pk=pk)
    # **Frozen in both directions.** Re-activating would put them back on the
    # rota with the bar still standing. Deactivating is refused for a sharper
    # reason: archiving releases the PAN, so a college able to archive a
    # suspended teacher could hand them to the next college and the sanction
    # would follow nobody. See accounts/suspension.may_manage.
    if not suspension.may_manage(request.user, teacher):
        return fail(suspension.manage_refusal(teacher), status=403)
    # Reactivation asks the PAN question from the other end: while this teacher
    # was archived another college may have taken them on, and switching them
    # back on here would put one person on two payrolls. Archiving is never
    # blocked — releasing somebody is always allowed.
    if not teacher.is_active:
        try:
            pan_rules.assert_can_reactivate(teacher)
        except pan_rules.PanError as exc:
            return fail(str(exc), {exc.field: str(exc)}, status=403)
    teacher.is_active = not teacher.is_active
    teacher.save(update_fields=["is_active"])
    state = "re-activated" if teacher.is_active else "deactivated"
    ActivityLog.log(request, action="TEACHER_TOGGLED", detail=f"{teacher.email} {state}")
    return ok({"is_active": teacher.is_active}, message=f"{teacher.email} {state}.")


@role_required(UNIVERSITY)
@require_POST
def api_teacher_suspend(request, pk):
    """
    Suspend a teacher of an institute this university affiliates.

    Scoped by `visible_teachers_for` first — a university may only reach the
    institutes it affiliates at all — and then by `suspension.may_suspend`,
    which narrows it to the ones whose *department's discipline* is this
    university's. The two are not the same test, and the second is the one that
    matters: a college with engineering under one body and pharmacy under
    another has two affiliating universities.
    """
    teacher = get_object_or_404(visible_teachers_for(request.user), pk=pk)
    try:
        result = suspension.suspend(teacher=teacher,
                                    reason=request.POST.get("reason"),
                                    actor=request.user)
    except suspension.SuspensionError as exc:
        return fail(str(exc), status=403)

    told = len(result["notified"])
    return ok(result, message=(
        f"{teacher.get_full_name()} suspended. "
        + (f"The teacher, their head of department and the institute were "
           f"emailed ({told} address(es))." if told else
           "No deliverable address was on file, so nobody could be emailed — "
           "tell them another way.")))


@role_required(UNIVERSITY)
@require_POST
def api_teacher_lift_suspension(request, pk):
    """Clear a suspension. Only the body that imposed it may do so."""
    teacher = get_object_or_404(visible_teachers_for(request.user), pk=pk)
    try:
        result = suspension.lift(teacher=teacher,
                                 reason=request.POST.get("reason"),
                                 actor=request.user)
    except suspension.SuspensionError as exc:
        return fail(str(exc), status=403)
    return ok(result, message=(
        f"Suspension on {teacher.get_full_name()} lifted. They can sign in "
        "again, and their classes and attendance are as they left them."))


# --------------------------------------------------------------------------- #
#  Students
# --------------------------------------------------------------------------- #
@role_required(HEAD, HOD, TEACHER, UNIVERSITY)
@require_GET
def api_students(request):
    # `scope=all` is the institute-wide directory. all_students_for() returns
    # nothing for a student account, so the parameter cannot widen anyone's
    # access beyond what their role already allows.
    wide = request.GET.get("scope") == "all"
    # The management screen, so archived and revoked departments are included:
    # listing those students with the right status is what the status column is
    # for. Reports use the same selectors with the default and get neither.
    base = (all_students_for(request.user, include_dead_departments=True) if wide
            else students_qs_for(request.user, include_dead_departments=True))
    qs = base.select_related(
        "user", "batch", "section", "department",
        "department__institute").prefetch_related(
        Prefetch("enrollments", queryset=Enrollment.objects.filter(is_active=True).select_related("subject"))
    )
    if request.GET.get("department"):
        qs = qs.filter(department_id=request.GET["department"])
    if request.GET.get("batch"):
        qs = qs.filter(batch_id=request.GET["batch"])
    # `none` is a real answer, not a missing filter: "who has not been put in a
    # section yet" is the question a head asks straight after an import.
    section = (request.GET.get("section") or "").strip()
    if section == "none":
        qs = qs.filter(section__isnull=True)
    elif section:
        qs = qs.filter(section_id=section)
    if request.GET.get("subject"):
        qs = qs.filter(enrollments__subject_id=request.GET["subject"], enrollments__is_active=True)
    search = (request.GET.get("q") or "").strip()
    if search:
        qs = qs.filter(
            Q(user__full_name__icontains=search)
            | Q(user__email__icontains=search)
            | Q(class_roll__icontains=search)
            | Q(exam_roll__icontains=search)
        )
    students = list(qs.distinct()[:2000])
    states = department_states({s.department for s in students})
    rows = [{
        "id": s.id,
        "user_id": s.user_id,
        "name": s.user.full_name or "(awaiting registration)",
        "email": s.user.email,
        "mobile": s.mobile or s.user.phone,
        "mobile_dial": dial(s.mobile or s.user.phone),
        "guardian_dial": dial(s.guardian_mobile),
        "guardian_name": s.guardian_name,
        "guardian_mobile": s.guardian_mobile,
        "guardian_email": s.guardian_email,
        "class_roll": s.class_roll,
        "exam_roll": s.exam_roll,
        "batch": s.batch.label,
        "batch_id": s.batch_id,
        "section": s.section.name if s.section_id else "",
        "section_id": s.section_id,
        "department": s.department.name,
        "department_id": s.department_id,
        "institute": s.department.institute.code,
        "institute_name": s.department.institute.name,
        "discipline": s.department.discipline,
        "discipline_label": s.department.get_discipline_display(),
        "subjects": [e.subject.code for e in s.enrollments.all()],
        # Code -> type, so the subject dropdown on this screen can group itself.
        # It is built from the rows rather than from api_lookups because this
        # table spans the institute while a teacher's lookups do not.
        "subject_types": {e.subject.code: e.subject.subject_type
                          for e in s.enrollments.all()},
        "subject_degrees": {e.subject.code: e.subject.degree
                            for e in s.enrollments.all()},
        "status": _row_status(s),
        "state": effective_state(s),
        "revoked": s.is_revoked,
        "is_active": s.is_active and s.user.is_active,
        "device_bound": bool(s.user.device_id),
        "device_bound_at": (
            timezone.localtime(s.user.device_bound_at).strftime("%d %b %Y, %H:%M")
            if s.user.device_bound_at else ""
        ),
        # The gate is hard, so staff need to see at a glance who is stuck
        # behind it and have the reset button to hand.
        "face_enrolled": bool(s.user.face_enrolled),
    } for s in students]
    return ok({"rows": rows, "count": len(rows)})


@role_required(HEAD, HOD, UNIVERSITY)
@require_POST
def api_students_import(request):
    # **No department argument any more.** It is a column in the sheet, so one
    # file can carry the whole college. What the caller supplies instead is the
    # scope: which institute, and which of its departments this account may
    # file students into — without that second half a Department column would
    # let a HoD put students in a department they do not run.
    institute = _target_institute(request)
    if institute is None:
        return fail("No institute is selected.", status=403)
    allowed = departments_for(request.user)
    if not allowed.exists():
        return fail("You do not manage any department to import into.",
                    status=403)
    upload = request.FILES.get("file")
    if upload is None:
        return fail("Please choose a spreadsheet to upload.", {"file": "This field is required."})
    if upload.size > 8 * 1024 * 1024:
        return fail("That file is larger than 8 MB.")
    rows, error = read_rows(upload)
    if error:
        return fail(error)
    if not rows:
        return fail("No data rows found in the file.")
    if len(rows) > 2000:
        return fail("Please split files larger than 2000 rows.")

    if request.POST.get("dry_run") == "1":
        # Run the real importer inside a transaction we deliberately roll back,
        # so the preview is byte-for-byte what a real import would do.
        class _Rollback(Exception):
            pass

        preview = {}
        try:
            with transaction.atomic():
                job = import_students(
                    rows, institute, request.user, upload.name,
                    send_invites=False, allowed_departments=allowed)
                preview = {
                    "counts": {
                        "total": job.total_rows, "created": job.created_count,
                        "updated": job.updated_count, "errors": job.error_count,
                    },
                    "rows": job.report["rows"],
                }
                raise _Rollback
        except _Rollback:
            pass
        return ok({"preview": True, **preview},
                  message="Preview only — nothing has been saved yet.")

    job = import_students(rows, institute, request.user, upload.name,
                          send_invites=True, allowed_departments=allowed)
    ActivityLog.log(
        request, action="STUDENTS_IMPORTED",
        detail=(f"{job.created_count} created / {job.updated_count} updated"
                f" · {', '.join(job.report.get('departments') or []) or 'none'}"))
    return ok({
        "job_id": job.id,
        "counts": {
            "total": job.total_rows, "created": job.created_count,
            "updated": job.updated_count, "errors": job.error_count,
        },
        "rows": job.report["rows"],
    }, message=(
        f"Imported {job.created_count} new and {job.updated_count} existing students. "
        f"{job.error_count} row(s) had problems."
    ))


@role_required(HEAD, HOD, UNIVERSITY)
@require_GET
def api_students_template(request):
    dept = current_department(request.user)
    stream = build_template_workbook(dept)
    return FileResponse(
        stream, as_attachment=True, filename="student_roster_template.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@role_required(HEAD, HOD, UNIVERSITY)
@require_GET
def api_import_jobs(request):
    # Scoped by institute, because a job is not one department's any more. The
    # `department` half of the filter is kept for the jobs uploaded before the
    # column existed, which carry a department and no institute — dropping it
    # would make that history vanish from the screen.
    from accounts.scoping import institutes_for

    scope = Q(institute__in=institutes_for(request.user))
    dept_ids = list(departments_for(request.user).values_list("id", flat=True))
    if dept_ids:
        scope |= Q(department_id__in=dept_ids)
    qs = ImportJob.objects.filter(scope).select_related(
        "uploaded_by", "department", "institute"
    )[:50]
    rows = [{
        "id": j.id, "file_name": j.file_name, "status": j.status,
        # The departments a file actually reached, from the report. Falls back
        # to the single department an older job recorded.
        "department": (", ".join(j.report.get("departments") or [])
                       or (j.department.name if j.department_id else "—")),
        "institute": j.institute.name if j.institute_id else "",
        "uploaded_by": j.uploaded_by.full_name if j.uploaded_by else "",
        "total": j.total_rows, "created": j.created_count,
        "updated": j.updated_count, "errors": j.error_count,
        "created_at": j.created_at.strftime("%d %b %Y, %H:%M"),
    } for j in qs]
    return ok({"rows": rows})


@role_required(HEAD, HOD, UNIVERSITY)
@require_GET
def api_import_job_detail(request, pk):
    from accounts.scoping import institutes_for

    # Same two-part scope as the list above — see the note there.
    scope = Q(institute__in=institutes_for(request.user))
    dept_ids = list(departments_for(request.user).values_list("id", flat=True))
    if dept_ids:
        scope |= Q(department_id__in=dept_ids)
    job = get_object_or_404(ImportJob.objects.filter(scope), pk=pk)
    return ok({"rows": job.report.get("rows", []),
               "sections_created": job.report.get("sections_created", []),
               "departments": job.report.get("departments", [])})


@role_required(HEAD, HOD, UNIVERSITY)
@require_POST
def api_student_save(request, pk):
    student = get_object_or_404(students_qs_for(request.user), pk=pk)
    form = StudentEditForm(request.POST)
    if not form.is_valid():
        return fail("Please correct the highlighted fields.", form_errors(form))
    batch = get_object_or_404(
        Batch, pk=form.cleaned_data["batch_id"],
        department=student.department, is_active=True,
    )
    # Resolved against the batch being *saved*, not the one the student is
    # leaving — otherwise moving somebody between cohorts and setting their
    # section in one go checks against the wrong list and lets a mismatch
    # through. `create=False`: a typo on a form should be refused, not become a
    # fourth section. Sections are created by an import, which is where a
    # spreadsheet is the source of truth.
    posted_section = (request.POST.get("section_id") or "").strip()
    try:
        if posted_section:
            section = get_object_or_404(Section, pk=clean_object_id(posted_section))
            sections.assert_in_batch(section, batch)
        else:
            section = None
    except sections.SectionError as exc:
        return fail(str(exc), {"section_id": str(exc)})

    student.batch = batch
    student.section = section
    student.mobile = form.cleaned_data.get("mobile", "")
    student.class_roll = form.cleaned_data.get("class_roll", "")
    student.exam_roll = form.cleaned_data.get("exam_roll", "")
    student.guardian_name = form.cleaned_data.get("guardian_name", "")
    student.guardian_mobile = form.cleaned_data["guardian_mobile"]
    student.guardian_email = form.cleaned_data.get("guardian_email", "")
    student.save()
    student.user.full_name = form.cleaned_data["full_name"]
    student.user.save(update_fields=["full_name"])

    subject_ids = clean_object_ids(request.POST.getlist("subjects[]"))
    if subject_ids:
        valid = list(Subject.objects.filter(
            id__in=subject_ids, department=student.department))
        # Materialised for the same reason as
        # `accounts.affiliations.archive_discipline_contents`: passing a
        # queryset here is a correlated subquery, and django_mongodb_backend
        # cannot express one inside an `update()` — Atlas rejects it with
        # "$in requires an array as a second argument, found: missing".
        # sqlite runs it, so no test catches it; only a real save does.
        Enrollment.objects.filter(student=student).exclude(
            subject_id__in=[s.pk for s in valid]
        ).update(is_active=False)
        for subj in valid:
            Enrollment.objects.update_or_create(
                student=student, subject=subj, defaults={"is_active": True}
            )
    return ok(message="Student updated.")


@role_required(HEAD, HOD, UNIVERSITY)
@require_POST
def api_student_toggle(request, pk):
    student = get_object_or_404(students_qs_for(request.user), pk=pk)
    student.is_active = not student.is_active
    student.save(update_fields=["is_active"])
    student.user.is_active = student.is_active
    student.user.save(update_fields=["is_active"])
    state = "re-activated" if student.is_active else "deactivated"
    return ok({"is_active": student.is_active}, message=f"{student.user.email} {state}.")


@role_required(HEAD, HOD, TEACHER, UNIVERSITY)
@require_POST
def api_student_reset_device(request, pk):
    """
    Release a student's device binding — the "he lost his phone" button.

    Available to the head, the HoD and any teacher — scoped by
    `all_students_for`, because a student who has lost their phone will ask
    whichever member of staff is nearby, not necessarily one who teaches them.
    The student is emailed and the action is logged, so a reset used to enable
    proxy attendance leaves a trail.
    """
    student = get_object_or_404(all_students_for(request.user), pk=pk)
    reason = (request.POST.get("reason") or "").strip()[:150]
    if not unlink_device(student.user, actor=request.user, request=request, reason=reason):
        return fail(f"{student.name} has no device linked at the moment.")
    return ok(
        {"device_bound": False},
        message=(f"Device unlinked for {student.name}. They can now mark attendance "
                 "from a new device, which will then become their registered one."),
    )


@role_required(HEAD, HOD, TEACHER, UNIVERSITY)
@require_GET
def api_student_face(request, pk):
    """
    What a student's enrolment holds, so staff can look at it.

    Metadata and per-image URLs only — the pictures themselves come from the
    view below, which re-checks who is asking. A face on file that nobody can
    look at is impossible to audit: you cannot tell an enrolment of the right
    student from one taken of a friend without seeing it.
    """
    student = get_object_or_404(all_students_for(request.user), pk=pk)
    enrolment = (FaceEnrolment.objects
                 .filter(user=student.user)
                 .prefetch_related("samples").first())
    samples = list(enrolment.samples.all()) if enrolment else []
    return ok({
        "student": student.name,
        "class_roll": student.class_roll,
        "captured_at": (timezone.localtime(enrolment.created_at).strftime("%d %b %Y, %H:%M")
                        if enrolment else ""),
        "model": enrolment.model_name if enrolment else "",
        "rows": [{
            "pose": s.pose,
            "label": s.get_pose_display(),
            "url": reverse("academics:api_student_face_image",
                           args=[student.pk, s.pose.lower()]),
            # The angle the server measured at enrolment, not what the browser
            # claimed — useful when a capture looks off.
            "yaw": round(s.yaw, 1),
        } for s in samples],
    })


@role_required(HEAD, HOD, TEACHER, UNIVERSITY)
@require_GET
def api_student_face_image(request, pk, pose):
    """
    One enrolment photo.

    Streamed through here rather than linked from the storage account so the
    permission check runs on every fetch: a URL copied out of the page is
    useless to anyone who cannot already see that student.
    """
    student = get_object_or_404(all_students_for(request.user), pk=pk)
    sample = get_object_or_404(
        FaceSample, enrolment__user=student.user, pose=(pose or "").upper())
    # Always image/jpeg: every one of these came from the capture canvas, which
    # only ever produces JPEG. Declared explicitly and with nosniff, so a file
    # that somehow was not one cannot be re-interpreted as something executable.
    response = FileResponse(sample.image.open("rb"), content_type="image/jpeg")
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Disposition"] = (
        f'inline; filename="{student.class_roll or student.pk}-{sample.pose.lower()}.jpg"')
    return response


@role_required(HEAD, HOD, TEACHER, UNIVERSITY)
@require_POST
def api_student_reset_face(request, pk):
    """
    Let a student capture their face again.

    Same reach as the device unlink — head, HoD or any teacher via
    `all_students_for` — because the student will ask whoever is nearby, and a
    stale template rejecting the real person is not a reason to make them walk
    across campus. Logged, because a reset is also how a proxy enrolment would
    begin.
    """
    student = get_object_or_404(all_students_for(request.user), pk=pk)
    reason = (request.POST.get("reason") or "").strip()[:200]
    cleared = face_service.clear(
        user=student.user, actor=request.user, reason=reason, request=request)
    if not cleared:
        return fail(f"{student.name} has not captured a face yet.")
    return ok(
        {"face_enrolled": False},
        message=(f"Face cleared for {student.name}. They will be asked to "
                 "capture it again next time they sign in."),
    )


@role_required(HEAD, HOD, UNIVERSITY)
@require_POST
def api_student_resend(request, pk):
    student = get_object_or_404(students_qs_for(request.user), pk=pk)
    if student.user.registration_completed:
        return fail("That student has already completed registration.")
    inv = Invitation.objects.filter(email=student.user.email).order_by("-created_at").first()
    if inv is None:
        _, inv, _ = invite_user(
            email=student.user.email, role=STUDENT, institute=request.user.institute,
            department=student.department, full_name=student.user.full_name,
            invited_by=request.user, send=True,
        )
    else:
        inv.refresh_token()
        send_invitation(inv)
    return ok(message=f"Invitation re-sent to {student.user.email}.")


@role_required(HEAD, HOD, TEACHER, UNIVERSITY)
@require_GET
def api_students_export(request):
    """Download the current roster in the same shape the importer accepts."""
    # `department__institute` for the Institute column the export now carries —
    # without it this was one extra query per student.
    qs = students_qs_for(request.user).select_related(
        "user", "batch", "section", "department", "department__institute"
    ).prefetch_related(
        Prefetch("enrollments", queryset=Enrollment.objects.filter(is_active=True).select_related("subject"))
    )
    if request.GET.get("department"):
        qs = qs.filter(department_id=request.GET["department"])
    if request.GET.get("batch"):
        qs = qs.filter(batch_id=request.GET["batch"])
    # The export mirrors the table's filters, so "what I am looking at" and
    # "what I download" are the same list.
    section = (request.GET.get("section") or "").strip()
    if section == "none":
        qs = qs.filter(section__isnull=True)
    elif section:
        qs = qs.filter(section_id=section)
    if request.GET.get("subject"):
        qs = qs.filter(enrollments__subject_id=request.GET["subject"], enrollments__is_active=True)
    stream = build_roster_workbook(qs.distinct())
    return FileResponse(
        stream, as_attachment=True, filename="student_roster.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# --------------------------------------------------------------------------- #
#  Shared lookups used by every dropdown in the UI
# --------------------------------------------------------------------------- #
@role_required(HEAD, HOD, TEACHER, STUDENT, UNIVERSITY)
@require_GET
def api_lookups(request):
    user = request.user
    # Every dropdown in the browser is built from this, so filtering here is
    # what keeps archived and revoked departments out of all of them at once.
    depts = [{"id": d.id, "name": d.name, "code": d.code}
             for d in live_departments(departments_for(user))]
    batches = [{"id": b.id, "label": b.label, "department_id": b.department_id}
               for b in batches_for(user).select_related("department")]
    subjects = [{"id": s.id, "code": s.code, "name": s.name,
                 "department_id": s.department_id, "subject_type": s.subject_type,
                 "degree": s.degree,
                 "semester": s.semester}
                for s in subjects_for(user)]
    # Live sections of every batch above, so the allocation picker can narrow
    # to one batch in the browser without a second request per batch.
    section_rows = [{"id": s.id, "name": s.name, "batch_id": s.batch_id}
                    for s in sections.for_user(user)]
    teachers = []
    if user.role in (HEAD, HOD):
        teachers = [{"id": t.id, "name": t.full_name or t.email} for t in teachers_for(user)]
    return ok({
        "departments": depts, "batches": batches, "subjects": subjects,
        "sections": section_rows,
        "teachers": teachers, "role": user.role,
        # Sent with the lookups so a dropdown built in the browser groups by
        # the same list, in the same order, as one rendered server-side.
        "subject_types": [{"value": v, "label": l} for v, l in SubjectType.choices],
        "degrees": [{"value": v, "label": l} for v, l in Degree.choices],
    })
