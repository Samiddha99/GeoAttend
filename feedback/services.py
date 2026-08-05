"""
Feedback rules, and the line between what a student sees and what staff see.

The two serialisers at the bottom are the whole privacy design. `student_row`
may name the student, because it is being handed back to that student.
`staff_*` may not, ever — and there is a test that walks the staff payloads
looking for anything that could identify a respondent.
"""
import datetime as dt
import logging
from collections import Counter

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from academics.selectors import departments_for
from attendance.models import AttendanceRecord

from .models import FeedbackForm, FeedbackRecipient, FeedbackResponse
from .questions import CURRENT_VERSION, QUESTIONS, score_of

log = logging.getLogger("geoattend")


class FeedbackError(Exception):
    def __init__(self, message, code="FEEDBACK_ERROR", status=400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


def conf(key, default):
    return settings.FEEDBACK.get(key, default)


def min_responses():
    """
    How many responses before individual answers are shown — and, because the
    two are the same question asked at different times, the smallest class that
    may be sent a form at all.
    """
    return int(conf("MIN_RESPONSES_TO_REVEAL", 5))


# --------------------------------------------------------------------------- #
#  Sending
# --------------------------------------------------------------------------- #
@transaction.atomic
def send_form(*, session, actor):
    """
    Open a feedback form on one class, addressed to the students who attended.

    Only students recorded present are asked. Someone who was not in the room
    has no view worth collecting, and inviting them would quietly turn this
    into a popularity poll of the whole cohort.
    """
    if session.teacher_id != actor.id and not (actor.is_hod or actor.is_head):
        raise FeedbackError("You can only ask for feedback on your own classes.",
                            "NOT_YOURS", 403)

    max_age = int(conf("MAX_SESSION_AGE_DAYS", 10))
    age = (timezone.localdate() - session.session_date).days
    if age > max_age:
        raise FeedbackError(
            f"That class was {age} days ago. Feedback can only be requested "
            f"within {max_age} days, while students still remember it.",
            "TOO_OLD")
    if age < 0:
        raise FeedbackError("That class has not happened yet.", "IN_FUTURE")

    if hasattr(session, "feedback_form"):
        raise FeedbackError("Feedback has already been requested for this class.",
                            "ALREADY_SENT", 409)

    present = list(
        AttendanceRecord.objects
        .filter(session=session, status__in=[AttendanceRecord.Status.PRESENT,
                                             AttendanceRecord.Status.MANUAL])
        .select_related("student")
    )
    if not present:
        raise FeedbackError(
            "Nobody was marked present in this class, so there is no one to ask.",
            "NO_AUDIENCE")

    # Refused rather than sent-and-hidden. With fewer present than the reveal
    # threshold, even a perfect response rate could never unlock the individual
    # answers — so the form could only ever produce a total. Worse, in a class
    # of three the teacher knows exactly who was there, so even the totals
    # point at people: "two of you said the pace was too fast" identifies
    # nobody on paper and everybody in the room.
    #
    # Asking anyway would collect honest answers under a promise of anonymity
    # the arithmetic cannot keep.
    minimum = min_responses()
    if len(present) < minimum:
        raise FeedbackError(
            f"Only {len(present)} student{'s' if len(present) != 1 else ''} attended "
            f"this class. Feedback needs at least {minimum} so that no answer can "
            "be traced back to the person who gave it.",
            "TOO_FEW_PRESENT")

    hours = int(conf("OPEN_HOURS", 24))
    form = FeedbackForm.objects.create(
        session=session,
        created_by=actor,
        question_version=CURRENT_VERSION,
        expires_at=timezone.now() + dt.timedelta(hours=hours),
        sent_count=len(present),
    )
    FeedbackRecipient.objects.bulk_create([
        FeedbackRecipient(form=form, student=record.student) for record in present
    ])
    return form


# --------------------------------------------------------------------------- #
#  Answering
# --------------------------------------------------------------------------- #
def validate_answers(raw):
    """
    Every question answered, with an option that exists.

    Checked here rather than trusted from the form: a partially answered
    response would skew a question's average without anyone being able to see
    that it had.
    """
    answers, missing = {}, []
    for question in QUESTIONS:
        value = (raw.get(question["key"]) or "").strip()
        if not value:
            missing.append(question["text"])
            continue
        if not any(o["value"] == value for o in question["options"]):
            raise FeedbackError("That is not one of the available answers.",
                                "BAD_OPTION")
        answers[question["key"]] = value
    if missing:
        raise FeedbackError(
            f"Please answer every question — {len(missing)} still to go.",
            "INCOMPLETE")
    return answers


@transaction.atomic
def submit(*, form, student, raw_answers, rating, remarks=""):
    if not form.is_open:
        raise FeedbackError("This feedback form has closed.", "EXPIRED", 410)
    if not FeedbackRecipient.objects.filter(form=form, student=student).exists():
        # Not "you already answered" — this student was never asked, and saying
        # so plainly is better than implying they were.
        raise FeedbackError("This form was not sent to you.", "NOT_A_RECIPIENT", 403)

    try:
        rating = int(rating)
    except (TypeError, ValueError):
        rating = 0
    if not 1 <= rating <= 5:
        raise FeedbackError("Please give a star rating from 1 to 5.", "NO_RATING")

    answers = validate_answers(raw_answers)
    try:
        response = FeedbackResponse.objects.create(
            form=form, student=student, answers=answers, rating=rating,
            remarks=(remarks or "").strip()[:1000])
    except IntegrityError:
        raise FeedbackError("You have already given feedback for this class.",
                            "ALREADY_ANSWERED", 409)

    FeedbackRecipient.objects.filter(form=form, student=student).update(
        responded_at=timezone.now())
    return response


# --------------------------------------------------------------------------- #
#  What a student sees
# --------------------------------------------------------------------------- #
def student_forms(student, *, answered):
    """
    Pending or submitted, never expired-and-unanswered.

    An expired form a student did not answer is in neither list on purpose:
    it is not pending, because nothing can be done about it, and it is not
    submitted. Leaving it in "pending" would build a list of permanent
    reproaches nobody can clear.
    """
    now = timezone.now()
    qs = (FeedbackRecipient.objects
          .filter(student=student)
          .select_related("form", "form__session", "form__session__subject",
                          "form__session__teacher", "form__session__batch"))
    if answered:
        return qs.filter(responded_at__isnull=False)
    return qs.filter(responded_at__isnull=True, form__expires_at__gt=now)


def pending_count(student):
    return student_forms(student, answered=False).count()


def student_row(recipient, response=None):
    """One row for the student's own screen. May name them — it is theirs."""
    form = recipient.form
    session = form.session
    row = {
        "id": form.id,
        "subject": session.subject.code,
        "subject_name": session.subject.name,
        "teacher": session.teacher.full_name or session.teacher.email,
        "batch": session.batch.label,
        "date": session.session_date.strftime("%d %b %Y"),
        "closes_at": timezone.localtime(form.expires_at).strftime("%d %b %Y, %H:%M"),
        "seconds_left": form.seconds_left,
        "submitted_at": (timezone.localtime(recipient.responded_at)
                         .strftime("%d %b %Y, %H:%M") if recipient.responded_at else ""),
    }
    if response is not None:
        row.update({
            "rating": response.rating,
            "remarks": response.remarks,
            "answers": response.answer_labels(),
        })
    return row


# --------------------------------------------------------------------------- #
#  What staff see — no student, ever
# --------------------------------------------------------------------------- #
def visible_forms(user):
    """Forms this member of staff may look at."""
    qs = (FeedbackForm.objects
          .select_related("session", "session__subject", "session__teacher",
                          "session__batch", "session__subject__department"))
    if user.is_head:
        return qs.filter(session__subject__department__institute=user.institute)
    if user.is_hod:
        return qs.filter(session__subject__department__in=departments_for(user))
    if user.is_teacher:
        return qs.filter(session__teacher=user)
    return qs.none()


def summarise(responses):
    """
    Counts, averages and distributions for a set of responses.

    Takes the responses rather than a form so the same function serves one
    class and a teacher's whole term.
    """
    responses = list(responses)
    total = len(responses)
    ratings = [r.rating for r in responses if r.rating]
    scores = [s for s in (r.score for r in responses) if s is not None]

    per_question = []
    for question in QUESTIONS:
        counts = Counter()
        values = []
        for response in responses:
            value = (response.answers or {}).get(question["key"])
            if not value:
                continue
            counts[value] += 1
            score = score_of(question["key"], value)
            if score is not None:
                values.append(score)
        per_question.append({
            "key": question["key"],
            "text": question["text"],
            "group": question["group"],
            "bipolar": question.get("bipolar", False),
            "answered": sum(counts.values()),
            "average": round(sum(values) / len(values) * 100, 1) if values else None,
            "options": [{
                "label": option["label"],
                "value": option["value"],
                "count": counts.get(option["value"], 0),
            } for option in question["options"]],
        })

    return {
        "responses": total,
        "average_rating": round(sum(ratings) / len(ratings), 2) if ratings else None,
        "rating_spread": [sum(1 for r in ratings if r == star) for star in range(1, 6)],
        # One number for the choice questions, as a percentage. Separate from
        # the star rating on purpose: they measure different things and
        # averaging them together would hide when they disagree.
        "score": round(sum(scores) / len(scores) * 100, 1) if scores else None,
        "questions": per_question,
    }


def staff_form_row(form, *, responses=None):
    """A form in the staff list. Deliberately carries no respondent data."""
    session = form.session
    stats = summarise(responses if responses is not None else form.responses.all())
    return {
        "id": form.id,
        "date": session.session_date.strftime("%d %b %Y"),
        "date_iso": session.session_date.isoformat(),
        "subject": session.subject.code,
        "subject_name": session.subject.name,
        "subject_id": session.subject_id,
        "teacher": session.teacher.full_name or session.teacher.email,
        "teacher_id": session.teacher_id,
        "batch": session.batch.label,
        "batch_id": session.batch_id,
        "department": session.subject.department.name,
        "department_id": session.subject.department_id,
        "sent": form.sent_count,
        "responses": stats["responses"],
        "response_rate": (round(stats["responses"] * 100.0 / form.sent_count, 1)
                          if form.sent_count else 0),
        "average_rating": stats["average_rating"],
        "score": stats["score"],
        "open": form.is_open,
        "closes_at": timezone.localtime(form.expires_at).strftime("%d %b %Y, %H:%M"),
    }


def staff_detail(form):
    """
    One form in full — statistics always, individual answers only once enough
    people have replied.

    Below the threshold a class is small enough that a teacher can often work
    out who wrote what, especially from a remark. Withholding the individual
    rows is what makes an honest answer safe to give; the totals stay visible
    so the teacher can still see something.
    """
    responses = list(form.responses.all())
    stats = summarise(responses)
    threshold = min_responses()
    revealed = len(responses) >= threshold

    return {
        "form": staff_form_row(form, responses=responses),
        "stats": stats,
        "revealed": revealed,
        "min_responses": threshold,
        # Order deliberately scrambled by submission time only — never by
        # student, roll or id, which would let two forms be lined up side by
        # side and read off against each other.
        "rows": [{
            "rating": r.rating,
            "remarks": r.remarks,
            "score": round(r.score * 100, 1) if r.score is not None else None,
            "answers": r.answers,
        } for r in sorted(responses, key=lambda r: r.submitted_at)] if revealed else [],
    }


def teacher_summary(user, teacher):
    """Everything a teacher's feedback says, across every form they have sent."""
    forms = list(visible_forms(user).filter(session__teacher=teacher))
    responses = list(FeedbackResponse.objects.filter(form__in=forms))
    stats = summarise(responses)

    # Per-form trend, oldest first, so a chart reads left to right.
    trend = []
    by_form = {}
    for response in responses:
        by_form.setdefault(response.form_id, []).append(response)
    for form in sorted(forms, key=lambda f: f.session.session_date):
        group = by_form.get(form.id, [])
        if not group:
            continue
        group_stats = summarise(group)
        trend.append({
            "date": form.session.session_date.strftime("%d %b"),
            "subject": form.session.subject.code,
            "responses": group_stats["responses"],
            "average_rating": group_stats["average_rating"],
            "score": group_stats["score"],
        })

    return {
        "teacher": teacher.full_name or teacher.email,
        "teacher_id": teacher.id,
        "forms": len(forms),
        "stats": stats,
        "trend": trend,
    }
