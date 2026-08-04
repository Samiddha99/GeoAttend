"""
A student's explanation for one missed class, and its review.

The rule that matters most is the one that is easiest to break by accident:
approving a reason must not move any attendance number.
"""
import datetime as dt

from django.core.exceptions import PermissionDenied
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from academics.models import Batch, Department, Enrollment, StudentProfile, Subject, TeacherAssignment
from accounts.models import Institute, User
from attendance import views
from attendance.models import AbsenceReason, AttendanceRecord, AttendanceSession
from attendance.services import (
    AttendanceError,
    can_review_reason,
    review_absence_reason,
    submit_absence_reason,
)


class AbsenceReasonTests(TestCase):
    def setUp(self):
        self.institute = Institute.objects.create(name="I", code="I", email="i@i.edu")
        self.cse = Department.objects.create(institute=self.institute, name="CSE", code="CSE")
        self.ece = Department.objects.create(institute=self.institute, name="ECE", code="ECE")
        self.dsa = Subject.objects.create(department=self.cse, code="DSA", name="DS")
        self.batch = Batch.objects.create(department=self.cse, label="2022-26",
                                          start_year=2022, end_year=2026)

        def user(email, role, dept=None):
            return User.objects.create_user(
                email=email, password="Str0ngPass!23", role=role, institute=self.institute,
                department=dept, registration_completed=True, full_name=email)

        self.teacher = user("t@i.edu", "TEACHER", self.cse)      # took the class
        self.other_teacher = user("t2@i.edu", "TEACHER", self.cse)
        self.hod = user("hod@i.edu", "HOD", self.cse)
        self.ece_hod = user("ehod@i.edu", "HOD", self.ece)
        self.head = user("head@i.edu", "HEAD")
        TeacherAssignment.objects.create(teacher=self.teacher, subject=self.dsa, batch=self.batch)
        TeacherAssignment.objects.create(teacher=self.other_teacher, subject=self.dsa,
                                         batch=self.batch)

        self.student_user = user("s@i.edu", "STUDENT", self.cse)
        self.student = StudentProfile.objects.create(
            user=self.student_user, department=self.cse, batch=self.batch, class_roll="01")
        Enrollment.objects.create(student=self.student, subject=self.dsa)

    def _session(self, days_ago=0):
        when = timezone.localdate() - dt.timedelta(days=days_ago)
        return AttendanceSession.objects.create(
            teacher=self.teacher, subject=self.dsa, batch=self.batch,
            session_date=when, latitude=22.5, longitude=88.3,
            expires_at=timezone.now() + dt.timedelta(minutes=5), expected_count=1)

    # ------------------------------------------------------------- window
    def test_a_reason_can_be_given_inside_the_window(self):
        reason = submit_absence_reason(
            student=self.student, session=self._session(2), text="Fever")
        self.assertEqual(reason.status, AbsenceReason.Status.PENDING)

    def test_the_last_day_of_the_window_still_counts(self):
        """Three days means three, not two — an off-by-one here is invisible."""
        reason = submit_absence_reason(
            student=self.student, session=self._session(3), text="Fever")
        self.assertEqual(reason.status, AbsenceReason.Status.PENDING)

    def test_a_day_past_the_window_is_refused(self):
        with self.assertRaises(AttendanceError) as ctx:
            submit_absence_reason(student=self.student, session=self._session(4), text="Fever")
        self.assertEqual(ctx.exception.code, "WINDOW_CLOSED")

    def test_zero_days_turns_the_feature_off(self):
        from django.conf import settings

        conf = {**settings.ATTENDANCE, "ABSENCE_REASON_DAYS": 0}
        with override_settings(ATTENDANCE=conf):
            with self.assertRaises(AttendanceError) as ctx:
                submit_absence_reason(
                    student=self.student, session=self._session(0), text="Fever")
        self.assertEqual(ctx.exception.code, "DISABLED")

    # ------------------------------------------------------------- rules
    def test_a_present_student_has_nothing_to_explain(self):
        session = self._session(1)
        AttendanceRecord.objects.create(session=session, student=self.student, status="PRESENT")
        with self.assertRaises(AttendanceError) as ctx:
            submit_absence_reason(student=self.student, session=session, text="x")
        self.assertEqual(ctx.exception.code, "NOT_ABSENT")

    def test_an_empty_reason_is_refused(self):
        with self.assertRaises(AttendanceError) as ctx:
            submit_absence_reason(student=self.student, session=self._session(1), text="   ")
        self.assertEqual(ctx.exception.code, "EMPTY_REASON")

    def test_only_one_submission_per_class(self):
        session = self._session(1)
        submit_absence_reason(student=self.student, session=session, text="Fever")
        with self.assertRaises(AttendanceError) as ctx:
            submit_absence_reason(student=self.student, session=session, text="Again")
        self.assertEqual(ctx.exception.code, "ALREADY_SUBMITTED")

    def test_no_resubmission_after_a_rejection(self):
        session = self._session(1)
        reason = submit_absence_reason(student=self.student, session=session, text="Fever")
        review_absence_reason(reason=reason, actor=self.teacher, approve=False, remark="Vague")
        with self.assertRaises(AttendanceError) as ctx:
            submit_absence_reason(student=self.student, session=session, text="Better wording")
        self.assertEqual(ctx.exception.code, "ALREADY_SUBMITTED")

    def test_a_student_cannot_explain_a_subject_they_do_not_take(self):
        other = Subject.objects.create(department=self.cse, code="AI", name="AI")
        session = AttendanceSession.objects.create(
            teacher=self.teacher, subject=other, batch=self.batch,
            session_date=timezone.localdate(), latitude=22.5, longitude=88.3,
            expires_at=timezone.now() + dt.timedelta(minutes=5), expected_count=1)
        with self.assertRaises(AttendanceError) as ctx:
            submit_absence_reason(student=self.student, session=session, text="x")
        self.assertEqual(ctx.exception.code, "NOT_ENROLLED")

    # ------------------------------------------------------------ review
    def test_who_may_review(self):
        reason = submit_absence_reason(
            student=self.student, session=self._session(1), text="Fever")
        self.assertTrue(can_review_reason(self.teacher, reason))       # took the class
        self.assertTrue(can_review_reason(self.hod, reason))
        self.assertTrue(can_review_reason(self.head, reason))
        # Teaches the same subject but did not take this class.
        self.assertFalse(can_review_reason(self.other_teacher, reason))
        self.assertFalse(can_review_reason(self.ece_hod, reason))
        self.assertFalse(can_review_reason(self.student_user, reason))

    def test_a_teacher_who_did_not_take_the_class_is_refused(self):
        reason = submit_absence_reason(
            student=self.student, session=self._session(1), text="Fever")
        with self.assertRaises(AttendanceError) as ctx:
            review_absence_reason(reason=reason, actor=self.other_teacher, approve=True)
        self.assertEqual(ctx.exception.status, 403)
        reason.refresh_from_db()
        self.assertTrue(reason.is_pending)

    def test_a_decision_is_final(self):
        reason = submit_absence_reason(
            student=self.student, session=self._session(1), text="Fever")
        review_absence_reason(reason=reason, actor=self.teacher, approve=True, remark="OK")
        with self.assertRaises(AttendanceError) as ctx:
            review_absence_reason(reason=reason, actor=self.hod, approve=False)
        self.assertEqual(ctx.exception.code, "ALREADY_REVIEWED")
        reason.refresh_from_db()
        self.assertEqual(reason.status, AbsenceReason.Status.APPROVED)
        self.assertEqual(reason.review_remark, "OK")
        self.assertEqual(reason.reviewed_by, self.teacher)

    # --------------------------------------------------- no effect on data
    def test_approving_does_not_change_the_attendance_figures(self):
        """
        The whole design rests on this. If approval ever starts moving numbers,
        it must be a deliberate change with reports and alerts updated too.
        """
        from dashboard.filters import ReportFilters
        from dashboard.services import student_detail

        session = self._session(1)
        before = student_detail(self.hod, ReportFilters.from_request(
            type("R", (), {"GET": {}})()), self.student)["overall"]

        reason = submit_absence_reason(student=self.student, session=session, text="Fever")
        review_absence_reason(reason=reason, actor=self.teacher, approve=True)

        after = student_detail(self.hod, ReportFilters.from_request(
            type("R", (), {"GET": {}})()), self.student)["overall"]
        self.assertEqual(before, after)
        self.assertEqual(after["attended"], 0)
        self.assertEqual(after["held"], 1)

    def test_the_reason_reaches_the_class_history_row(self):
        from dashboard.filters import ReportFilters
        from dashboard.services import student_detail

        session = self._session(1)
        reason = submit_absence_reason(student=self.student, session=session, text="Fever")
        review_absence_reason(reason=reason, actor=self.teacher, approve=True, remark="Get well")

        detail = student_detail(self.hod, ReportFilters.from_request(
            type("R", (), {"GET": {}})()), self.student)
        row = next(r for r in detail["recent"] if r["subject"] == "DSA")
        self.assertEqual(row["reason"], "Fever")
        self.assertEqual(row["reason_status"], "APPROVED")
        self.assertEqual(row["reason_remark"], "Get well")
        self.assertFalse(row["can_explain"])       # already explained

    def test_an_unexplained_recent_absence_is_offered_the_button(self):
        from dashboard.filters import ReportFilters
        from dashboard.services import student_detail

        self._session(1)
        detail = student_detail(self.hod, ReportFilters.from_request(
            type("R", (), {"GET": {}})()), self.student)
        row = next(r for r in detail["recent"] if r["subject"] == "DSA")
        self.assertTrue(row["can_explain"])

    # ------------------------------------------------------------- access
    def test_the_review_list_is_scoped_to_the_sessions_you_ran(self):
        submit_absence_reason(student=self.student, session=self._session(1), text="Fever")
        client = self.client_class()

        client.force_login(self.other_teacher)
        self.assertEqual(
            client.get(reverse("attendance:api_absence_reasons")).json()["data"]["rows"], [])

        client.force_login(self.teacher)
        self.assertEqual(
            len(client.get(reverse("attendance:api_absence_reasons")).json()["data"]["rows"]), 1)

    def test_a_student_cannot_open_the_review_screen(self):
        client = self.client_class()
        client.force_login(self.student_user)
        self.assertEqual(client.get(reverse("attendance:absence_reasons")).status_code, 403)

    def test_a_teacher_cannot_submit_a_reason(self):
        from django.test import RequestFactory

        from attendance import views

        request = RequestFactory().post("/x/", {"reason": "x"})
        request.user = self.teacher
        with self.assertRaises(PermissionDenied):
            views.api_absence_reason_submit(request, pk=self._session(1).pk)


