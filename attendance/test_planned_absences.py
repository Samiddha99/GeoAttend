"""
Absences declared in advance.

The feature only earns its keep if a planned absence is applied automatically
when the class is later missed — that is the assertion to protect.
"""
import datetime as dt

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from academics.models import Batch, Department, Enrollment, StudentProfile, Subject, TeacherAssignment
from accounts.models import Institute, User
from attendance.models import AbsenceReason, AttendanceSession, PlannedAbsence
from attendance.services import (
    AttendanceError,
    can_review_planned,
    cancel_planned_absence,
    review_planned_decision,
    submit_planned_absence,
)


class PlannedAbsenceTests(TestCase):
    def setUp(self):
        self.institute = Institute.objects.create(name="I", code="I", email="i@i.edu")
        self.cse = Department.objects.create(institute=self.institute, name="CSE", code="CSE")
        self.dsa = Subject.objects.create(department=self.cse, code="DSA", name="DS")
        self.ai = Subject.objects.create(department=self.cse, code="AI", name="AI")
        self.batch = Batch.objects.create(department=self.cse, label="2022-26",
                                          start_year=2022, end_year=2026)

        def user(email, role, dept=None):
            return User.objects.create_user(
                email=email, password="Str0ngPass!23", role=role, institute=self.institute,
                department=dept, registration_completed=True, full_name=email)

        self.dsa_teacher = user("dsa@i.edu", "TEACHER", self.cse)
        self.ai_teacher = user("ai@i.edu", "TEACHER", self.cse)
        self.hod = user("hod@i.edu", "HOD", self.cse)
        TeacherAssignment.objects.create(teacher=self.dsa_teacher, subject=self.dsa,
                                         batch=self.batch)
        TeacherAssignment.objects.create(teacher=self.ai_teacher, subject=self.ai,
                                         batch=self.batch)

        self.student_user = user("s@i.edu", "STUDENT", self.cse)
        self.student = StudentProfile.objects.create(
            user=self.student_user, department=self.cse, batch=self.batch, class_roll="01")
        for subject in (self.dsa, self.ai):
            Enrollment.objects.create(student=self.student, subject=subject)

        self.tomorrow = timezone.localdate() + dt.timedelta(days=1)
        self.next_week = timezone.localdate() + dt.timedelta(days=7)

    def _plan(self, start=None, end=None, text="Wedding", subject_ids=None):
        return submit_planned_absence(
            student=self.student, from_date=start or self.tomorrow,
            to_date=end or self.tomorrow, text=text, subject_ids=subject_ids)

    def _session(self, subject, on):
        return AttendanceSession.objects.create(
            teacher=self.dsa_teacher if subject == self.dsa else self.ai_teacher,
            subject=subject, batch=self.batch, session_date=on,
            latitude=22.5, longitude=88.3,
            expires_at=timezone.now() + dt.timedelta(minutes=5), expected_count=1)

    # -------------------------------------------------------------- filing
    def test_one_decision_per_enrolled_subject(self):
        planned = self._plan()
        self.assertEqual(
            {d.subject.code for d in planned.decisions.all()}, {"DSA", "AI"})
        self.assertTrue(planned.all_subjects)

    def test_narrowing_to_chosen_subjects(self):
        planned = self._plan(subject_ids=[self.dsa.pk])
        self.assertEqual([d.subject.code for d in planned.decisions.all()], ["DSA"])
        self.assertFalse(planned.all_subjects)

    def test_a_date_range_spans_days(self):
        planned = self._plan(self.tomorrow, self.next_week)
        self.assertEqual(planned.days, 7)

    def test_today_and_the_past_are_refused(self):
        """This form is advance notice; a class already held has its own route."""
        for when in (timezone.localdate(), timezone.localdate() - dt.timedelta(days=1)):
            with self.subTest(when=when):
                with self.assertRaises(AttendanceError) as ctx:
                    self._plan(when, when)
                self.assertEqual(ctx.exception.code, "NOT_FUTURE")

    def test_end_before_start_is_refused(self):
        with self.assertRaises(AttendanceError) as ctx:
            self._plan(self.next_week, self.tomorrow)
        self.assertEqual(ctx.exception.code, "BAD_RANGE")

    def test_overlapping_requests_are_refused(self):
        self._plan(self.tomorrow, self.next_week)
        with self.assertRaises(AttendanceError) as ctx:
            self._plan(self.next_week, self.next_week + dt.timedelta(days=2))
        self.assertEqual(ctx.exception.code, "OVERLAP")

    def test_a_cancelled_request_does_not_block_a_new_one(self):
        planned = self._plan(self.tomorrow, self.next_week)
        cancel_planned_absence(planned=planned, actor=self.student_user)
        self._plan(self.tomorrow, self.next_week)      # no OVERLAP

    def test_an_empty_reason_is_refused(self):
        with self.assertRaises(AttendanceError) as ctx:
            self._plan(text="   ")
        self.assertEqual(ctx.exception.code, "EMPTY_REASON")

    # -------------------------------------------------------------- review
    def test_each_teacher_reviews_only_their_own_subject(self):
        planned = self._plan()
        dsa_decision = planned.decisions.get(subject=self.dsa)
        ai_decision = planned.decisions.get(subject=self.ai)

        self.assertTrue(can_review_planned(self.dsa_teacher, dsa_decision))
        self.assertFalse(can_review_planned(self.dsa_teacher, ai_decision))
        self.assertTrue(can_review_planned(self.ai_teacher, ai_decision))
        self.assertTrue(can_review_planned(self.hod, dsa_decision))

    def test_a_teacher_cannot_decide_another_subject(self):
        planned = self._plan()
        ai_decision = planned.decisions.get(subject=self.ai)
        with self.assertRaises(AttendanceError) as ctx:
            review_planned_decision(decision=ai_decision, actor=self.dsa_teacher, approve=True)
        self.assertEqual(ctx.exception.status, 403)

    def test_the_overall_status_is_pessimistic(self):
        planned = self._plan()
        self.assertEqual(planned.overall_status, AbsenceReason.Status.PENDING)

        review_planned_decision(decision=planned.decisions.get(subject=self.dsa),
                                actor=self.dsa_teacher, approve=True)
        planned.refresh_from_db()
        # One approved, one still pending — not "approved" yet.
        self.assertEqual(planned.overall_status, AbsenceReason.Status.PENDING)

        review_planned_decision(decision=planned.decisions.get(subject=self.ai),
                                actor=self.ai_teacher, approve=False)
        planned.refresh_from_db()
        # A single rejection is worth surfacing over a majority of approvals.
        self.assertEqual(planned.overall_status, AbsenceReason.Status.REJECTED)

    def test_a_decision_is_final(self):
        planned = self._plan()
        decision = planned.decisions.get(subject=self.dsa)
        review_planned_decision(decision=decision, actor=self.dsa_teacher, approve=True)
        with self.assertRaises(AttendanceError) as ctx:
            review_planned_decision(decision=decision, actor=self.hod, approve=False)
        self.assertEqual(ctx.exception.code, "ALREADY_REVIEWED")

    # --------------------------------------------------------- auto-attach
    def _filters(self):
        # The default report range ends today, so a class dated tomorrow would
        # fall outside it. Widen the window rather than back-dating the plan —
        # by the time such a class is actually held it is in the past anyway.
        from dashboard.filters import ReportFilters

        end = (self.next_week + dt.timedelta(days=30)).isoformat()
        return ReportFilters.from_request(type("R", (), {"GET": {"end": end}})())

    def _recent(self, viewer):
        from dashboard.services import student_detail

        return student_detail(viewer, self._filters(), self.student)["recent"]

    def test_a_missed_class_inside_the_period_gets_the_reason_automatically(self):
        planned = self._plan(self.tomorrow, self.tomorrow, text="Wedding")
        review_planned_decision(decision=planned.decisions.get(subject=self.dsa),
                                actor=self.dsa_teacher, approve=True, remark="Fine")
        self._session(self.dsa, self.tomorrow)          # held, student absent

        row = next(r for r in self._recent(self.hod) if r["subject"] == "DSA")
        self.assertEqual(row["status"], "ABSENT")
        self.assertEqual(row["reason"], "Wedding")
        self.assertEqual(row["reason_status"], "APPROVED")
        self.assertEqual(row["reason_remark"], "Fine")
        self.assertTrue(row["reason_planned"])
        # And the student is not asked to explain it again.
        self.assertFalse(row["can_explain"])

    def test_a_class_outside_the_period_is_untouched(self):
        self._plan(self.tomorrow, self.tomorrow)
        self._session(self.dsa, timezone.localdate())    # today, not covered

        row = next(r for r in self._recent(self.hod) if r["subject"] == "DSA")
        self.assertEqual(row["reason"], "")
        self.assertTrue(row["can_explain"])

    def test_a_subject_outside_the_request_is_untouched(self):
        self._plan(self.tomorrow, self.tomorrow, subject_ids=[self.dsa.pk])
        self._session(self.ai, self.tomorrow)

        row = next(r for r in self._recent(self.hod) if r["subject"] == "AI")
        self.assertEqual(row["reason"], "")

    def test_a_pending_plan_still_shows_on_the_class(self):
        """The student should see it is covered, even before anyone decides."""
        self._plan(self.tomorrow, self.tomorrow, text="Wedding")
        self._session(self.dsa, self.tomorrow)
        row = next(r for r in self._recent(self.hod) if r["subject"] == "DSA")
        self.assertEqual(row["reason_status"], "PENDING")
        self.assertFalse(row["can_explain"])

    def test_cancelling_releases_the_cover(self):
        planned = self._plan(self.tomorrow, self.tomorrow)
        cancel_planned_absence(planned=planned, actor=self.student_user)
        self._session(self.dsa, self.tomorrow)
        row = next(r for r in self._recent(self.hod) if r["subject"] == "DSA")
        self.assertEqual(row["reason"], "")
        self.assertTrue(row["can_explain"])

    def test_a_per_class_reason_wins_over_a_planned_one(self):
        from attendance.services import submit_absence_reason

        # Filed for tomorrow, but the student also explains the class itself
        # once it has happened — the specific explanation should be shown.
        session = self._session(self.dsa, timezone.localdate())
        submit_absence_reason(student=self.student, session=session, text="Specific")
        planned = PlannedAbsence.objects.create(
            student=self.student, from_date=timezone.localdate(),
            to_date=timezone.localdate(), reason="General")
        planned.decisions.create(subject=self.dsa)

        row = next(r for r in self._recent(self.hod) if r["subject"] == "DSA")
        self.assertEqual(row["reason"], "Specific")
        self.assertFalse(row["reason_planned"])

    def test_figures_are_unaffected(self):
        from dashboard.services import student_detail

        planned = self._plan(self.tomorrow, self.tomorrow)
        review_planned_decision(decision=planned.decisions.get(subject=self.dsa),
                                actor=self.dsa_teacher, approve=True)
        self._session(self.dsa, self.tomorrow)

        overall = student_detail(self.hod, self._filters(), self.student)["overall"]
        self.assertEqual(overall["attended"], 0)
        self.assertEqual(overall["held"], 1)
        self.assertEqual(overall["percentage"], 0)

    # -------------------------------------------------------------- access
    def test_a_teacher_only_sees_requests_touching_their_subjects(self):
        self._plan(subject_ids=[self.dsa.pk])
        client = self.client_class()

        client.force_login(self.ai_teacher)
        self.assertEqual(client.get(reverse("attendance:api_planned_absences"))
                         .json()["data"]["rows"], [])

        client.force_login(self.dsa_teacher)
        rows = client.get(reverse("attendance:api_planned_absences")).json()["data"]["rows"]
        self.assertEqual(len(rows), 1)

    def test_a_reviewer_only_sees_their_own_decisions_on_a_shared_request(self):
        """A DSA teacher must not be shown the AI teacher's verdict as theirs."""
        self._plan()
        client = self.client_class()
        client.force_login(self.dsa_teacher)
        row = client.get(reverse("attendance:api_planned_absences")).json()["data"]["rows"][0]
        self.assertEqual([d["subject"] for d in row["decisions"]], ["DSA"])

    def test_a_student_sees_their_own_with_every_decision(self):
        self._plan()
        client = self.client_class()
        client.force_login(self.student_user)
        row = client.get(reverse("attendance:api_planned_absences")).json()["data"]["rows"][0]
        self.assertEqual({d["subject"] for d in row["decisions"]}, {"DSA", "AI"})

    def test_a_student_cannot_cancel_somebody_elses(self):
        planned = self._plan()
        other = User.objects.create_user(
            email="s2@i.edu", password="Str0ngPass!23", role="STUDENT",
            institute=self.institute, registration_completed=True)
        with self.assertRaises(AttendanceError) as ctx:
            cancel_planned_absence(planned=planned, actor=other)
        self.assertEqual(ctx.exception.status, 403)


