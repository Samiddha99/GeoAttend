import json

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Prefetch, Q
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from accounts.emails import send_invitation
from accounts import face_service
from accounts.models import ActivityLog, FaceEnrolment, FaceSample, Invitation
from accounts.services import invite_user, unlink_device
from notifications.whatsapp import normalise_msisdn
from core.decorators import guardian_readonly, role_required
from core.http import fail, form_errors, ok
from core.utils import clean_object_id, clean_object_ids

from .forms import BatchForm, DepartmentForm, StudentEditForm, SubjectForm, TeacherInviteForm
from .importer import (
    build_roster_workbook,
    build_template_workbook,
    import_students,
    read_rows,
)
from .models import (
    Batch,
    Department,
    Enrollment,
    ImportJob,
    Subject,
    SubjectType,
    TeacherAssignment,
)
from .selectors import (
    all_students_for,
    batches_for,
    current_department,
    departments_for,
    manageable_department_ids,
    students_qs_for,
    subjects_for,
    teachers_for,
    visible_teachers_for,
)

HEAD, HOD, TEACHER, STUDENT, GUARDIAN = (
    "HEAD", "HOD", "TEACHER", "STUDENT", "GUARDIAN")


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
@role_required(HEAD)
@ensure_csrf_cookie
def departments_page(request):
    return render(request, "academics/departments.html", {"form": DepartmentForm()})


@role_required(HEAD, HOD)
@ensure_csrf_cookie
def subjects_page(request):
    return render(request, "academics/subjects.html", {
        "form": SubjectForm(), "departments": departments_for(request.user),
    })


@role_required(HEAD, HOD)
@ensure_csrf_cookie
def batches_page(request):
    return render(request, "academics/batches.html", {
        "form": BatchForm(), "departments": departments_for(request.user),
    })