class StudentReasonAccessTests(AbsenceReasonTests):
    """The student-facing half: submitting over HTTP, and their own list."""

    def _login_student(self):
        client = self.client_class()
        client.force_login(self.student_user)
        return client

    # A literal id: these assert *routing*, which needs no row in the database
    # and no particular primary-key format.
    OID = "6a6cf0c46252538ba9808843"

    def test_the_submit_route_is_not_shadowed_by_the_action_catch_all(self):
        """
        Regression: the submit URL sat *after* api/sessions/<pk>/<action>/, so
        the catch-all matched action="reason" and routed students into a
        teacher-only view — they got "restricted to: TEACHER, HOD, HEAD".
        """
        from django.urls import resolve

        match = resolve(f"/attendance/api/sessions/{self.OID}/reason/")
        self.assertEqual(match.func.__name__, "api_absence_reason_submit")

    def test_the_other_session_actions_still_route_correctly(self):
        """Moving the route above the catch-all must not shadow the catch-all."""
        from django.urls import resolve

        for action in ("close", "extend", "resend"):
            with self.subTest(action=action):
                match = resolve(f"/attendance/api/sessions/{self.OID}/{action}/")
                self.assertEqual(match.func.__name__, "api_session_action")

    def test_the_submit_view_accepts_a_student(self):
        """The other half of the bug: the view a student is now routed to."""
        from django.test import RequestFactory

        from attendance import views

        session = self._session(1)
        request = RequestFactory().post("/x/", {"reason": "Fever"})
        request.user = self.student_user
        res = views.api_absence_reason_submit(request, pk=session.pk)
        self.assertEqual(res.status_code, 200)
        self.assertTrue(
            AbsenceReason.objects.filter(session=session, student=self.student).exists())

    def test_a_student_sees_their_own_reasons_and_nobody_elses(self):
        from academics.models import Enrollment, StudentProfile

        submit_absence_reason(student=self.student, session=self._session(1), text="Mine")

        other_user = User.objects.create_user(
            email="s2@i.edu", password="Str0ngPass!23", role="STUDENT",
            institute=self.institute, department=self.cse,
            registration_completed=True, full_name="Other")
        other = StudentProfile.objects.create(
            user=other_user, department=self.cse, batch=self.batch, class_roll="02")
        Enrollment.objects.create(student=other, subject=self.dsa)
        submit_absence_reason(student=other, session=self._session(2), text="Theirs")

        rows = self._login_student().get(
            reverse("attendance:api_absence_reasons")).json()["data"]["rows"]
        self.assertEqual([r["reason"] for r in rows], ["Mine"])

    def test_the_student_list_omits_reviewer_only_columns(self):
        submit_absence_reason(student=self.student, session=self._session(1), text="Fever")
        row = self._login_student().get(
            reverse("attendance:api_absence_reasons")).json()["data"]["rows"][0]
        self.assertNotIn("student", row)      # reviewer-only
        self.assertNotIn("email", row)
        self.assertIn("status_label", row)

    def test_the_student_page_loads(self):
        self.assertEqual(
            self._login_student().get(reverse("attendance:my_absence_reasons")).status_code, 200)

    def test_staff_cannot_open_the_student_page(self):
        client = self.client_class()
        client.force_login(self.teacher)
        self.assertEqual(
            client.get(reverse("attendance:my_absence_reasons")).status_code, 403)


