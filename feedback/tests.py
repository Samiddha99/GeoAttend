"""
Class feedback: the rules, the scoring, and the privacy boundary.

The last of those is the one worth guarding hardest. A student can read their
own submission back, which means answers are stored against them — so nothing
prevents a staff-facing payload leaking a respondent except the code that
builds it. `test_no_staff_payload_can_identify_a_respondent` walks those
payloads looking for exactly that.
"""
import datetime as dt

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from feedback import services as svc
from feedback.questions import QUESTION_INDEX as QUESTION_INDEX_FOR_TEST
from feedback.questions import QUESTIONS, score_of
from feedback.services import FeedbackError


class QuestionSetTests(SimpleTestCase):
    """The form itself, before anyone has answered it."""

    def test_every_question_is_asked_positively(self):
        """
        A negative stem makes an agreeing student pick the option that looks
        like a complaint, and the results read backwards until someone notices.
        """
        for question in QUESTIONS:
            with self.subTest(key=question["key"]):
                text = question["text"].lower()
                for phrase in ("not ", "n't", "fail", "unable", "poor", "late"):
                    self.assertNotIn(phrase, text)

    def test_the_form_covers_what_was_asked_for(self):
        keys = {q["key"] for q in QUESTIONS}
        for expected in ("punctuality", "subject_knowledge", "depth", "explanation",
                         "real_world", "queries", "audible", "pace", "english",
                         "board_work"):
            self.assertIn(expected, keys)

    def test_ordinary_questions_run_best_to_worst(self):
        for question in QUESTIONS:
            if question.get("bipolar") or question.get("descriptive"):
                continue
            scored = [o["score"] for o in question["options"] if o["score"] is not None]
            with self.subTest(key=question["key"]):
                self.assertEqual(scored, sorted(scored, reverse=True))
                self.assertEqual(scored[0], 1.0)

    def test_questions_are_grouped_together(self):
        """
        Each group appears once, as a run. Interleaved groups would print the
        same heading several times and read as a muddle.
        """
        from feedback.questions import GROUP_ORDER

        seen, order = [], [q["group"] for q in QUESTIONS]
        for group in order:
            if not seen or seen[-1] != group:
                seen.append(group)
        self.assertEqual(len(seen), len(set(seen)), f"groups interleave: {order}")
        self.assertEqual(seen, [g for g in GROUP_ORDER if g in seen])

    def test_the_language_question_asks_about_english(self):
        """
        Scored like any other scale, so the options carry colours. Worth
        remembering that this encodes a policy — more English rates higher —
        rather than a fact about teaching.
        """
        from feedback.questions import QUESTION_INDEX

        question = QUESTION_INDEX["english"]
        self.assertIn("English", question["text"])
        self.assertNotIn("language", QUESTION_INDEX)
        tones = {o["value"]: o["tone"] for o in question["options"]}
        self.assertEqual(tones["entirely"], "good")
        self.assertEqual(tones["mostly_other"], "bad")
        self.assertEqual(score_of("english", "entirely"), 1.0)

    def test_every_option_carries_a_colour_that_matches_its_score(self):
        """
        The tone is derived from the score, so a green option worth 0.2 — worse
        than no colour at all — cannot happen.
        """
        for question in QUESTIONS:
            for option in question["options"]:
                with self.subTest(key=question["key"], option=option["value"]):
                    tone, score = option["tone"], option["score"]
                    if score is None:
                        self.assertEqual(tone, "neutral")
                    elif tone == "good":
                        self.assertGreaterEqual(score, 0.85)
                    elif tone == "bad":
                        self.assertLess(score, 0.30)

    def test_the_best_and_worst_options_are_green_and_red(self):
        tones = {o["value"]: o["tone"]
                 for o in QUESTION_INDEX_FOR_TEST["punctuality"]["options"]}
        self.assertEqual(tones["always"], "good")
        self.assertEqual(tones["rarely"], "bad")
        pace = {o["value"]: o["tone"]
                for o in QUESTION_INDEX_FOR_TEST["pace"]["options"]}
        self.assertEqual(pace["just_right"], "good")
        self.assertEqual(pace["too_fast"], "bad")
        self.assertEqual(pace["too_slow"], "bad")

    def test_pace_scores_highest_in_the_middle(self):
        """
        "Too fast" and "too slow" are both wrong and sit at opposite ends. If
        pace were scored by position, a class split between the two would
        average out to a comfortable pace nobody actually experienced.
        """
        self.assertEqual(score_of("pace", "just_right"), 1.0)
        self.assertLess(score_of("pace", "too_fast"), score_of("pace", "a_bit_fast"))
        self.assertLess(score_of("pace", "too_slow"), score_of("pace", "a_bit_slow"))
        self.assertEqual(score_of("pace", "too_fast"), score_of("pace", "too_slow"))

    def test_board_not_used_is_recorded_but_not_scored(self):
        """A teacher who taught from slides is not marked down for board work."""
        self.assertIsNone(score_of("board_work", "not_used"))
        self.assertIsNotNone(score_of("board_work", "very_clear"))