@role_required(HEAD, HOD, TEACHER, STUDENT, GUARDIAN)
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
    institute = request.user.institute
    # Filters span the institute now that the table does. They deliberately do
    # not come from api_lookups: for a teacher that endpoint returns only the
    # subjects they personally teach, which would be useless for finding a
    # colleague in another department.
    return render(request, "academics/teachers.html", {
        "form": TeacherInviteForm() if can_manage else None,
        "departments": departments_for(request.user),   # invite/edit target list
        "can_manage": can_manage,
        "is_student": is_student,
        # Staff mobile numbers are not published to students by default. Names,
        # departments and subjects make a useful directory; handing every
        # student a teacher's personal number and a WhatsApp button is a
        # separate decision, and not one that can be walked back once made.
        "show_mobile": not is_student,
        "filter_departments": Department.objects.filter(
            institute=institute, is_active=True).order_by("name"),
        "filter_subjects": Subject.objects.filter(
            department__institute=institute, is_active=True).order_by("code"),
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
    staff = request.user.role in (HOD, HEAD)
    return render(request, "academics/students.html", {
        "departments": (Department.objects.filter(
            institute=request.user.institute, is_active=True).order_by("name")
            if wide else departments_for(request.user)),
        # Both are head/HoD only. They are separate flags because they are
        # separate ideas: importing a roster and editing a row. Teachers get
        # neither on either screen.
        "can_import": staff,
        "can_edit": staff,
        # The whole point of the wide screen for a teacher: unlink a device for
        # a student who is not in their own classes.
        "can_unlink_device": True,
        "wide": wide,
        "page_heading": "Students" if wide else "My students",
    })


@role_required(HEAD, HOD, TEACHER)
@ensure_csrf_cookie
def students_page(request):
    return _students_page(request, wide=request.user.role in (HOD, HEAD))


@role_required(HEAD, HOD, TEACHER)
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
@role_required(HEAD)
@require_GET
def api_departments(request):
    qs = (
        departments_for(request.user)
        .select_related("hod")
        .annotate(
            subject_count=Count("subjects", distinct=True),
            # Counts deliberately ignore archived batches and their students.
            batch_count=Count(
                "batches", filter=Q(batches__is_active=True), distinct=True),
            student_count=Count(
                "students", filter=Q(students__batch__is_active=True), distinct=True),
            teacher_count=Count(
                "members", filter=Q(members__role=TEACHER), distinct=True
            ),
        )
        .order_by("name")
    )
    rows = [{
        "id": d.id,
        "name": d.name,
        "code": d.code,
        "hod_name": d.hod.full_name if d.hod else "",
        "hod_email": d.hod.email if d.hod else "",
        "hod_status": d.hod_status,
        "subject_count": d.subject_count,
        "batch_count": d.batch_count,
        "student_count": d.student_count,
        "teacher_count": d.teacher_count,
        "is_active": d.is_active,
    } for d in qs]
    return ok({"rows": rows})


@role_required(HEAD)
@require_POST
def api_department_save(request, pk=None):
    instance = get_object_or_404(departments_for(request.user), pk=pk) if pk else None
    form = DepartmentForm(request.POST, instance=instance)
    if not form.is_valid():
        return fail("Please correct the highlighted fields.", form_errors(form))
    hod_email = form.cleaned_data.get("hod_email")
    try:
        with transaction.atomic():
            dept = form.save(commit=False)
            dept.institute = request.user.institute
            dept.save()
            invited = False
            if hod_email:
                if dept.hod and dept.hod.email == hod_email and dept.hod.registration_completed:
                    pass
                else:
                    clash = Department.objects.filter(hod__email=hod_email).exclude(pk=dept.pk).first()
                    if clash:
                        return fail(f"{hod_email} already leads {clash.name}.")
                    user, invitation, _ = invite_user(
                        email=hod_email, role=HOD, institute=request.user.institute,
                        department=dept, invited_by=request.user,
                        extra_lines=[f"Department: {dept.name} ({dept.code})"],
                    )
                    if invitation is None:
                        return fail(
                            f"{hod_email} already has an active account in this system."
                        )
                    dept.hod = user
                    dept.save(update_fields=["hod"])
                    invited = True
    except IntegrityError:
        return fail("A department with that name or code already exists.")
    ActivityLog.log(request, action="DEPARTMENT_SAVED", detail=dept.name)
    msg = "Department saved." + (f" Invitation emailed to {hod_email}." if invited else "")
    return ok({"id": dept.id}, message=msg)


@role_required(HEAD)
@require_POST
def api_department_delete(request, pk):
    dept = get_object_or_404(departments_for(request.user), pk=pk)
    if dept.students.exists() or dept.subjects.exists():
        dept.is_active = False
        dept.save(update_fields=["is_active"])
        return ok(message="Department archived (it still holds academic records).")
    name = dept.name
    dept.delete()
    ActivityLog.log(request, action="DEPARTMENT_DELETED", detail=name)
    return ok(message="Department removed.")


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


# --------------------------------------------------------------------------- #
#  Subjects
# --------------------------------------------------------------------------- #
@role_required(HEAD, HOD, TEACHER, STUDENT)
@require_GET
def api_subjects(request):
    qs = subjects_for(request.user).select_related("department")
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
    rows = [{
        "id": s.id, "code": s.code, "name": s.name, "semester": s.semester,
        "subject_type": s.subject_type,
        "subject_type_label": s.get_subject_type_display(),
        "credits": s.credits, "department": s.department.name,
        "department_id": s.department_id, "is_active": s.is_active,
        "teacher_count": s.teacher_count, "student_count": s.student_count,
    } for s in qs]
    return ok({"rows": rows})


@role_required(HEAD, HOD)
@require_POST
def api_subject_save(request, pk=None):
    dept = _scoped_department(request, request.POST.get("department") or None)
    instance = get_object_or_404(Subject, pk=pk, department__in=departments_for(request.user)) if pk else None
    form = SubjectForm(request.POST, instance=instance)
    if not form.is_valid():
        return fail("Please correct the highlighted fields.", form_errors(form))
    subject = form.save(commit=False)
    subject.department = instance.department if instance else dept
    try:
        subject.save()
    except IntegrityError:
        return fail("A subject with that code already exists in this department.")
    ActivityLog.log(request, action="SUBJECT_SAVED", detail=str(subject))
    return ok({"id": subject.id}, message="Subject saved.")


@role_required(HEAD, HOD)
@require_POST
def api_subject_delete(request, pk):
    subject = get_object_or_404(Subject, pk=pk, department__in=departments_for(request.user))
    if subject.assignments.exists() or subject.enrollments.exists():
        subject.is_active = False
        subject.save(update_fields=["is_active"])
        return ok(message="Subject deactivated (it is linked to teachers or students).")
    subject.delete()
    return ok(message="Subject removed.")


# --------------------------------------------------------------------------- #
#  Batches
# --------------------------------------------------------------------------- #
@role_required(HEAD, HOD, TEACHER, STUDENT)
@require_GET
def api_batches(request):
    # The only screen that shows archived batches — it is where they are revived.
    qs = batches_for(request.user, include_inactive=True).select_related(
        "department"
    ).annotate(n_students=Count("students", distinct=True))
    dept_id = request.GET.get("department")
    if dept_id:
        qs = qs.filter(department_id=dept_id)
    rows = [{
        "id": b.id, "label": b.label, "start_year": b.start_year, "end_year": b.end_year,
        "department": b.department.name, "department_id": b.department_id,
        "student_count": b.n_students, "is_active": b.is_active,
    } for b in qs]
    return ok({"rows": rows})


@role_required(HEAD, HOD)
@require_POST
def api_batch_save(request, pk=None):
    dept = _scoped_department(request, request.POST.get("department") or None)
    instance = get_object_or_404(Batch, pk=pk, department__in=departments_for(request.user)) if pk else None
    form = BatchForm(request.POST, instance=instance)
    if not form.is_valid():
        return fail("Please correct the highlighted fields.", form_errors(form))
    batch = form.save(commit=False)
    batch.department = instance.department if instance else dept
    batch.start_year = form.cleaned_data["start_year"]
    batch.end_year = form.cleaned_data["end_year"]
    try:
        batch.save()
    except IntegrityError:
        return fail("That batch already exists in this department.")
    return ok({"id": batch.id}, message="Batch saved.")


@role_required(HEAD, HOD)
@require_POST
def api_batch_toggle(request, pk):
    """
    Archive or restore a batch.

    Archiving hides the cohort — its students, sessions, records and every
    statistic derived from them — across the whole application. Nothing is
    deleted, so restoring brings all of it straight back.
    """
    batch = get_object_or_404(Batch, pk=pk, department__in=departments_for(request.user))
    batch.is_active = not batch.is_active
    batch.save(update_fields=["is_active"])
    ActivityLog.log(request, action="BATCH_TOGGLED",
                    detail=f"{batch.label} {'restored' if batch.is_active else 'archived'}")
    students = batch.students.count()
    if batch.is_active:
        message = f"{batch.label} restored — its {students} student(s) are visible again."
    else:
        message = (f"{batch.label} archived. Its {students} student(s) and all their "
                   "records are now hidden everywhere.")
    return ok({"is_active": batch.is_active}, message=message)


@role_required(HEAD, HOD)
@require_POST
def api_batch_delete(request, pk):
    batch = get_object_or_404(Batch, pk=pk, department__in=departments_for(request.user))
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
        "batch_id": a.batch_id,
        "batch": a.batch.label,
    } for a in teacher.assignments.select_related("subject", "batch").filter(
        is_active=True, batch__is_active=True)]


