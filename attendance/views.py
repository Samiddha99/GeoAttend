import csv

from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from academics.models import StudentProfile, Subject, TeacherAssignment
from academics.selectors import (
    batches_for,
    departments_for,
    enrolled_students,
    teacher_subjects_for_batch,
)
from accounts.models import ActivityLog
from core.decorators import role_required
from core.http import fail, ok
from core.utils import clean_object_id, clean_object_ids, parse_date

from .models import (
    AbsenceAttachment,
    AbsenceReason,
    AttendanceRecord,
    AttendanceSession,
    MarkAttempt,
    PlannedAbsence,
    PlannedAbsenceDecision,
)
from .services import (
    CONF,
    AttendanceError,
    can_view_attachment,
    create_session,
    manual_mark,
    mark_attendance,
    notify_session,
    cancel_planned_absence,
    planned_subjects_for,
    review_absence_reason,
    review_planned_decision,
    submit_absence_reason,
    submit_planned_absence,
)

HEAD, HOD, TEACHER, STUDENT = "HEAD", "HOD", "TEACHER", "STUDENT"


def _visible_sessions(user):
    # Sessions belonging to an archived batch disappear along with the batch.
    qs = AttendanceSession.objects.select_related(
        "subject", "batch", "teacher", "batch__department"
    ).filter(batch__is_active=True)
    if user.is_head:
        return qs.filter(subject__department__institute=user.institute)
    if user.is_hod:
        return qs.filter(subject__department=user.department)
    if user.is_teacher:
        return qs.filter(teacher=user)
    if user.is_student:
        profile = getattr(user, "student_profile", None)
        if profile is None or not profile.batch.is_active:
            return qs.none()
        return qs.filter(batch=profile.batch, subject__enrollments__student=profile).distinct()
    return qs.none()


# --------------------------------------------------------------------------- #
#  Teacher — generate & monitor
# --------------------------------------------------------------------------- #
@role_required(TEACHER)
@ensure_csrf_cookie
def generate_page(request):
    return render(request, "attendance/generate.html", {
        "batches": batches_for(request.user).select_related("department"),
    })


@role_required(TEACHER, HOD, HEAD)
@ensure_csrf_cookie
def sessions_page(request):
    today = timezone.localdate()
    return render(request, "attendance/sessions.html", {
        "default_start": f"{today.year}-01-01",
        "default_end": today.isoformat(),
    })


@role_required(TEACHER, HOD, HEAD)
@ensure_csrf_cookie
def session_detail_page(request, pk):
    session = get_object_or_404(_visible_sessions(request.user), pk=pk)
    return render(request, "attendance/session_detail.html", {"session": session})


@role_required(TEACHER)
@require_GET
def api_teacher_batches(request):
    rows = [{"id": b.id, "label": b.label, "department": b.department.name}
            for b in batches_for(request.user).select_related("department")]
    return ok({"rows": rows})


@role_required(TEACHER)
@require_GET
def api_batch_subjects(request, batch_id):
    batch = get_object_or_404(batches_for(request.user), pk=batch_id)
    subjects = teacher_subjects_for_batch(request.user, batch)
    rows = []
    for s in subjects:
        rows.append({
            "id": s.id,
            "code": s.code,
            "name": s.name,
            "semester": s.semester,
            "enrolled": enrolled_students(s, batch).count(),
        })
    return ok({"rows": rows, "batch": {"id": batch.id, "label": batch.label}})


@role_required(TEACHER)
@require_POST
def api_session_create(request):
    batch = get_object_or_404(batches_for(request.user), pk=request.POST.get("batch"))
    subject = get_object_or_404(Subject, pk=request.POST.get("subject"))
    try:
        session = create_session(
            teacher=request.user,
            subject=subject,
            batch=batch,
            latitude=request.POST.get("latitude"),
            longitude=request.POST.get("longitude"),
            accuracy=request.POST.get("accuracy") or 0,
            minutes=request.POST.get("minutes"),
            radius=request.POST.get("radius"),
            note=request.POST.get("note", ""),
        )
    except AttendanceError as exc:
        return fail(exc.message, status=exc.status, code=exc.code)

    emailed = 0
    if request.POST.get("notify", "1") == "1":
        emailed = notify_session(session)
    ActivityLog.log(request, action="SESSION_CREATED",
                    detail=f"{subject.code} · {batch.label} · {session.expected_count} students")
    return ok({
        "id": session.id,
        "token": session.token,
        "url": session.mark_url,
        "detail_url": f"/attendance/sessions/{session.id}/",
        "expires_at": session.expires_at.isoformat(),
        "seconds_left": session.seconds_left,
        "expected": session.expected_count,
        "emailed": emailed,
        "radius": session.radius_m,
        "subject": f"{subject.code} — {subject.name}",
        "batch": batch.label,
    }, message=f"Attendance link is live for {session.expected_count} students.")