class ScoringTests(SimpleTestCase):
    """Aggregation, without a database."""

    class _Response:
        def __init__(self, answers, rating=0):
            self.answers = answers
            self.rating = rating

        @property
        def score(self):
            values = [s for s in (score_of(k, v) for k, v in self.answers.items())
                      if s is not None]
            return sum(values) / len(values) if values else None

    def test_an_empty_set_reports_nothing_rather_than_zero(self):
        """Zero and "nobody answered" are different things on a dashboard."""
        stats = svc.summarise([])
        self.assertEqual(stats["responses"], 0)
        self.assertIsNone(stats["average_rating"])
        self.assertIsNone(stats["score"])

    def test_it_counts_each_option(self):
        stats = svc.summarise([
            self._Response({"punctuality": "always"}, rating=5),
            self._Response({"punctuality": "always"}, rating=4),
            self._Response({"punctuality": "sometimes"}, rating=3),
        ])
        question = next(q for q in stats["questions"] if q["key"] == "punctuality")
        counts = {o["label"]: o["count"] for o in question["options"]}
        self.assertEqual(counts["Always"], 2)
        self.assertEqual(counts["Sometimes"], 1)
        self.assertEqual(stats["average_rating"], 4.0)
        self.assertEqual(stats["rating_spread"], [0, 0, 1, 1, 1])

    def test_an_unscored_answer_is_counted_but_left_out_of_the_average(self):
        stats = svc.summarise([
            self._Response({"board_work": "not_used"}),
            self._Response({"board_work": "very_clear"}),
        ])
        question = next(q for q in stats["questions"] if q["key"] == "board_work")
        self.assertEqual(question["answered"], 2)
        self.assertEqual(question["average"], 100.0)   # only the scored one


