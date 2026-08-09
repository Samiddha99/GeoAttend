import csv

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET

from academics.selectors import batches_for, departments_for, subjects_for, teachers_for
from accounts.guardians import acting_profile
from attendance.models import AttendanceSession
from core.decorators import guardian_readonly, role_required
from core.http import fail, ok

from .filters import ReportFilters
from . import services as svc

HEAD, HOD, TEACHER, STUDENT, GUARDIAN = (
    "HEAD", "HOD", "TEACHER", "STUDENT", "GUARDIAN")

TEMPLATE_BY_ROLE = {
    HEAD: "dashboard/analytics.html",
    HOD: "dashboard/analytics.html",
    TEACHER: "dashboard/analytics.html",
    STUDENT: "dashboard/student.html",
    # The same screen a student sees. Rendering a second, near-identical
    # template would mean every future change to the student dashboard had to
    # be made twice, and the copy that got forgotten would be the guardian's.
    GUARDIAN: "dashboard/student.html",
}


# --------------------------------------------------------------------------- #
#  Pages
# --------------------------------------------------------------------------- #
@login_required
@ensure_csrf_cookie
def home(request):
    template = TEMPLATE_BY_ROLE.get(request.user.role, "dashboard/student.html")
    context = {
        "departments": departments_for(request.user),
        "batches": batches_for(request.user).select_related("department"),
        "subjects": subjects_for(request.user),
        # Only the semesters that actually exist in scope — a fixed 1..12 list
        # would offer filters that return nothing.
        "semesters": sorted(
            subjects_for(request.user)
            .exclude(semester=None)
            .values_list("semester", flat=True).distinct()),
        "teachers": teachers_for(request.user) if request.user.role in (HEAD, HOD) else [],
        "threshold": settings.ATTENDANCE["LOW_ATTENDANCE_THRESHOLD"],
        "default_start": f"{timezone.localdate().year}-01-01",
        "default_end": timezone.localdate().isoformat(),
        # Shown to the student so the deadline is stated rather than implied.
        "reason_window_days": settings.ATTENDANCE.get("ABSENCE_REASON_DAYS", 3),
    }
    if request.user.is_guardian:
        # The student templates ask "is this mine or am I watching?" in a few
        # places. One flag rather than `user.is_guardian` scattered through the
        # markup, and the child's name so the page can say whose record it is.
        child = acting_profile(request.user)
        context["viewing_child"] = child
        context["read_only"] = True
    if request.user.is_teacher:
        context["open_sessions"] = AttendanceSession.objects.filter(
            teacher=request.user, status=AttendanceSession.Status.OPEN,
            expires_at__gt=timezone.now(),
        ).select_related("subject", "batch")
    return render(request, template, context)


@role_required(HEAD, HOD, TEACHER)
@ensure_csrf_cookie
def reports_page(request):
    return render(request, "dashboard/reports.html", {
        "departments": departments_for(request.user),
        "batches": batches_for(request.user).select_related("department"),
        "subjects": subjects_for(request.user),
        "semesters": sorted(
            subjects_for(request.user)
            .exclude(semester=None)
            .values_list("semester", flat=True).distinct()),
        "teachers": teachers_for(request.user) if request.user.role in (HEAD, HOD) else [],
        "threshold": settings.ATTENDANCE["LOW_ATTENDANCE_THRESHOLD"],
        "default_start": f"{timezone.localdate().year}-01-01",
        "default_end": timezone.localdate().isoformat(),
    })


@role_required(HEAD, HOD, TEACHER)
@ensure_csrf_cookie
def student_detail_page(request, pk):
    student = get_object_or_404(svc.scoped_students(request.user), pk=pk)
    return render(request, "dashboard/student_detail.html", {
        "student": student,
        "default_start": f"{timezone.localdate().year}-01-01",
        "default_end": timezone.localdate().isoformat(),
    })


# --------------------------------------------------------------------------- #
#  Report APIs
# --------------------------------------------------------------------------- #
@login_required
@require_GET
def api_summary(request):
    f = ReportFilters.from_request(request)
    return ok({"kpis": svc.kpi_summary(request.user, f), "filters": f.as_dict()})


@login_required
@require_GET
def api_trend(request):
    f = ReportFilters.from_request(request)
    return ok(svc.daily_trend(request.user, f))


@role_required(HEAD, HOD, TEACHER)
@require_GET
def api_students_report(request):
    f = ReportFilters.from_request(request)
    rows = svc.student_report(request.user, f)
    return ok({"rows": rows, "count": len(rows)})