@role_required(TEACHER, HOD, HEAD)
@require_GET
def api_session_status(request, pk):
    session = get_object_or_404(_visible_sessions(request.user), pk=pk)
    records = session.records.select_related("student", "student__user").order_by("-marked_at")
    present_ids = {r.student_id for r in records}
    roster = enrolled_students(session.subject, session.batch)
    present = [{
        "student_id": r.student_id,
        "name": r.student.name,
        "roll": r.student.class_roll,
        "email": r.student.email,
        "at": timezone.localtime(r.marked_at).strftime("%H:%M:%S"),
        "distance": round(r.distance_m, 1) if r.distance_m is not None else None,
        "status": r.status,
        "remark": r.remark,
    } for r in records]
    absent = [{
        "student_id": s.id, "name": s.name, "roll": s.class_roll, "email": s.email,
    } for s in roster if s.id not in present_ids]
    return ok({
        "id": session.id,
        "status": session.effective_status,
        "seconds_left": session.seconds_left,
        "expires_at": timezone.localtime(session.expires_at).strftime("%d %b %Y, %H:%M:%S"),
        "expected": session.expected_count,
        "present_count": len(present),
        "absent_count": len(absent),
        "percentage": round(len(present) * 100.0 / session.expected_count, 1) if session.expected_count else 0,
        "present": present,
        "absent": absent,
        "url": session.mark_url,
        "radius": session.radius_m,
        "subject": f"{session.subject.code} — {session.subject.name}",
        "batch": session.batch.label,
        "teacher": session.teacher.full_name,
        "note": session.note,
        "created_at": timezone.localtime(session.created_at).strftime("%d %b %Y, %H:%M"),
    })


@role_required(TEACHER, HOD, HEAD)
@require_POST
def api_session_action(request, pk, action):
    session = get_object_or_404(_visible_sessions(request.user), pk=pk)
    if request.user.is_teacher and session.teacher_id != request.user.id:
        return fail("You can only manage your own sessions.", status=403)
    if action == "close":
        session.close()
        message = "Session closed."
    elif action == "extend":
        try:
            minutes = int(request.POST.get("minutes", 5))
        except ValueError:
            return fail("Enter a valid number of minutes.")
        if not 1 <= minutes <= 180:
            return fail("Extend by 1–180 minutes.")
        session.extend(minutes)
        message = f"Extended by {minutes} minute(s)."
    elif action == "cancel":
        session.status = AttendanceSession.Status.CANCELLED
        session.closed_at = timezone.now()
        session.save(update_fields=["status", "closed_at"])
        message = "Session cancelled."
    elif action == "resend":
        sent = notify_session(session)
        message = f"Link re-sent to {sent} student(s)."
    else:
        return fail("Unknown action.", status=404)
    ActivityLog.log(request, action=f"SESSION_{action.upper()}", detail=str(session))
    return ok({"status": session.effective_status, "seconds_left": session.seconds_left},
              message=message)


@role_required(TEACHER, HOD, HEAD)
@require_POST
def api_manual_mark(request, pk):
    session = get_object_or_404(_visible_sessions(request.user), pk=pk)
    student = get_object_or_404(StudentProfile, pk=request.POST.get("student"))
    present = request.POST.get("present", "1") == "1"
    try:
        _, message = manual_mark(
            session=session, student=student, teacher=request.user,
            present=present, remark=request.POST.get("remark", ""),
        )
    except AttendanceError as exc:
        return fail(exc.message, status=exc.status)
    ActivityLog.log(request, action="MANUAL_MARK",
                    detail=f"{student.email} → {'present' if present else 'absent'} ({session})")
    return ok(message=message)