class SendRuleTests(TestCase):
    def setUp(self):
        from academics.models import Batch, Department, Enrollment, StudentProfile, Subject
        from accounts.models import Institute, User
        from attendance.models import AttendanceRecord, AttendanceSession

        self.institute = Institute.objects.create(name="I", code="I", email="i@i.edu")
        self.cse = Department.objects.create(institute=self.institute, name="CSE", code="CSE")
        self.dsa = Subject.objects.create(department=self.cse, code="DSA", name="DS")
        self.batch = Batch.objects.create(department=self.cse, label="2022-26",
                                          start_year=2022, end_year=2026)

        def user(email, role):
            return User.objects.create_user(
                email=email, password="Str0ngPass!23", role=role,
                institute=self.institute, department=self.cse,
                registration_completed=True, full_name=email)

        self.teacher = user("t@i.edu", "TEACHER")
        self.other_teacher = user("t2@i.edu", "TEACHER")
        self.students = []
        for i in range(6):
            u = user(f"s{i}@i.edu", "STUDENT")
            profile = StudentProfile.objects.create(
                user=u, department=self.cse, batch=self.batch, class_roll=f"0{i}")
            Enrollment.objects.create(student=profile, subject=self.dsa)
            self.students.append(profile)

        self.session = AttendanceSession.objects.create(
            teacher=self.teacher, subject=self.dsa, batch=self.batch,
            session_date=timezone.localdate() - dt.timedelta(days=1),
            latitude=22.5, longitude=88.3, expected_count=6,
            expires_at=timezone.now())
        # Five present, one absent.
        for profile in self.students[:5]:
            AttendanceRecord.objects.create(
                session=self.session, student=profile,
                status=AttendanceRecord.Status.PRESENT)

    def _answers(self):
        return {q["key"]: q["options"][0]["value"] for q in QUESTIONS}

    # ------------------------------------------------------------- sending
    def test_only_students_who_attended_are_asked(self):
        """
        Someone who was not in the room has no view worth collecting, and
        asking them would turn this into a popularity poll of the whole cohort.
        """
        form = svc.send_form(session=self.session, actor=self.teacher)
        self.assertEqual(form.sent_count, 5)
        asked = {r.student_id for r in form.recipients.all()}
        self.assertNotIn(self.students[5].id, asked)

    def test_another_teacher_cannot_ask_on_your_behalf(self):
        with self.assertRaises(FeedbackError) as ctx:
            svc.send_form(session=self.session, actor=self.other_teacher)
        self.assertEqual(ctx.exception.code, "NOT_YOURS")

    def test_a_class_older_than_the_window_is_refused(self):
        self.session.session_date = timezone.localdate() - dt.timedelta(days=11)
        self.session.save(update_fields=["session_date"])
        with self.assertRaises(FeedbackError) as ctx:
            svc.send_form(session=self.session, actor=self.teacher)
        self.assertEqual(ctx.exception.code, "TOO_OLD")

    def test_asking_twice_for_the_same_class_is_refused(self):
        svc.send_form(session=self.session, actor=self.teacher)
        with self.assertRaises(FeedbackError) as ctx:
            svc.send_form(session=self.session, actor=self.teacher)
        self.assertEqual(ctx.exception.code, "ALREADY_SENT")

    def test_a_class_too_small_to_stay_anonymous_is_refused(self):
        """
        Not sent-and-hidden. Below the reveal threshold even a perfect response
        rate could never unlock the individual answers — and in a class of
        three the teacher knows exactly who was there, so the totals point at
        people whatever the screen says. Asking anyway would collect honest
        answers under a promise the arithmetic cannot keep.
        """
        from attendance.models import AttendanceRecord

        # Leave three of the five present.
        AttendanceRecord.objects.filter(
            session=self.session, student__in=self.students[3:5]).delete()
        with self.assertRaises(FeedbackError) as ctx:
            svc.send_form(session=self.session, actor=self.teacher)
        self.assertEqual(ctx.exception.code, "TOO_FEW_PRESENT")
        self.assertIn("3 students", ctx.exception.message)
        self.assertFalse(hasattr(self.session, "feedback_form"))

    def test_exactly_the_threshold_is_allowed(self):
        """Five present, five needed — the boundary belongs on the yes side."""
        form = svc.send_form(session=self.session, actor=self.teacher)
        self.assertEqual(form.sent_count, 5)

    def test_the_minimum_class_size_follows_the_reveal_threshold(self):
        """
        One setting, because they are the same question asked at different
        times: how many people does it take before an answer stops being
        attributable?
        """
        from django.conf import settings
        from attendance.models import AttendanceRecord

        AttendanceRecord.objects.filter(
            session=self.session, student__in=self.students[2:5]).delete()
        conf = {**settings.FEEDBACK, "MIN_RESPONSES_TO_REVEAL": 2}
        with override_settings(FEEDBACK=conf):
            form = svc.send_form(session=self.session, actor=self.teacher)
        self.assertEqual(form.sent_count, 2)

    def test_a_class_nobody_attended_has_nobody_to_ask(self):
        from attendance.models import AttendanceRecord

        AttendanceRecord.objects.filter(session=self.session).delete()
        with self.assertRaises(FeedbackError) as ctx:
            svc.send_form(session=self.session, actor=self.teacher)
        self.assertEqual(ctx.exception.code, "NO_AUDIENCE")

    # ----------------------------------------------------------- answering
    def test_a_student_answers_once(self):
        form = svc.send_form(session=self.session, actor=self.teacher)
        svc.submit(form=form, student=self.students[0],
                   raw_answers=self._answers(), rating=5)
        with self.assertRaises(FeedbackError) as ctx:
            svc.submit(form=form, student=self.students[0],
                       raw_answers=self._answers(), rating=1)
        self.assertEqual(ctx.exception.code, "ALREADY_ANSWERED")

    def test_someone_who_was_not_asked_cannot_answer(self):
        form = svc.send_form(session=self.session, actor=self.teacher)
        with self.assertRaises(FeedbackError) as ctx:
            svc.submit(form=form, student=self.students[5],
                       raw_answers=self._answers(), rating=5)
        self.assertEqual(ctx.exception.code, "NOT_A_RECIPIENT")

    def test_a_half_answered_form_is_refused(self):
        """One skipped question would skew that question's average invisibly."""
        form = svc.send_form(session=self.session, actor=self.teacher)
        answers = self._answers()
        answers.pop("pace")
        with self.assertRaises(FeedbackError) as ctx:
            svc.submit(form=form, student=self.students[0],
                       raw_answers=answers, rating=5)
        self.assertEqual(ctx.exception.code, "INCOMPLETE")

    def test_a_rating_is_required(self):
        form = svc.send_form(session=self.session, actor=self.teacher)
        with self.assertRaises(FeedbackError) as ctx:
            svc.submit(form=form, student=self.students[0],
                       raw_answers=self._answers(), rating=0)
        self.assertEqual(ctx.exception.code, "NO_RATING")

    def test_a_closed_form_cannot_be_answered(self):
        form = svc.send_form(session=self.session, actor=self.teacher)
        form.expires_at = timezone.now() - dt.timedelta(minutes=1)
        form.save(update_fields=["expires_at"])
        with self.assertRaises(FeedbackError) as ctx:
            svc.submit(form=form, student=self.students[0],
                       raw_answers=self._answers(), rating=4)
        self.assertEqual(ctx.exception.code, "EXPIRED")

    # ------------------------------------------------- the student's lists
    def test_an_expired_unanswered_form_appears_in_neither_list(self):
        """
        It is not pending — nothing can be done about it — and it was not
        submitted. Leaving it in "pending" would build a list of permanent
        reproaches the student cannot clear.
        """
        form = svc.send_form(session=self.session, actor=self.teacher)
        form.expires_at = timezone.now() - dt.timedelta(minutes=1)
        form.save(update_fields=["expires_at"])
        student = self.students[0]
        self.assertEqual(svc.student_forms(student, answered=False).count(), 0)
        self.assertEqual(svc.student_forms(student, answered=True).count(), 0)
        self.assertEqual(svc.pending_count(student), 0)

    def test_answering_moves_a_form_from_pending_to_submitted(self):
        form = svc.send_form(session=self.session, actor=self.teacher)
        student = self.students[0]
        self.assertEqual(svc.pending_count(student), 1)
        svc.submit(form=form, student=student, raw_answers=self._answers(), rating=4)
        self.assertEqual(svc.pending_count(student), 0)
        self.assertEqual(svc.student_forms(student, answered=True).count(), 1)

    # --------------------------------------------------------- the results
    def _fill(self, count):
        form = svc.send_form(session=self.session, actor=self.teacher)
        for profile in self.students[:count]:
            svc.submit(form=form, student=profile,
                       raw_answers=self._answers(), rating=4, remarks="Good class")
        return form

    def test_individual_answers_stay_hidden_in_a_small_class(self):
        form = self._fill(3)
        detail = svc.staff_detail(form)
        self.assertFalse(detail["revealed"])
        self.assertEqual(detail["rows"], [])
        # The totals still show — a teacher can see that people are replying.
        self.assertEqual(detail["stats"]["responses"], 3)

    def test_individual_answers_appear_once_enough_have_replied(self):
        form = self._fill(5)
        detail = svc.staff_detail(form)
        self.assertTrue(detail["revealed"])
        self.assertEqual(len(detail["rows"]), 5)

    def test_the_threshold_comes_from_settings(self):
        from django.conf import settings

        form = self._fill(3)
        conf = {**settings.FEEDBACK, "MIN_RESPONSES_TO_REVEAL": 2}
        with override_settings(FEEDBACK=conf):
            self.assertTrue(svc.staff_detail(form)["revealed"])

    def test_no_staff_payload_can_identify_a_respondent(self):
        """
        The guarantee, checked rather than asserted in a docstring. Answers are
        stored against a student so they can read their own back, which means
        only this code stands between staff and knowing who said what.
        """
        form = self._fill(5)
        student_ids = {str(s.id) for s in self.students}
        user_emails = {s.user.email for s in self.students}
        rolls = {s.class_roll for s in self.students}

        payloads = [
            svc.staff_form_row(form),
            svc.staff_detail(form),
            svc.teacher_summary(self.teacher, self.teacher),
        ]

        def walk(node, path="root"):
            if isinstance(node, dict):
                for key, value in node.items():
                    self.assertNotIn(
                        key, ("student", "student_id", "email", "roll",
                              "class_roll", "exam_roll", "name"),
                        f"{path}.{key} could identify a respondent")
                    walk(value, f"{path}.{key}")
            elif isinstance(node, (list, tuple)):
                for i, item in enumerate(node):
                    walk(item, f"{path}[{i}]")
            elif isinstance(node, str):
                self.assertNotIn(node, student_ids, f"{path} holds a student id")
                self.assertNotIn(node, user_emails, f"{path} holds a student email")
                if node in rolls:
                    self.fail(f"{path} holds a class roll")

        for payload in payloads:
            walk(payload)
