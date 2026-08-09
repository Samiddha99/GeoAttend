from django.conf import settings


def site(request):
    """Expose a few globals to every template."""
    from academics.selectors import subject_type_options

    user = getattr(request, "user", None)
    return {
        "SITE_NAME": settings.SITE_NAME,
        "SITE_URL": settings.SITE_URL,
        "ATT_CONF": settings.ATTENDANCE,
        # A global because the subject-type filter appears on nine pages owned
        # by four apps. Threading it through nine view contexts means nine
        # chances to forget, and the symptom of forgetting — a filter with no
        # options — is easy to miss in review.
        "subject_types": subject_type_options(),
        # Whose record a guardian is looking at, and who else they could pick.
        # A global for the same reason as the types above: the bar appears on
        # every page a guardian can reach, and a view that forgot to supply it
        # would silently render a page with no indication whose data it shows.
        "guardian_child": guardian_child(request),
        "guardian_children": guardian_children(request),
        "current_role": getattr(user, "role", None) if getattr(user, "is_authenticated", False) else None,
        "pending_reviews": pending_review_count(user),
        "pending_feedback": pending_feedback_count(user),
    }


def guardian_child(request):
    """
    The student a guardian is viewing — already resolved by the middleware, so
    this is a read rather than another set of queries.
    """
    user = getattr(request, "user", None)
    if not getattr(user, "is_authenticated", False) or not user.is_guardian:
        return None
    return getattr(request, "guardian_child", None)


def guardian_children(request):
    """The switcher rows, or [] for anyone with fewer than two children."""
    user = getattr(request, "user", None)
    if not getattr(user, "is_authenticated", False) or not user.is_guardian:
        return []

    from accounts.guardians import child_options

    return child_options(request)


def pending_feedback_count(user):
    """
    Feedback forms a student still owes, for the sidebar badge.

    Expired forms are not counted: they cannot be answered, so listing them
    would build a badge that never clears and that the student can do nothing
    about.
    """
    if not getattr(user, "is_authenticated", False) or user.role != "STUDENT":
        return 0
    profile = getattr(user, "student_profile", None)
    if profile is None:
        return 0

    from feedback.services import pending_count

    return pending_count(profile)


def pending_review_count(user):
    """
    How many absence decisions are waiting on this person.

    Rendered server-side so the badge is correct on first paint rather than
    appearing a moment later; the reasons page then keeps it live from data it
    already loads, without another round trip.

    Returns 0 for anyone who cannot review, so the badge simply never shows.
    """
    if not getattr(user, "is_authenticated", False):
        return 0
    if user.role not in ("HEAD", "HOD", "TEACHER"):
        return 0

    # Imported here: this module is loaded during settings/template setup, and
    # importing models at module scope would drag the app registry in too early.
    from academics.models import Subject, TeacherAssignment
    from academics.selectors import departments_for
    from attendance.models import AbsenceReason, PlannedAbsenceDecision

    reasons = AbsenceReason.objects.filter(status=AbsenceReason.Status.PENDING)
    decisions = PlannedAbsenceDecision.objects.filter(
        status=AbsenceReason.Status.PENDING, planned__cancelled_at__isnull=True)

    if user.is_teacher:
        # The same scoping the two queues use: sessions this teacher ran, and
        # planned absences touching subjects they are allocated to.
        reasons = reasons.filter(session__teacher=user)
        decisions = decisions.filter(
            subject__in=TeacherAssignment.objects.filter(
                teacher=user, is_active=True).values("subject_id"))
    elif user.is_hod:
        departments = departments_for(user)
        reasons = reasons.filter(session__subject__department__in=departments)
        decisions = decisions.filter(subject__department__in=departments)
    else:
        subjects = Subject.objects.filter(department__institute=user.institute)
        reasons = reasons.filter(session__subject__in=subjects)
        decisions = decisions.filter(subject__in=subjects)

    return reasons.count() + decisions.count()