@role_required(HEAD, HOD, TEACHER, STUDENT, GUARDIAN)
@guardian_readonly
@require_GET
def api_teachers(request):
    # Read scope: staff see the whole institute. Editing is decided per row by
    # `can_edit` below, and enforced independently by the write endpoints.
    qs = visible_teachers_for(request.user).select_related("department").prefetch_related(
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
    pending = {}
    if can_manage:
        pending = {
            i.email: i.id
            for i in Invitation.objects.filter(role=TEACHER, status=Invitation.Status.PENDING,
                                               institute=request.user.institute)
        }
    rows = [{
        "id": t.id,
        "full_name": t.full_name or "(awaiting registration)",
        "email": t.email,
        "phone": t.phone if show_mobile else "",
        "phone_dial": dial(t.phone) if show_mobile else None,
        "department": t.department.name if t.department else "",
        "department_id": t.department_id,      # the client filters on this
        "status": "active" if t.registration_completed else "invited",
        "is_active": t.is_active,
        # Mirrors what teachers_for() would allow, so the buttons the browser
        # shows match what the server will actually accept.
        "can_edit": can_manage and (manageable is None or t.department_id in manageable),
        "invitation_id": pending.get(t.email),
        "assignments": _assignment_rows(t),
    } for t in qs]
    return ok({"rows": rows})


@role_required(HEAD, HOD)
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

    # The browser sends ids as 24-char hex strings; the database returns
    # ObjectIds, and the two are never equal — so key the lookups by str().
    # Junk is dropped here rather than in the queryset, because
    # ObjectIdAutoField raises ValidationError instead of simply not matching.
    subject_ids = set(clean_object_ids(p.get("subject_id") for p in pairs))
    batch_ids = set(clean_object_ids(p.get("batch_id") for p in pairs))
    subjects = {str(s.id): s for s in Subject.objects.filter(
        id__in=subject_ids, department=dept)}
    batches = {str(b.id): b for b in Batch.objects.filter(
        id__in=batch_ids, department=dept, is_active=True)}
    if (len(subjects) != len(subject_ids) or len(batches) != len(batch_ids)
            or len(subject_ids) != len({p.get("subject_id") for p in pairs})
            or len(batch_ids) != len({p.get("batch_id") for p in pairs})):
        return fail("One of the selected subjects or batches is not in your department, "
                    "or the batch is archived.", status=403)

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
                f"{subjects[p['subject_id']].code} · {batches[p['batch_id']].label}"
                for p in pairs
            )],
        )
        if invitation is None and not user.is_teacher:
            return fail("That email already belongs to a non-teacher account.")
        if form.cleaned_data.get("phone"):
            user.phone = form.cleaned_data["phone"]
            user.save(update_fields=["phone"])
        for p in pairs:
            TeacherAssignment.objects.update_or_create(
                teacher=user,
                subject=subjects[p["subject_id"]],
                batch=batches[p["batch_id"]],
                defaults={"assigned_by": request.user, "is_active": True},
            )
    ActivityLog.log(request, action="TEACHER_INVITED", detail=user.email)
    note = " Invitation emailed." if invitation else " (Account already active — assignments updated.)"
    return ok({"id": user.id}, message="Teacher saved." + note)


