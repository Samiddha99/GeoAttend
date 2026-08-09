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
        "subject_type": session.subject.subject_type,
        "degree": session.subject.degree,
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


def totals_for(forms):
    """
    The headline numbers above the table: forms, replies, rating and score.

    Counted over every response in the set, not by averaging each form's own
    average. Those two disagree whenever classes differ in size — a three-reply
    class would otherwise weigh as heavily as a sixty-reply one — and the card
    sits beside a count of responses, so it should be the average of those.

    Deliberately lighter than `summarise()`: this needs two numbers, and doing
    the per-question tally over a thousand forms to reach them is work nobody
    reads.
    """
    ratings, scores = [], []
    for form in forms:
        for response in form.responses.all():
            if response.rating:
                ratings.append(response.rating)
            score = response.score
            if score is not None:
                scores.append(score)
    return {
        "forms": len(forms),
        "responses": sum(len(f.responses.all()) for f in forms),
        "average_rating": round(sum(ratings) / len(ratings), 2) if ratings else None,
        "score": round(sum(scores) / len(scores) * 100, 1) if scores else None,
    }


def remarks_from(forms):
    """
    Every written remark across a set of forms, newest first.

    Gathered per form and only from forms that reached the reveal threshold —
    the risk of a remark being attributed is set by the class it came from, not
    by the size of the pool it is later aggregated into. Pooling a 2-response
    class into a teacher's 200 would hide the arithmetic, not the author.

    Returns (remarks, withheld) so the screen can say how many are missing
    rather than quietly showing fewer than exist.
    """
    threshold = min_responses()
    remarks, withheld = [], 0
    for form in forms:
        responses = list(form.responses.all())
        written = [r for r in responses if (r.remarks or "").strip()]
        if len(responses) < threshold:
            withheld += len(written)
            continue
        for response in written:
            remarks.append({
                "text": response.remarks,
                "rating": response.rating,
                "score": (round(response.score * 100, 1)
                          if response.score is not None else None),
                "date": form.session.session_date.strftime("%d %b %Y"),
                "date_iso": form.session.session_date.isoformat(),
                "subject": form.session.subject.code,
                "subject_type": form.session.subject.subject_type,
                "degree": form.session.subject.degree,
                "teacher": (form.session.teacher.full_name
                            or form.session.teacher.email),
                "batch": form.session.batch.label,
            })
    remarks.sort(key=lambda r: r["date_iso"], reverse=True)
    return remarks, withheld


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
        "subject_type": session.subject.subject_type,
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
    remarks = remarks_from([form])

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
        # Split out from `rows` so the remarks tab has one shape to render
        # whether it is showing a class, a teacher or a subject.
        "remarks": remarks[0],
        "remarks_withheld": remarks[1],
    }


# --------------------------------------------------------------------------- #
#  Rolling the same responses up different ways
# --------------------------------------------------------------------------- #
#
# One description per grouping rather than three near-identical functions. The
# only things that actually differ are which field identifies the group and
# what to call it on screen; everything else — filtering, summarising, the
# per-form trend — is the same work, and three copies of it would drift.
GROUPINGS = {
    "teachers": {
        "label": "Teacher",
        "key": lambda form: form.session.teacher_id,
        "name": lambda form: (form.session.teacher.full_name
                              or form.session.teacher.email),
        "extra": lambda form: form.session.subject.department.name,
    },
    "subjects": {
        "label": "Subject",
        "key": lambda form: form.session.subject_id,
        "name": lambda form: (f"{form.session.subject.code} — "
                              f"{form.session.subject.name}"),
        "extra": lambda form: form.session.subject.department.name,
    },
    "departments": {
        "label": "Department",
        "key": lambda form: form.session.subject.department_id,
        "name": lambda form: form.session.subject.department.name,
        "extra": lambda form: "",
    },
    "batches": {
        "label": "Batch",
        "key": lambda form: form.session.batch_id,
        "name": lambda form: form.session.batch.label,
        "extra": lambda form: form.session.batch.department.name,
    },
}


