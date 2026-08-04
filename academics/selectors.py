"""
Row-level scoping.  Every list a user can see is derived from these helpers,
so authorisation lives in exactly one place.

**Archived batches.**  Setting ``Batch.is_active = False`` makes that cohort and
everything hanging off it — students, enrolments, attendance sessions, records
and every statistic derived from them — vanish from the whole application.
Nothing is deleted: flipping the flag back restores it all instantly.

The rule is enforced here rather than in each view, so a new screen cannot
forget it.  Only the Batches management page passes ``include_inactive=True``,
because that is where an archived batch is brought back.
"""
from django.db.models import Q

from accounts.models import User

from .models import Batch, Department, Enrollment, Subject, TeacherAssignment


def departments_for(user):
    if not user.is_authenticated:
        return Department.objects.none()
    if user.is_head:
        return Department.objects.filter(institute=user.institute)
    if user.is_hod:
        return Department.objects.filter(Q(hod=user) | Q(pk=user.department_id))
    if user.department_id:
        return Department.objects.filter(pk=user.department_id)
    return Department.objects.none()


def current_department(user):
    return departments_for(user).first()


def subjects_for(user):
    if user.is_head:
        return Subject.objects.filter(department__institute=user.institute)
    if user.is_hod:
        return Subject.objects.filter(department__in=departments_for(user))
    if user.is_teacher:
        # A subject taught only to archived batches drops out of view.
        return Subject.objects.filter(
            assignments__teacher=user, assignments__is_active=True,
            assignments__batch__is_active=True,
        ).distinct()
    if user.is_student:
        profile = getattr(user, "student_profile", None)
        if profile is None or not profile.batch.is_active:
            return Subject.objects.none()
        return Subject.objects.filter(enrollments__student=profile, enrollments__is_active=True).distinct()
    return Subject.objects.none()


def batches_for(user, include_inactive=False):
    """
    Batches this user may see.  Archived ones are hidden unless explicitly asked
    for — only the Batches admin screen does that.
    """
    if user.is_head:
        qs = Batch.objects.filter(department__institute=user.institute)
    elif user.is_hod:
        qs = Batch.objects.filter(department__in=departments_for(user))
    elif user.is_teacher:
        qs = Batch.objects.filter(
            assignments__teacher=user, assignments__is_active=True
        ).distinct()
    elif user.is_student:
        profile = getattr(user, "student_profile", None)
        qs = Batch.objects.filter(pk=profile.batch_id) if profile else Batch.objects.none()
    else:
        return Batch.objects.none()
    return qs if include_inactive else qs.filter(is_active=True)


def teachers_for(user):
    """
    Teachers this user may **manage** — edit, reallocate, deactivate.

    Deliberately narrower than `visible_teachers_for`. Every write endpoint
    gates on this, so a HoD who can see the whole institute still cannot touch
    anyone outside their own department, whatever the browser posts.
    """
    if user.is_head:
        return User.objects.filter(role=User.Role.TEACHER, institute=user.institute)
    if user.is_hod:
        return User.objects.filter(role=User.Role.TEACHER, department__in=departments_for(user))
    return User.objects.none()


def visible_teachers_for(user):
    """
    Teachers this user may **see** — the whole institute.

    Seeing who teaches what across departments is useful (and not sensitive);
    changing them is not the same question, which is why this is separate from
    `teachers_for`. Read scope must never be used to authorise a write.

    Students get a directory rather than a roster: accounts that are dormant
    (deactivated) or not yet claimed (invited but never registered) are staff
    bookkeeping, and listing them to students is confusing at best — a student
    messaging a teacher who has left is a worse outcome than a shorter list.
    """
    if user.is_head or user.is_hod or user.is_teacher:
        return User.objects.filter(role=User.Role.TEACHER, institute=user.institute)
    if user.is_student:
        return User.objects.filter(
            role=User.Role.TEACHER, institute=user.institute,
            is_active=True, registration_completed=True,
        )
    return User.objects.none()


def manageable_department_ids(user):
    """Department ids whose teachers `user` may edit — drives the row flags."""
    if user.is_head:
        return None                      # None means "everything in scope"
    if user.is_hod:
        return set(departments_for(user).values_list("id", flat=True))
    return set()


def students_qs_for(user, include_inactive_batches=False):
    """Students this user may see.  Members of archived batches are excluded."""
    from .models import StudentProfile

    if user.is_head:
        qs = StudentProfile.objects.filter(department__institute=user.institute)
    elif user.is_hod:
        qs = StudentProfile.objects.filter(department__in=departments_for(user))
    elif user.is_teacher:
        # An allocation is (subject, batch) — both halves matter. Matching on
        # subject alone exposed every student taking that subject in *any*
        # batch, including batches this teacher has never taught: a teacher
        # taking DSA for 2022-26 could see the 2021-25 students too.
        #
        # So OR the pairs rather than crossing two flat lists. The list is a
        # teacher's own allocations, so it stays small.
        pairs = TeacherAssignment.objects.filter(
            teacher=user, is_active=True, batch__is_active=True
        ).values_list("subject_id", "batch_id")
        if not pairs:
            return StudentProfile.objects.none()
        match = Q()
        for subject_id, batch_id in pairs:
            match |= Q(enrollments__subject_id=subject_id, batch_id=batch_id)
        qs = StudentProfile.objects.filter(
            match, enrollments__is_active=True
        ).distinct()
    elif user.is_student:
        qs = StudentProfile.objects.filter(user=user)
    else:
        return StudentProfile.objects.none()
    return qs if include_inactive_batches else qs.filter(batch__is_active=True)


def all_students_for(user, include_inactive_batches=False):
    """
    Every student in the institute — the directory scope.

    Distinct from `students_qs_for`, which answers "whose attendance is this
    person responsible for". A teacher needs the wide view to unlink a device
    for a student who is not in their own classes; they still cannot edit or
    deactivate anyone, and those endpoints gate on their own rules.
    """
    from .models import StudentProfile

    if user.is_head or user.is_hod or user.is_teacher:
        qs = StudentProfile.objects.filter(department__institute=user.institute)
    else:
        return StudentProfile.objects.none()
    return qs if include_inactive_batches else qs.filter(batch__is_active=True)


def teacher_subjects_for_batch(teacher, batch):
    """Subjects this teacher is assigned to teach to this batch."""
    return Subject.objects.filter(
        assignments__teacher=teacher,
        assignments__batch=batch,
        assignments__batch__is_active=True,
        assignments__is_active=True,
        is_active=True,
    ).distinct()


def enrolled_students(subject, batch):
    """Students of `batch` who enrolled in `subject` — the attendance audience."""
    from .models import StudentProfile

    return (
        StudentProfile.objects.filter(
            batch=batch,
            batch__is_active=True,
            is_active=True,
            enrollments__subject=subject,
            enrollments__is_active=True,
            user__is_active=True,
        )
        .select_related("user", "batch")
        .distinct()
    )


def can_teach(teacher, subject, batch):
    return TeacherAssignment.objects.filter(
        teacher=teacher, subject=subject, batch=batch,
        batch__is_active=True, is_active=True,
    ).exists()


def is_enrolled(student_profile, subject):
    return Enrollment.objects.filter(
        student=student_profile, subject=subject, is_active=True
    ).exists()