@role_required(TEACHER, HOD, HEAD, STUDENT)
@require_GET
def api_sessions(request):
    qs = _visible_sessions(request.user).annotate(
        marked=Count("records", filter=Q(records__status__in=["PRESENT", "MANUAL"]))
    )
    # clean_object_id() rather than the raw value: ObjectIdAutoField raises
    # ValidationError on a malformed id instead of just not matching, so an
    # unchecked query string would 500 the page.
    for param, lookup in (("subject", "subject_id"), ("batch", "batch_id"),
                          ("teacher", "teacher_id"),
                          ("department", "subject__department_id")):
        value = clean_object_id(request.GET.get(param))
        if value:
            qs = qs.filter(**{lookup: value})
    start = parse_date(request.GET.get("start"))
    end = parse_date(request.GET.get("end"))
    if start:
        qs = qs.filter(session_date__gte=start)
    if end:
        qs = qs.filter(session_date__lte=end)
    status = request.GET.get("status")
    if status == "OPEN":
        qs = qs.filter(status=AttendanceSession.Status.OPEN, expires_at__gt=timezone.now())

    rows = [{
        "id": s.id,
        "date": s.session_date.strftime("%d %b %Y"),
        "time": timezone.localtime(s.created_at).strftime("%H:%M"),
        "subject": s.subject.code,
        "subject_name": s.subject.name,
        "batch": s.batch.label,
        "teacher": s.teacher.full_name or s.teacher.email,
        "expected": s.expected_count,
        "present": s.marked,
        "percentage": round(s.marked * 100.0 / s.expected_count, 1) if s.expected_count else 0,
        "status": s.effective_status,
        "note": s.note,
        "url": f"/attendance/sessions/{s.id}/",
    } for s in qs[:500]]
    return ok({"rows": rows})


@role_required(TEACHER, HOD, HEAD)
@require_GET
def api_session_export(request, pk):
    session = get_object_or_404(_visible_sessions(request.user), pk=pk)
    present = {r.student_id: r for r in session.records.select_related("student__user")}
    response = HttpResponse(content_type="text/csv")
    fname = f"attendance_{session.subject.code}_{session.session_date}.csv"
    response["Content-Disposition"] = f'attachment; filename="{fname}"'
    writer = csv.writer(response)
    writer.writerow(["Class Roll", "Exam Roll", "Name", "Email", "Status",
                     "Marked At", "Distance (m)", "Remark"])
    for student in enrolled_students(session.subject, session.batch):
        rec = present.get(student.id)
        writer.writerow([
            student.class_roll, student.exam_roll, student.name, student.email,
            "PRESENT" if rec else "ABSENT",
            timezone.localtime(rec.marked_at).strftime("%Y-%m-%d %H:%M:%S") if rec else "",
            round(rec.distance_m, 1) if rec and rec.distance_m is not None else "",
            rec.remark if rec else "",
        ])
    return response


@role_required(TEACHER, HOD, HEAD)
@require_GET
def api_session_attempts(request, pk):
    session = get_object_or_404(_visible_sessions(request.user), pk=pk)
    rows = [{
        "user": a.user.full_name if a.user else "—",
        "email": a.user.email if a.user else "",
        "reason": a.get_reason_display(),
        "code": a.reason,
        "distance": round(a.distance_m, 1) if a.distance_m is not None else None,
        "accuracy": round(a.accuracy_m, 1) if a.accuracy_m is not None else None,
        "ip": a.ip or "",
        "at": timezone.localtime(a.created_at).strftime("%H:%M:%S"),
        "detail": a.detail,
    } for a in session.attempts.select_related("user")[:300]]
    return ok({"rows": rows})


# --------------------------------------------------------------------------- #
#  Student — mark
# --------------------------------------------------------------------------- #
@login_required
@ensure_csrf_cookie
def mark_page(request, token):
    """
    The link a student taps.  If they were not signed in, @login_required has
    already bounced them through the login page with ?next=… so they land back
    here and attendance is marked automatically by the page's JS.
    """
    session = AttendanceSession.objects.select_related(
        "subject", "batch", "teacher"
    ).filter(token=token).first()
    already = None
    profile = getattr(request.user, "student_profile", None)
    if session and profile:
        already = AttendanceRecord.objects.filter(session=session, student=profile).first()
    return render(request, "attendance/mark.html", {
        "session": session,
        "token": token,
        "already": already,
        "not_student": not request.user.is_student,
        # The client refines its GPS fix until it clears the same bar the server
        # enforces, so the two can never drift apart.
        "max_accuracy_m": CONF["MAX_GPS_ACCURACY_M"],
    })