@role_required(HEAD, HOD, TEACHER)
@require_GET
def api_subjects_report(request):
    f = ReportFilters.from_request(request)
    rows = svc.subject_report(request.user, f)
    return ok({"rows": rows, "count": len(rows)})


@role_required(HEAD, HOD, TEACHER)
@require_GET
def api_batches_report(request):
    f = ReportFilters.from_request(request)
    return ok({"rows": svc.batch_comparison(request.user, f)})


@role_required(HEAD)
@require_GET
def api_departments_report(request):
    f = ReportFilters.from_request(request)
    return ok({"rows": svc.department_comparison(request.user, f)})


@role_required(HEAD, HOD)
@require_GET
def api_teachers_report(request):
    f = ReportFilters.from_request(request)
    return ok({"rows": svc.teacher_activity(request.user, f)})


@role_required(HEAD, HOD, TEACHER)
@require_GET
def api_distribution(request):
    f = ReportFilters.from_request(request)
    return ok({
        "bands": svc.attendance_distribution(request.user, f),
        "hours": svc.hour_distribution(request.user, f),
    })


@role_required(HEAD, HOD, TEACHER)
@require_GET
def api_low_attendance(request):
    f = ReportFilters.from_request(request)
    try:
        threshold = float(request.GET.get("threshold", settings.ATTENDANCE["LOW_ATTENDANCE_THRESHOLD"]))
    except ValueError:
        threshold = 75
    rows = svc.low_attendance(request.user, f, threshold)
    return ok({"rows": rows, "threshold": threshold, "count": len(rows)})


@login_required
@require_GET
def api_student_detail(request, pk=None):
    f = ReportFilters.from_request(request)
    if request.user.is_student or request.user.is_guardian:
        # `pk` is ignored for these two roles — the record they may read is
        # decided by who they are, never by what they ask for.
        student = acting_profile(request.user)
        if student is None:
            return fail("Your student profile is not set up yet.", status=404)
    else:
        student = get_object_or_404(svc.scoped_students(request.user), pk=pk)
    return ok(svc.student_detail(request.user, f, student))


@role_required(STUDENT, GUARDIAN)
@guardian_readonly
@require_GET
def api_my_summary(request):
    f = ReportFilters.from_request(request)
    student = acting_profile(request.user)
    if student is None:
        return fail("Your student profile is not set up yet.", status=404)
    data = svc.student_detail(request.user, f, student)
    # An archived batch yields all-zero figures. Say so explicitly rather than
    # letting a student think their attendance collapsed overnight.
    data["batch_archived"] = not student.batch.is_active
    data["batch_label"] = student.batch.label
    data["open_sessions"] = [{
        "id": s.id, "subject": s.subject.code, "subject_name": s.subject.name,
        "teacher": s.teacher.full_name, "seconds_left": s.seconds_left,
        "url": f"/attendance/mark/{s.token}/",
    } for s in svc.scoped_sessions(request.user).filter(
        status=AttendanceSession.Status.OPEN, expires_at__gt=timezone.now()
    ).exclude(records__student=student).select_related("subject", "teacher")[:5]]
    return ok(data)


# --------------------------------------------------------------------------- #
#  CSV exports
# --------------------------------------------------------------------------- #
def _csv(filename, header, rows):
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    writer = csv.writer(response)
    writer.writerow(header)
    writer.writerows(rows)
    return response


@role_required(HEAD, HOD, TEACHER)
@require_GET
def export_students(request):
    f = ReportFilters.from_request(request)
    rows = svc.student_report(request.user, f)
    body = [[
        r["roll"], r.get("exam_roll", ""), r["name"], r["email"],
        r["department"], r["batch"], r["held"], r["attended"], r["percentage"],
    ] for r in rows]
    return _csv(
        f"student_attendance_{f.start}_{f.end}.csv",
        ["Class Roll", "Exam Roll", "Name", "Email", "Department", "Batch",
         "Classes Held", "Attended", "Percentage"],
        body,
    )


@role_required(HEAD, HOD, TEACHER)
@require_GET
def export_subjects(request):
    f = ReportFilters.from_request(request)
    rows = svc.subject_report(request.user, f)
    body = [[
        r["code"], r["name"], r["department"], r["batch"], r["semester"],
        r["classes"], r["enrolled"], r["students_attended"],
        r["avg_present"], r["percentage"], r["teachers"],
    ] for r in rows]
    return _csv(
        f"subject_attendance_{f.start}_{f.end}.csv",
        ["Code", "Subject", "Department", "Batch", "Semester", "Classes Conducted",
         "Enrolled", "Distinct Students Attended", "Avg Present", "Percentage", "Teachers"],
        body,
    )
