"""
Class feedback: one short form per class, answered anonymously.

"Anonymously" needs care here, because a student can look back at what they
wrote — which means the answers are stored against them. The privacy guarantee
is therefore a property of the *code*, not of the schema: nothing on the staff
side ever selects, serialises or aggregates by student. `feedback/services.py`
keeps the two serialisers apart for exactly that reason, and a test asserts no
staff-facing payload carries a student identifier.

Worth being clear-eyed: this is weaker than storing answers with no owner at
all. One careless query or one database export undoes it. It is the trade made
in exchange for a student being able to see their own submission.
"""
from django.conf import settings
from django.db import models
from django.utils import timezone

from academics.models import StudentProfile
from attendance.models import AttendanceSession

from .questions import CURRENT_VERSION, QUESTION_INDEX, score_of


class FeedbackForm(models.Model):
    """One request for feedback on one class."""

    session = models.OneToOneField(
        AttendanceSession, on_delete=models.CASCADE, related_name="feedback_form"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name="feedback_forms_sent",
    )
    # Which question set produced the stored answers. Answers outlive the code
    # that asked for them, so a form that cannot say which version it used is a
    # form nobody can read back with confidence.
    question_version = models.PositiveSmallIntegerField(default=CURRENT_VERSION)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    expires_at = models.DateTimeField(db_index=True)
    # Fixed at send time. Marking someone present afterwards does not quietly
    # add them to a form that has been open for twenty hours.
    sent_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["-created_at"])]

    def __str__(self):
        return f"Feedback · {self.session}"

    @property
    def is_open(self):
        return timezone.now() < self.expires_at

    @property
    def seconds_left(self):
        return max(int((self.expires_at - timezone.now()).total_seconds()), 0)


class FeedbackRecipient(models.Model):
    """
    A student the form was sent to.

    Snapshotted rather than recomputed: "who was present" is a moving target —
    a teacher can mark someone present an hour later — and a student's list of
    pending forms should not change under them.
    """

    form = models.ForeignKey(
        FeedbackForm, on_delete=models.CASCADE, related_name="recipients"
    )
    student = models.ForeignKey(
        StudentProfile, on_delete=models.CASCADE, related_name="feedback_requests"
    )
    responded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["student__class_roll"]
        constraints = [
            models.UniqueConstraint(fields=["form", "student"],
                                    name="uniq_feedback_recipient"),
        ]
        indexes = [models.Index(fields=["student", "responded_at"])]

    def __str__(self):
        return f"{self.student} · {self.form_id}"


class FeedbackResponse(models.Model):
    """
    One student's answers.

    `student` exists so they can read their own submission back. Every staff
    query in this project must go through the serialisers in services.py, which
    never touch it.
    """

    form = models.ForeignKey(
        FeedbackForm, on_delete=models.CASCADE, related_name="responses"
    )
    student = models.ForeignKey(
        StudentProfile, on_delete=models.CASCADE, related_name="feedback_responses"
    )
    # {question key: chosen option value}
    answers = models.JSONField(default=dict)
    rating = models.PositiveSmallIntegerField(default=0, help_text="1–5 stars")
    remarks = models.TextField(max_length=1000, blank=True)
    submitted_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["submitted_at"]
        constraints = [
            models.UniqueConstraint(fields=["form", "student"],
                                    name="uniq_feedback_response_per_student"),
        ]
        indexes = [models.Index(fields=["form", "submitted_at"])]

    def __str__(self):
        return f"Response to {self.form_id}"

    @property
    def score(self):
        """
        The answers as a single 0–1 number, or None if nothing was scorable.

        Unscored options — "board not used" — are left out rather than counted
        as zero. A teacher who taught from slides should not be marked down for
        board work that never happened.
        """
        scores = [s for s in (score_of(k, v) for k, v in (self.answers or {}).items())
                  if s is not None]
        return round(sum(scores) / len(scores), 4) if scores else None

    def answer_labels(self):
        """The submission in words, for the student reading it back."""
        rows = []
        for key, value in (self.answers or {}).items():
            question = QUESTION_INDEX.get(key)
            if question is None:
                continue
            option = next((o for o in question["options"] if o["value"] == value), None)
            rows.append({
                "key": key,
                "text": question["text"],
                "answer": option["label"] if option else value,
            })
        return rows
