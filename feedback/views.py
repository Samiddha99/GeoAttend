"""
Feedback endpoints.

Split by audience rather than by resource: the student endpoints and the staff
endpoints read the same tables but must never return the same shape, and
keeping them apart in the file is the cheapest way to keep that true.
"""
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from academics.models import Batch, Department, Subject
from academics.selectors import departments_for, visible_teachers_for
from accounts.models import ActivityLog, User
from attendance.models import AttendanceSession
from core.decorators import role_required
from core.http import fail, ok
from core.utils import clean_object_id, parse_date

from . import services as svc
from .models import FeedbackForm, FeedbackRecipient, FeedbackResponse
from .questions import as_payload
from .services import FeedbackError

HEAD, HOD, TEACHER, STUDENT = "HEAD", "HOD", "TEACHER", "STUDENT"


# --------------------------------------------------------------------------- #
#  Teacher: asking
# --------------------------------------------------------------------------- #
@role_required(TEACHER, HOD, HEAD)
@require_POST
def api_send(request, pk):
    session = get_object_or_404(
        AttendanceSession.objects.select_related("subject", "batch"), pk=pk)
    try:
        form = svc.send_form(session=session, actor=request.user)
    except FeedbackError as exc:
        return fail(exc.message, status=exc.status, code=exc.code)
    ActivityLog.log(request, action="FEEDBACK_SENT",
                    detail=f"{session.subject.code} · {form.sent_count} students")
    return ok({"id": form.id, "sent": form.sent_count},
              message=(f"Feedback form sent to {form.sent_count} students. "
                       "It stays open for 24 hours."))


# --------------------------------------------------------------------------- #
#  Student
# --------------------------------------------------------------------------- #
@role_required(STUDENT)
@ensure_csrf_cookie
def my_feedback_page(request):
    return render(request, "feedback/my_feedback.html", {"questions": as_payload()})


@role_required(STUDENT)
@require_GET
def api_my_feedback(request):
    profile = getattr(request.user, "student_profile", None)
    if profile is None:
        return ok({"pending": [], "submitted": [], "pending_count": 0})

    pending = [svc.student_row(r)
               for r in svc.student_forms(profile, answered=False)]

    answered = list(svc.student_forms(profile, answered=True))
    responses = {
        r.form_id: r for r in FeedbackResponse.objects.filter(
            student=profile, form__in=[r.form_id for r in answered])
    }
    submitted = [svc.student_row(r, responses.get(r.form_id)) for r in answered]

    return ok({"pending": pending, "submitted": submitted,
               "pending_count": len(pending)})


@role_required(STUDENT)
@require_GET
def api_form(request, pk):
    """The questions, for a form this student still owes an answer to."""
    profile = getattr(request.user, "student_profile", None)
    recipient = get_object_or_404(
        FeedbackRecipient.objects.select_related(
            "form", "form__session", "form__session__subject",
            "form__session__teacher", "form__session__batch"),
        form_id=pk, student=profile)
    if recipient.responded_at:
        return fail("You have already given feedback for this class.",
                    status=409, code="ALREADY_ANSWERED")
    if not recipient.form.is_open:
        return fail("This feedback form has closed.", status=410, code="EXPIRED")
    return ok({"form": svc.student_row(recipient), "questions": as_payload()})


@role_required(STUDENT)
@require_POST
def api_submit(request, pk):
    profile = getattr(request.user, "student_profile", None)
    form = get_object_or_404(FeedbackForm, pk=pk)
    raw = {q["key"]: request.POST.get(q["key"], "") for q in as_payload()}
    try:
        svc.submit(form=form, student=profile, raw_answers=raw,
                   rating=request.POST.get("rating"),
                   remarks=request.POST.get("remarks", ""))
    except FeedbackError as exc:
        return fail(exc.message, status=exc.status, code=exc.code)
    return ok(message="Thank you — your feedback has been recorded anonymously.")


# --------------------------------------------------------------------------- #
#  Staff
# --------------------------------------------------------------------------- #
@role_required(TEACHER, HOD, HEAD)
@ensure_csrf_cookie
def feedback_page(request):
    institute = request.user.institute
    return render(request, "feedback/feedback.html", {
        "departments": departments_for(request.user),
        "subjects": Subject.objects.filter(
            department__institute=institute, is_active=True).order_by("code"),
        "batches": Batch.objects.filter(
            department__institute=institute, is_active=True).order_by("-start_year"),
        "teachers": visible_teachers_for(request.user),
        "min_responses": svc.min_responses(),
    })


@role_required(TEACHER, HOD, HEAD)
@require_GET
def api_forms(request):
    """One row per class."""
    forms = list(svc.filtered_forms(request.user, request.GET)[:500])
    return ok({
        "rows": [svc.staff_form_row(f) for f in forms],
        "totals": svc.totals_for(forms),
    })


@role_required(TEACHER, HOD, HEAD)
@require_GET
def api_form_detail(request, pk):
    form = get_object_or_404(
        svc.visible_forms(request.user).prefetch_related("responses"), pk=pk)
    return ok(svc.staff_detail(form))


@role_required(TEACHER, HOD, HEAD)
@require_GET
def api_groups(request, kind):
    """
    The same forms rolled up by teacher, subject, department or batch.

    Grouped in Python rather than by the database: the filtered set is already
    loaded with its responses, and the scoring rules that turn answers into a
    number live in Python anyway — an aggregate query would have to duplicate
    them and could not stay in step.
    """
    if kind not in svc.GROUPINGS:
        raise Http404
    forms = list(svc.filtered_forms(request.user, request.GET)[:1000])
    rows = svc.group_rows(forms, kind)
    return ok({
        "rows": rows,
        "label": svc.GROUPINGS[kind]["label"],
        # `groups` is the only figure the rollup adds; the rest are the same
        # responses counted the same way as on the Classes tab, so switching
        # tabs never makes the headline numbers move.
        "totals": dict(svc.totals_for(forms), groups=len(rows)),
    })


@role_required(TEACHER, HOD, HEAD)
@require_GET
def api_group_detail(request, kind, pk):
    if kind not in svc.GROUPINGS:
        raise Http404
    forms = list(svc.filtered_forms(request.user, request.GET)[:1000])
    return ok(svc.group_detail(forms, kind, pk))