@role_required(HEAD, HOD)
@require_POST
def api_teacher_assignments_save(request, pk):
    """
    Update a teacher: name, mobile, department (head only) and allocations.

    `teachers_for` is the scope gate — a HoD asking for a teacher outside their
    department gets a 404 here, which is what keeps "HoDs manage only their own
    department" true regardless of what the browser sends.
    """
    teacher = get_object_or_404(teachers_for(request.user), pk=pk)
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

    try:
        pairs = json.loads(request.POST.get("assignments") or "[]")
    except ValueError:
        return fail("Assignments payload is malformed.")
    keep = set()
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
        if changed:
            teacher.save(update_fields=changed)
        for p in pairs:
            subject = get_object_or_404(
                Subject, pk=clean_object_id(p.get("subject_id")) or "", department=dept)
            batch = get_object_or_404(
                Batch, pk=clean_object_id(p.get("batch_id")) or "",
                department=dept, is_active=True)
            obj, _ = TeacherAssignment.objects.update_or_create(
                teacher=teacher, subject=subject, batch=batch,
                defaults={"assigned_by": request.user, "is_active": True},
            )
            keep.add(obj.id)
        # Anything not resubmitted is retired. This also cleans up after a
        # department move: allocations to the old department's subjects are not
        # in `pairs` (they could not be, they are validated against `dept`), so
        # they deactivate here rather than lingering as invalid cross-department
        # links.
        TeacherAssignment.objects.filter(teacher=teacher).exclude(id__in=keep).update(is_active=False)
    ActivityLog.log(request, action="TEACHER_UPDATED", detail=teacher.email)
    return ok({"assignments": _assignment_rows(teacher)}, message="Teacher updated.")


@role_required(HEAD, HOD)
@require_POST
def api_teacher_toggle(request, pk):
    teacher = get_object_or_404(teachers_for(request.user), pk=pk)
    teacher.is_active = not teacher.is_active
    teacher.save(update_fields=["is_active"])
    state = "re-activated" if teacher.is_active else "deactivated"
    ActivityLog.log(request, action="TEACHER_TOGGLED", detail=f"{teacher.email} {state}")
    return ok({"is_active": teacher.is_active}, message=f"{teacher.email} {state}.")


# --------------------------------------------------------------------------- #
#  Students
# --------------------------------------------------------------------------- #
@role_required(HEAD, HOD, TEACHER)
@require_GET
def api_students(request):
    # `scope=all` is the institute-wide directory. all_students_for() returns
    # nothing for a student account, so the parameter cannot widen anyone's
    # access beyond what their role already allows.
    wide = request.GET.get("scope") == "all"
    base = all_students_for(request.user) if wide else students_qs_for(request.user)
    qs = base.select_related("user", "batch", "department").prefetch_related(
        Prefetch("enrollments", queryset=Enrollment.objects.filter(is_active=True).select_related("subject"))
    )
    if request.GET.get("department"):
        qs = qs.filter(department_id=request.GET["department"])
    if request.GET.get("batch"):
        qs = qs.filter(batch_id=request.GET["batch"])
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
        "department": s.department.name,
        "department_id": s.department_id,
        "subjects": [e.subject.code for e in s.enrollments.all()],
        # Code -> type, so the subject dropdown on this screen can group itself.
        # It is built from the rows rather than from api_lookups because this
        # table spans the institute while a teacher's lookups do not.
        "subject_types": {e.subject.code: e.subject.subject_type
                          for e in s.enrollments.all()},
        "status": "active" if s.user.registration_completed else "invited",
        "is_active": s.is_active and s.user.is_active,
        "device_bound": bool(s.user.device_id),
        "device_bound_at": (
            timezone.localtime(s.user.device_bound_at).strftime("%d %b %Y, %H:%M")
            if s.user.device_bound_at else ""
        ),
        # The gate is hard, so staff need to see at a glance who is stuck
        # behind it and have the reset button to hand.
        "face_enrolled": bool(s.user.face_enrolled),
    } for s in qs.distinct()[:2000]]
    return ok({"rows": rows, "count": len(rows)})