@login_required
@require_POST
def api_mark(request, token):
    session = AttendanceSession.objects.select_related("subject", "batch").filter(token=token).first()
    if session is None:
        MarkAttempt.objects.create(user=request.user, reason=MarkAttempt.Reason.EXPIRED,
                                   detail="unknown token")
        return fail("This attendance link is not valid.", status=404, code="INVALID")
    try:
        record, distance = mark_attendance(
            request=request,
            session=session,
            latitude=request.POST.get("latitude"),
            longitude=request.POST.get("longitude"),
            accuracy=request.POST.get("accuracy"),
            client_hash=request.POST.get("device_hash", ""),
        )
    except AttendanceError as exc:
        return fail(exc.message, status=exc.status, code=exc.code, **exc.extra)
    return ok({
        "subject": f"{session.subject.code} — {session.subject.name}",
        "batch": session.batch.label,
        "student": record.student.name,
        "marked_at": timezone.localtime(record.marked_at).strftime("%d %b %Y, %I:%M:%S %p"),
        "distance_m": distance,
        "radius": session.radius_m,
    }, message="Attendance marked successfully.")


@role_required(STUDENT)
@ensure_csrf_cookie
def my_attendance_page(request):
    return render(request, "attendance/my_attendance.html")


# --------------------------------------------------------------------------- #
#  Absence reasons
# --------------------------------------------------------------------------- #
def _attachment_rows(parent):
    """
    The evidence on one request, as metadata only.

    No URL to the file itself: downloads go through a view that re-checks who
    is asking, so a link copied out of a JSON response is worthless to anyone
    else. The id is all the client needs to build that link.
    """
    return [{
        "id": a.id,
        "name": a.original_name,
        "size": a.size_label,
        "is_image": a.is_image,
    } for a in parent.attachments.all()]


def _reason_row(r, *, for_reviewer=False):
    row = {
        "id": r.id,
        "session_id": r.session_id,
        "date": r.session.session_date.strftime("%d %b %Y"),
        # The display date cannot be sorted or grouped on — "01 Aug" sorts
        # before "02 Jul". The ISO form travels alongside it for both jobs.
        "date_iso": r.session.session_date.isoformat(),
        "subject": r.session.subject.code,
        "subject_name": r.session.subject.name,
        "batch": r.session.batch.label,
        "reason": r.reason,
        "status": r.status,
        "status_label": r.get_status_display(),
        "submitted_at": timezone.localtime(r.submitted_at).strftime("%d %b %Y, %H:%M"),
        "submitted_iso": timezone.localtime(r.submitted_at).isoformat(),
        "review_remark": r.review_remark,
        "reviewed_by": (r.reviewed_by.full_name or r.reviewed_by.email) if r.reviewed_by else "",
        "reviewed_at": (timezone.localtime(r.reviewed_at).strftime("%d %b %Y, %H:%M")
                        if r.reviewed_at else ""),
        "attachments": _attachment_rows(r),
    }
    if for_reviewer:
        row.update({
            "student": r.student.name,
            "student_id": r.student_id,
            "class_roll": r.student.class_roll,
            "email": r.student.email,
            "department": r.student.department.name,
            "teacher": r.session.teacher.full_name or r.session.teacher.email,
        })
    return row