class GroupReasonRowsTests(SimpleTestCase):
    """
    The grouping itself, with no database in the way.

    One student's morning is one row; the subjects are what sits inside it.
    """

    def _row(self, *, student="Imran Banerjee", sid="s1", date="2026-08-01",
             subject="DSA", status="PENDING", submitted="2026-08-01T10:00:00"):
        return {
            "id": f"{sid}-{subject}-{date}", "student": student, "student_id": sid,
            "class_roll": "07", "email": "imran@i.edu", "department": "CSE",
            "batch": "2022-26", "date": "01 Aug 2026", "date_iso": date,
            "subject": subject, "subject_name": subject, "reason": "Fever",
            "status": status, "status_label": status.title(),
            "submitted_at": submitted, "submitted_iso": submitted,
        }

    def test_one_students_day_collapses_into_a_single_row(self):
        rows = views._group_reason_rows([
            self._row(subject="DSA"), self._row(subject="DBMS")])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["subjects"], "DBMS, DSA")
        self.assertEqual([i["subject"] for i in rows[0]["items"]], ["DBMS", "DSA"])

    def test_each_class_keeps_its_own_verdict(self):
        """Grouping is presentation only — the decisions stay separate."""
        rows = views._group_reason_rows([
            self._row(subject="DSA"), self._row(subject="DBMS", status="APPROVED")])
        self.assertEqual(
            {i["subject"]: i["status"] for i in rows[0]["items"]},
            {"DSA": "PENDING", "DBMS": "APPROVED"})
        self.assertEqual(rows[0]["pending"], 1)

    def test_different_days_and_different_students_stay_apart(self):
        rows = views._group_reason_rows([
            self._row(),
            self._row(date="2026-07-30"),
            self._row(student="Anita Roy", sid="s2"),
        ])
        self.assertEqual(len(rows), 3)

    def test_the_row_carries_the_latest_submission_time(self):
        rows = views._group_reason_rows([
            self._row(subject="DSA", submitted="2026-08-01T10:00:00"),
            self._row(subject="DBMS", submitted="2026-08-02T09:00:00")])
        self.assertEqual(rows[0]["submitted_at"], "2026-08-02T09:00:00")

    def test_newest_day_first_then_student_name(self):
        rows = views._group_reason_rows([
            self._row(student="Anita Roy", sid="s2", date="2026-08-01"),
            self._row(date="2026-07-30"),
            self._row(date="2026-08-01"),
        ])
        self.assertEqual(
            [(r["date_iso"], r["student"]) for r in rows],
            [("2026-08-01", "Anita Roy"),
             ("2026-08-01", "Imran Banerjee"),
             ("2026-07-30", "Imran Banerjee")])