class PendingBadgeTests(PlannedAbsenceTests):
    """
    The sidebar count must match what each reviewer actually has to act on —
    a badge that overstates is worse than none, because it never clears.
    """

    def _count(self, user):
        from core.context_processors import pending_review_count

        return pending_review_count(user)

    def test_a_teacher_counts_only_their_own_subject(self):
        self._plan()                       # covers DSA + AI, both pending
        self.assertEqual(self._count(self.dsa_teacher), 1)
        self.assertEqual(self._count(self.ai_teacher), 1)

    def test_the_hod_counts_the_whole_department(self):
        self._plan()
        self.assertEqual(self._count(self.hod), 2)

    def test_deciding_lowers_the_count(self):
        planned = self._plan()
        review_planned_decision(decision=planned.decisions.get(subject=self.dsa),
                                actor=self.dsa_teacher, approve=True)
        self.assertEqual(self._count(self.dsa_teacher), 0)
        self.assertEqual(self._count(self.ai_teacher), 1)     # untouched
        self.assertEqual(self._count(self.hod), 1)

    def test_cancelling_lowers_the_count(self):
        planned = self._plan()
        cancel_planned_absence(planned=planned, actor=self.student_user)
        self.assertEqual(self._count(self.hod), 0)

    def test_per_class_reasons_are_counted_too(self):
        from attendance.services import submit_absence_reason

        session = self._session(self.dsa, timezone.localdate())
        submit_absence_reason(student=self.student, session=session, text="Fever")
        # The DSA teacher ran that session, so it is theirs to review.
        self.assertEqual(self._count(self.dsa_teacher), 1)
        # The AI teacher has nothing.
        self.assertEqual(self._count(self.ai_teacher), 0)

    def test_both_kinds_add_up(self):
        from attendance.services import submit_absence_reason

        submit_absence_reason(student=self.student,
                              session=self._session(self.dsa, timezone.localdate()),
                              text="Fever")
        self._plan()                       # + DSA decision
        self.assertEqual(self._count(self.dsa_teacher), 2)

    def test_students_and_anonymous_get_nothing(self):
        from django.contrib.auth.models import AnonymousUser

        self._plan()
        self.assertEqual(self._count(self.student_user), 0)
        self.assertEqual(self._count(AnonymousUser()), 0)

    def test_the_badge_renders_in_the_sidebar(self):
        self._plan()
        client = self.client_class()
        client.force_login(self.dsa_teacher)
        html = client.get(reverse("attendance:absence_reasons")).content.decode()
        self.assertIn('id="ga-review-badge"', html)
        self.assertIn("awaiting your review", html)
        # Exactly one badge — a teacher matches two nav branches, and a
        # duplicate id would break the live update.
        self.assertEqual(html.count('id="ga-review-badge"'), 1)

    def test_the_badge_is_hidden_when_there_is_nothing_to_do(self):
        client = self.client_class()
        client.force_login(self.ai_teacher)
        html = client.get(reverse("attendance:absence_reasons")).content.decode()
        self.assertIn('id="ga-review-badge"', html)
        self.assertIn("d-none", html.split('id="ga-review-badge"')[0][-80:])

    def test_a_student_page_has_no_badge(self):
        client = self.client_class()
        client.force_login(self.student_user)
        html = client.get(reverse("attendance:my_absence_reasons")).content.decode()
        self.assertNotIn('id="ga-review-badge"', html)