def _group_reason_rows(rows):
    """
    Collapse per-class reasons into one row per student per day.

    A student who missed a whole morning files one reason per class, and they
    are nearly always the same account of the same event — so three rows carry
    one story and the reviewer reads it three times. Grouped, the day is the
    row and the subjects are chips inside it, which is exactly the shape the
    planned-absence table already uses.

    The per-class records themselves are untouched: each subject keeps its own
    status, remark and reviewer, because each is still decided separately.
    """
    groups = {}
    for row in rows:
        key = (str(row.get("student_id") or ""), row["date_iso"])
        group = groups.get(key)
        if group is None:
            group = groups[key] = {
                "key": f"{key[0]}:{key[1]}",
                "student": row.get("student", ""),
                "student_id": row.get("student_id"),
                "class_roll": row.get("class_roll", ""),
                "email": row.get("email", ""),
                "department": row.get("department", ""),
                "batch": row["batch"],
                "date": row["date"],
                "date_iso": row["date_iso"],
                # The group is "submitted" when its last class was explained.
                "submitted_at": row["submitted_at"],
                "submitted_iso": row["submitted_iso"],
                "items": [],
            }
        elif row["submitted_iso"] > group["submitted_iso"]:
            group["submitted_at"] = row["submitted_at"]
            group["submitted_iso"] = row["submitted_iso"]
        group["items"].append(row)

    grouped = []
    for group in groups.values():
        group["items"].sort(key=lambda i: i["subject"])
        # A flat string so the table's search and CSV can see the subjects
        # without knowing how the chips are built.
        group["subjects"] = ", ".join(i["subject"] for i in group["items"])
        group["pending"] = sum(
            1 for i in group["items"] if i["status"] == AbsenceReason.Status.PENDING)
        grouped.append(group)

    grouped.sort(key=lambda g: g["student"])
    grouped.sort(key=lambda g: g["date_iso"], reverse=True)
    return grouped


@role_required(STUDENT)
@require_POST
def api_absence_reason_submit(request, pk):
    """A student explains one missed class. Every rule lives in the service."""
    profile = getattr(request.user, "student_profile", None)
    if profile is None:
        return fail("Your student profile is not set up yet.", status=403)
    session = get_object_or_404(AttendanceSession, pk=pk)
    try:
        reason = submit_absence_reason(
            student=profile, session=session, text=request.POST.get("reason", ""),
            files=request.FILES.getlist("files[]"))
    except AttendanceError as exc:
        return fail(exc.message, {"reason": exc.message}, status=exc.status, code=exc.code)
    ActivityLog.log(request, action="ABSENCE_REASON_SUBMITTED",
                    detail=f"{session.subject.code} {session.session_date}")
    return ok({"row": _reason_row(reason)},
              message="Reason submitted. Your teacher will review it.")


@role_required(HEAD, HOD, TEACHER, STUDENT)
@require_GET
def api_absence_reasons(request):
    """
    Reasons the caller may see.

    Reviewers get everything they can act on, grouped one row per student per
    day; a student gets their own, flat, with the reviewer-only columns left
    off.
    """
    qs = AbsenceReason.objects.select_related(
        "session", "session__subject", "session__subject__department",
        "session__batch", "session__teacher", "student", "student__user",
        "student__department", "reviewed_by",
    ).prefetch_related("attachments")
    user = request.user
    if user.is_student:
        profile = getattr(user, "student_profile", None)
        if profile is None:
            return ok({"rows": [], "pending": 0})
        qs = qs.filter(student=profile)
        status = (request.GET.get("status") or "").upper()
        if status in AbsenceReason.Status.values:
            qs = qs.filter(status=status)
        rows = [_reason_row(r) for r in qs[:500]]
        return ok({
            "rows": rows,
            "pending": sum(1 for r in rows if r["status"] == AbsenceReason.Status.PENDING),
        })
    if user.is_teacher:
        # Only the sessions this teacher actually ran.
        qs = qs.filter(session__teacher=user)
    elif user.is_hod:
        qs = qs.filter(session__subject__department__in=departments_for(user))
    else:
        qs = qs.filter(session__subject__department__institute=user.institute)

    status = (request.GET.get("status") or "").upper()
    if status in AbsenceReason.Status.values:
        qs = qs.filter(status=status)
    rows = _group_reason_rows([_reason_row(r, for_reviewer=True) for r in qs[:1000]])
    return ok({
        "rows": rows,
        # Still counted per class, not per row — the sidebar badge answers
        # "how many decisions are waiting", and a grouped row can hold several.
        "pending": sum(r["pending"] for r in rows),
    })


@role_required(HEAD, HOD, TEACHER)
@require_POST
def api_absence_reason_review(request, pk):
    reason = get_object_or_404(
        AbsenceReason.objects.select_related(
            "session", "session__subject", "session__subject__department", "student"),
        pk=pk)
    approve = request.POST.get("decision") == "approve"
    try:
        review_absence_reason(
            reason=reason, actor=request.user, approve=approve,
            remark=request.POST.get("remark", ""))
    except AttendanceError as exc:
        return fail(exc.message, status=exc.status, code=exc.code)
    ActivityLog.log(
        request, action="ABSENCE_REASON_REVIEWED",
        detail=f"{reason.status} · {reason.student.name} · {reason.session.subject.code}")
    return ok({"row": _reason_row(reason, for_reviewer=True)},
              message=f"Reason {reason.get_status_display().lower()}.")