class GroupedReviewListTests(AbsenceReasonTests):
    """The grouped shape as the review screen actually receives it."""

    def setUp(self):
        super().setUp()
        self.dbms = Subject.objects.create(department=self.cse, code="DBMS", name="Databases")
        TeacherAssignment.objects.create(teacher=self.teacher, subject=self.dbms,
                                         batch=self.batch)
        Enrollment.objects.create(student=self.student, subject=self.dbms)

    def _session_for(self, subject, days_ago=0):
        return AttendanceSession.objects.create(
            teacher=self.teacher, subject=subject, batch=self.batch,
            session_date=timezone.localdate() - dt.timedelta(days=days_ago),
            latitude=22.5, longitude=88.3,
            expires_at=timezone.now() + dt.timedelta(minutes=5), expected_count=1)

    def _rows(self):
        client = self.client_class()
        client.force_login(self.teacher)
        return client.get(reverse("attendance:api_absence_reasons")).json()["data"]

    def test_two_subjects_on_one_day_are_one_row(self):
        submit_absence_reason(
            student=self.student, session=self._session_for(self.dsa, 1), text="Fever")
        submit_absence_reason(
            student=self.student, session=self._session_for(self.dbms, 1), text="Fever")

        data = self._rows()
        self.assertEqual(len(data["rows"]), 1)
        self.assertEqual(data["rows"][0]["subjects"], "DBMS, DSA")
        # Counted per class, not per row: two decisions are still waiting.
        self.assertEqual(data["pending"], 2)

    def test_the_students_own_list_is_still_flat(self):
        """Grouping is for the reviewer; the student's own page is unchanged."""
        submit_absence_reason(
            student=self.student, session=self._session_for(self.dsa, 1), text="Fever")
        submit_absence_reason(
            student=self.student, session=self._session_for(self.dbms, 1), text="Fever")

        client = self.client_class()
        client.force_login(self.student_user)
        rows = client.get(reverse("attendance:api_absence_reasons")).json()["data"]["rows"]
        self.assertEqual(len(rows), 2)
        self.assertNotIn("items", rows[0])