@role_required(HEAD, HOD)
@require_POST
def api_students_import(request):
    dept = _scoped_department(request, request.POST.get("department") or None)
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
                job = import_students(rows, dept, request.user, upload.name, send_invites=False)
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

    job = import_students(rows, dept, request.user, upload.name, send_invites=True)
    ActivityLog.log(request, action="STUDENTS_IMPORTED",
                    detail=f"{job.created_count} created / {job.updated_count} updated")
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


@role_required(HEAD, HOD)
@require_GET
def api_students_template(request):
    dept = current_department(request.user)
    stream = build_template_workbook(dept)
    return FileResponse(
        stream, as_attachment=True, filename="student_roster_template.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@role_required(HEAD, HOD)
@require_GET
def api_import_jobs(request):
    qs = ImportJob.objects.filter(department__in=departments_for(request.user)).select_related(
        "uploaded_by", "department"
    )[:50]
    rows = [{
        "id": j.id, "file_name": j.file_name, "status": j.status,
        "department": j.department.name,
        "uploaded_by": j.uploaded_by.full_name if j.uploaded_by else "",
        "total": j.total_rows, "created": j.created_count,
        "updated": j.updated_count, "errors": j.error_count,
        "created_at": j.created_at.strftime("%d %b %Y, %H:%M"),
    } for j in qs]
    return ok({"rows": rows})


@role_required(HEAD, HOD)
@require_GET
def api_import_job_detail(request, pk):
    job = get_object_or_404(ImportJob, pk=pk, department__in=departments_for(request.user))
    return ok({"rows": job.report.get("rows", [])})


@role_required(HEAD, HOD)
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
    student.batch = batch
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
        valid = Subject.objects.filter(id__in=subject_ids, department=student.department)
        Enrollment.objects.filter(student=student).exclude(
            subject__in=valid
        ).update(is_active=False)
        for subj in valid:
            Enrollment.objects.update_or_create(
                student=student, subject=subj, defaults={"is_active": True}
            )
    return ok(message="Student updated.")


@role_required(HEAD, HOD)
@require_POST
def api_student_toggle(request, pk):
    student = get_object_or_404(students_qs_for(request.user), pk=pk)
    student.is_active = not student.is_active
    student.save(update_fields=["is_active"])
    student.user.is_active = student.is_active
    student.user.save(update_fields=["is_active"])
    state = "re-activated" if student.is_active else "deactivated"
    return ok({"is_active": student.is_active}, message=f"{student.user.email} {state}.")


@role_required(HEAD, HOD, TEACHER)
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


@role_required(HEAD, HOD, TEACHER)
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


@role_required(HEAD, HOD, TEACHER)
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


@role_required(HEAD, HOD, TEACHER)
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


@role_required(HEAD, HOD)
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


@role_required(HEAD, HOD, TEACHER)
@require_GET
def api_students_export(request):
    """Download the current roster in the same shape the importer accepts."""
    qs = students_qs_for(request.user).select_related("user", "batch").prefetch_related(
        Prefetch("enrollments", queryset=Enrollment.objects.filter(is_active=True).select_related("subject"))
    )
    if request.GET.get("department"):
        qs = qs.filter(department_id=request.GET["department"])
    if request.GET.get("batch"):
        qs = qs.filter(batch_id=request.GET["batch"])
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
@role_required(HEAD, HOD, TEACHER, STUDENT)
@require_GET
def api_lookups(request):
    user = request.user
    depts = [{"id": d.id, "name": d.name, "code": d.code} for d in departments_for(user)]
    batches = [{"id": b.id, "label": b.label, "department_id": b.department_id}
               for b in batches_for(user).select_related("department")]
    subjects = [{"id": s.id, "code": s.code, "name": s.name,
                 "department_id": s.department_id, "subject_type": s.subject_type,
                 "semester": s.semester}
                for s in subjects_for(user)]
    teachers = []
    if user.role in (HEAD, HOD):
        teachers = [{"id": t.id, "name": t.full_name or t.email} for t in teachers_for(user)]
    return ok({
        "departments": depts, "batches": batches, "subjects": subjects,
        "teachers": teachers, "role": user.role,
        # Sent with the lookups so a dropdown built in the browser groups by
        # the same list, in the same order, as one rendered server-side.
        "subject_types": [{"value": v, "label": l} for v, l in SubjectType.choices],
    })