@role_required(HEAD, HOD, TEACHER)
@ensure_csrf_cookie
def absence_reasons_page(request):
    return render(request, "attendance/absence_reasons.html", {
        # Teachers only ever see their own sessions, so there is nothing to
        # filter by department for them.
        "show_department": request.user.role in (HEAD,),
    })


@role_required(STUDENT)
@ensure_csrf_cookie
def my_absence_reasons_page(request):
    """A student's own submissions and where each one stands."""
    return render(request, "attendance/my_absence_reasons.html", {
        "reason_window_days": CONF.get("ABSENCE_REASON_DAYS", 3),
    })


# --------------------------------------------------------------------------- #
#  Planned absences
# --------------------------------------------------------------------------- #
def _planned_row(p, *, for_reviewer=False, only_subjects=None):
    """
    One planned absence. `only_subjects` trims the decision list to the
    subjects a reviewer may actually act on, so a teacher is not shown other
    teachers' verdicts as if they were theirs to change.
    """
    decisions = list(p.decisions.all())
    if only_subjects is not None:
        decisions = [d for d in decisions if d.subject_id in only_subjects]
    row = {
        "id": p.id,
        "from_date": p.from_date.strftime("%d %b %Y"),
        "to_date": p.to_date.strftime("%d %b %Y"),
        "days": p.days,
        "reason": p.reason,
        "all_subjects": p.all_subjects,
        "cancelled": p.is_cancelled,
        "status": p.overall_status,
        "status_label": dict(AbsenceReason.Status.choices)[p.overall_status],
        "created_at": timezone.localtime(p.created_at).strftime("%d %b %Y, %H:%M"),
        "can_cancel": not p.is_cancelled and p.from_date > timezone.localdate(),
        "attachments": _attachment_rows(p),
        "decisions": [{
            "id": d.id,
            "subject_id": d.subject_id,
            "subject": d.subject.code,
            "subject_name": d.subject.name,
            "status": d.status,
            "status_label": d.get_status_display(),
            "remark": d.review_remark,
            "reviewed_by": (d.reviewed_by.full_name or d.reviewed_by.email)
                           if d.reviewed_by else "",
        } for d in decisions],
    }
    if for_reviewer:
        row.update({
            "student": p.student.name,
            "class_roll": p.student.class_roll,
            "email": p.student.email,
            "batch": p.student.batch.label,
            "department": p.student.department.name,
        })
    return row


def _reviewable_subject_ids(user):
    """Subjects whose planned-absence decisions `user` may make."""
    if user.is_head:
        return set(Subject.objects.filter(
            department__institute=user.institute).values_list("id", flat=True))
    if user.is_hod:
        return set(Subject.objects.filter(
            department__in=departments_for(user)).values_list("id", flat=True))
    if user.is_teacher:
        return set(TeacherAssignment.objects.filter(
            teacher=user, is_active=True).values_list("subject_id", flat=True))
    return set()


@role_required(STUDENT)
@require_POST
def api_planned_absence_submit(request):
    profile = getattr(request.user, "student_profile", None)
    if profile is None:
        return fail("Your student profile is not set up yet.", status=403)
    try:
        planned = submit_planned_absence(
            student=profile,
            from_date=parse_date(request.POST.get("from_date")),
            to_date=parse_date(request.POST.get("to_date")),
            text=request.POST.get("reason", ""),
            subject_ids=clean_object_ids(request.POST.getlist("subjects[]")),
            files=request.FILES.getlist("files[]"),
        )
    except AttendanceError as exc:
        return fail(exc.message, {"reason": exc.message}, status=exc.status, code=exc.code)
    ActivityLog.log(request, action="PLANNED_ABSENCE_SUBMITTED",
                    detail=f"{planned.from_date} – {planned.to_date}")
    return ok({"row": _planned_row(planned)},
              message="Planned absence submitted. Your teachers will review it.")