def filtered_forms(user, params):
    """
    The forms this person may see, narrowed by the filter bar.

    Shared by every tab so a filter means the same thing wherever it is
    applied — and so a new filter has one place to be added rather than four.
    """
    from core.utils import clean_object_id, parse_date

    from academics.models import Degree, SubjectType

    qs = visible_forms(user).prefetch_related("responses")
    for field, param in (("session__subject_id", "subject"),
                         ("session__teacher_id", "teacher"),
                         ("session__batch_id", "batch"),
                         ("session__subject__department_id", "department")):
        value = clean_object_id(params.get(param))
        if value:
            qs = qs.filter(**{field: value})

    # Not an id, so it does not go through clean_object_id. Unrecognised values
    # are dropped rather than matched — an empty page is indistinguishable from
    # a real "no feedback yet", and the wrong one of those is misleading.
    subject_type = (params.get("subject_type") or "").strip().upper()
    if subject_type in SubjectType.values:
        qs = qs.filter(session__subject__subject_type=subject_type)
    degree = (params.get("degree") or "").strip().upper()
    if degree in Degree.values:
        qs = qs.filter(session__subject__degree=degree)

    # A single date and a range are the same filter with the ends collapsed.
    on = parse_date(params.get("date"))
    if on:
        return qs.filter(session__session_date=on)
    start, end = parse_date(params.get("start")), parse_date(params.get("end"))
    if start:
        qs = qs.filter(session__session_date__gte=start)
    if end:
        qs = qs.filter(session__session_date__lte=end)
    return qs


def group_rows(forms, kind):
    """One row per teacher, subject, department or batch."""
    spec = GROUPINGS[kind]
    buckets = {}
    for form in forms:
        key = spec["key"](form)
        bucket = buckets.setdefault(key, {
            "id": key,
            "name": spec["name"](form),
            "extra": spec["extra"](form),
            "forms": 0,
            "sent": 0,
            "responses": [],
            "last": form.session.session_date,
        })
        bucket["forms"] += 1
        bucket["sent"] += form.sent_count
        bucket["responses"].extend(form.responses.all())
        bucket["last"] = max(bucket["last"], form.session.session_date)

    rows = []
    for bucket in buckets.values():
        stats = summarise(bucket["responses"])
        rows.append({
            "id": bucket["id"],
            "name": bucket["name"],
            "extra": bucket["extra"],
            "forms": bucket["forms"],
            "sent": bucket["sent"],
            "responses": stats["responses"],
            "response_rate": (round(stats["responses"] * 100.0 / bucket["sent"], 1)
                              if bucket["sent"] else 0),
            "average_rating": stats["average_rating"],
            "score": stats["score"],
            "last": bucket["last"].strftime("%d %b %Y"),
            "last_iso": bucket["last"].isoformat(),
        })
    # Worst first: a list of teaching feedback is read to find what needs
    # attention, and burying that under the best scores helps nobody.
    rows.sort(key=lambda r: (r["score"] is None, r["score"] or 0))
    return rows


def group_detail(forms, kind, pk):
    """
    Everything one group's feedback says, with a trend across its forms.

    Carries no respondent data, like every other staff payload here.
    """
    spec = GROUPINGS[kind]
    mine = [f for f in forms if str(spec["key"](f)) == str(pk)]
    responses = [r for f in mine for r in f.responses.all()]

    trend = []
    for form in sorted(mine, key=lambda f: f.session.session_date):
        group = list(form.responses.all())
        if not group:
            continue
        stats = summarise(group)
        trend.append({
            "date": form.session.session_date.strftime("%d %b"),
            "label": form.session.subject.code,
            "responses": stats["responses"],
            "average_rating": stats["average_rating"],
            "score": stats["score"],
        })

    written, withheld = remarks_from(mine)
    return {
        "kind": kind,
        "label": spec["label"],
        "name": spec["name"](mine[0]) if mine else "",
        "forms": len(mine),
        "stats": summarise(responses),
        "trend": trend,
        "remarks": written,
        "remarks_withheld": withheld,
        "min_responses": min_responses(),
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