@role_required(STUDENT)
@require_POST
def api_planned_absence_cancel(request, pk):
    planned = get_object_or_404(PlannedAbsence, pk=pk)
    try:
        cancel_planned_absence(planned=planned, actor=request.user)
    except AttendanceError as exc:
        return fail(exc.message, status=exc.status, code=exc.code)
    return ok(message="Planned absence cancelled.")


@role_required(HEAD, HOD, TEACHER, STUDENT)
@require_GET
def api_planned_absences(request):
    qs = PlannedAbsence.objects.select_related(
        "student", "student__user", "student__batch", "student__department"
    ).prefetch_related("decisions__subject", "decisions__reviewed_by", "attachments")
    user = request.user

    if user.is_student:
        profile = getattr(user, "student_profile", None)
        if profile is None:
            return ok({"rows": [], "pending": 0})
        rows = [_planned_row(p) for p in qs.filter(student=profile)[:300]]
        return ok({"rows": rows,
                   "pending": sum(1 for r in rows if r["status"] == "PENDING")})

    subject_ids = _reviewable_subject_ids(user)
    qs = qs.filter(decisions__subject_id__in=subject_ids, cancelled_at__isnull=True).distinct()
    rows = [_planned_row(p, for_reviewer=True, only_subjects=subject_ids) for p in qs[:500]]
    status = (request.GET.get("status") or "").upper()
    if status in AbsenceReason.Status.values:
        # Filter on the decisions this reviewer owns, not the overall verdict —
        # otherwise a teacher's own pending decision hides behind a colleague's.
        rows = [r for r in rows
                if any(d["status"] == status for d in r["decisions"])]
    return ok({
        "rows": rows,
        "pending": sum(1 for r in rows
                       for d in r["decisions"] if d["status"] == "PENDING"),
    })


@role_required(HEAD, HOD, TEACHER)
@require_POST
def api_planned_decision_review(request, pk):
    decision = get_object_or_404(
        PlannedAbsenceDecision.objects.select_related(
            "planned", "planned__student", "planned__student__batch",
            "subject", "subject__department"),
        pk=pk)
    try:
        review_planned_decision(
            decision=decision, actor=request.user,
            approve=request.POST.get("decision") == "approve",
            remark=request.POST.get("remark", ""))
    except AttendanceError as exc:
        return fail(exc.message, status=exc.status, code=exc.code)
    ActivityLog.log(
        request, action="PLANNED_ABSENCE_REVIEWED",
        detail=f"{decision.status} · {decision.planned.student.name} · {decision.subject.code}")
    return ok(message=f"{decision.subject.code} {decision.get_status_display().lower()}.")


@role_required(HEAD, HOD, TEACHER, STUDENT)
@require_GET
def api_attachment_download(request, pk):
    """
    Stream one piece of evidence to someone entitled to see it.

    Served through Django rather than linked directly from the storage account
    so the permission check happens on every fetch. Three deliberate choices in
    the response:

    * ``Content-Disposition: attachment`` — the file is downloaded, never
      rendered in the origin. Nothing a student uploads gets to execute here.
    * the *sniffed* type, recorded at upload from the file's own bytes, rather
      than anything the uploader asserted.
    * ``X-Content-Type-Options: nosniff`` — so the browser does not go looking
      for a more interesting type than the one we sent.
    """
    attachment = get_object_or_404(
        AbsenceAttachment.objects.select_related(
            "reason", "reason__session", "reason__session__subject",
            "reason__session__subject__department", "reason__student",
            "planned", "planned__student", "planned__student__batch",
        ),
        pk=pk)
    if not can_view_attachment(request.user, attachment):
        # 404, not 403: whether a given attachment exists is itself something
        # the asker is not entitled to learn.
        raise Http404
    response = FileResponse(
        attachment.file.open("rb"),
        content_type=attachment.content_type or "application/octet-stream",
        as_attachment=True,
        filename=attachment.original_name,
    )
    response["X-Content-Type-Options"] = "nosniff"
    return response


@role_required(STUDENT)
@require_GET
def api_my_subjects(request):
    """The subjects a student may narrow a planned absence to."""
    profile = getattr(request.user, "student_profile", None)
    if profile is None:
        return ok({"rows": []})
    rows = [{"id": s.id, "code": s.code, "name": s.name}
            for s in planned_subjects_for(profile)]
    return ok({"rows": rows})
