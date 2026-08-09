"""
Manual (teacher-entered) attendance: the window it may happen in, and the
visibility of it afterwards.

Two separate concerns that arrived together and are easy to conflate:

* **The window.** A teacher may mark someone present by hand for 30 minutes
  after the link is created — while they are still in the room and can see who
  is in front of them. Afterwards it would be an unverifiable claim about the
  past.
* **The count.** Every attendance figure in this app already counts a manual
  mark as present, which is right but hides something worth seeing. A class at
  95% where a third of the marks were typed in is not the same class as one at
  95% where the students marked themselves.
"""
import datetime as dt

from django.test import TestCase, override_settings
from django.utils import timezone

from academics.models import Batch, Department, Enrollment, StudentProfile, Subject
from accounts.models import Institute, User
from attendance.models import AttendanceRecord, AttendanceSession
from attendance.services import (
    AttendanceError,
    manual_mark,
    manual_mark_open,
    manual_mark_seconds_left,
)


class ManualMarkFixture(TestCase):
    def setUp(self):
        self.institute = Institute.objects.create(name="I", code="I", email="i@i.edu")
        self.dept = Department.objects.create(institute=self.institute,
                                              name="CSE", code="CSE")
        self.batch = Batch.objects.create(department=self.dept, label="2022-26",
                                          start_year=2022, end_year=2026)
        self.subject = Subject.objects.create(department=self.dept, code="DSA",
                                              name="Data Structures")
        self.teacher = User.objects.create_user(
            email="t@i.edu", password="Str0ngPass!23", role="TEACHER",
            institute=self.institute, department=self.dept,
            registration_completed=True)
        from academics.models import TeacherAssignment

        TeacherAssignment.objects.create(teacher=self.teacher, subject=self.subject,
                                         batch=self.batch, is_active=True)
        self.student = self._student("s1@i.edu", "Asha Roy", "01")
        self.session = self._session()

    def _student(self, email, name, roll):
        user = User.objects.create_user(
            email=email, password="Str0ngPass!23", role="STUDENT",
            institute=self.institute, department=self.dept, full_name=name,
            registration_completed=True, face_enrolled=True)
        profile = StudentProfile.objects.create(
            user=user, department=self.dept, batch=self.batch, class_roll=roll)
        Enrollment.objects.create(student=profile, subject=self.subject,
                                  is_active=True)
        return profile

    def _session(self):
        return AttendanceSession.objects.create(
            teacher=self.teacher, subject=self.subject, batch=self.batch,
            latitude=0, longitude=0, radius_m=50, expected_count=1,
            expires_at=timezone.now() + dt.timedelta(minutes=5))

    def _age_session(self, minutes):
        """Push the link's creation time into the past."""
        created = timezone.now() - dt.timedelta(minutes=minutes)
        AttendanceSession.objects.filter(pk=self.session.pk).update(created_at=created)
        self.session.refresh_from_db()


@override_settings(ATTENDANCE={"MANUAL_MARK_MINUTES": 30})
class ManualWindowTests(ManualMarkFixture):
    def test_inside_the_window_a_teacher_may_mark_someone_present(self):
        self._age_session(29)
        record, _ = manual_mark(session=self.session, student=self.student,
                                teacher=self.teacher)
        self.assertEqual(record.status, AttendanceRecord.Status.MANUAL)

    def test_the_boundary_minute_still_counts_as_inside(self):
        """
        Exactly 30 minutes is inside, not outside — an inclusive edge, so a
        teacher acting right on the deadline is not refused by a fraction of a
        second.

        The instant is passed in rather than aged into the past: between
        setting `created_at` and reading it back, real microseconds elapse, and
        a wall-clock test of an inclusive boundary would fail for reasons that
        say nothing about the rule.
        """
        deadline = self.session.created_at + dt.timedelta(minutes=30)
        self.assertTrue(manual_mark_open(self.session, now=deadline))
        self.assertFalse(manual_mark_open(
            self.session, now=deadline + dt.timedelta(seconds=1)))

    def test_outside_the_window_it_is_refused(self):
        self._age_session(31)
        with self.assertRaises(AttendanceError) as caught:
            manual_mark(session=self.session, student=self.student,
                        teacher=self.teacher)
        self.assertEqual(caught.exception.code, "MANUAL_WINDOW_CLOSED")
        self.assertFalse(
            AttendanceRecord.objects.filter(session=self.session).exists())

    def test_the_window_runs_from_creation_not_from_expiry(self):
        """
        The link lives 5 minutes; hand-marking lives 30. They are different
        clocks on purpose — a flat battery or a failed face match still gets
        sorted out during the lesson.
        """
        self._age_session(20)
        self.session.expires_at = timezone.now() - dt.timedelta(minutes=15)
        self.session.save(update_fields=["expires_at"])
        self.assertTrue(manual_mark_open(self.session))
        record, _ = manual_mark(session=self.session, student=self.student,
                                teacher=self.teacher)
        self.assertEqual(record.status, AttendanceRecord.Status.MANUAL)

    def test_removing_a_mark_stays_possible_after_the_window(self):
        """
        The window exists to stop attendance being conjured up later. Undoing a
        mistake cannot do that, so it is not bound by the same clock — and a
        teacher who mis-clicks at minute 29 would otherwise be stuck with it.
        """
        manual_mark(session=self.session, student=self.student,
                    teacher=self.teacher)
        self._age_session(120)
        manual_mark(session=self.session, student=self.student,
                    teacher=self.teacher, present=False)
        self.assertFalse(
            AttendanceRecord.objects.filter(session=self.session).exists())

    def test_seconds_left_counts_down_and_floors_at_zero(self):
        self._age_session(10)
        self.assertAlmostEqual(manual_mark_seconds_left(self.session),
                               20 * 60, delta=5)
        self._age_session(90)
        self.assertEqual(manual_mark_seconds_left(self.session), 0)

    def test_a_student_request_cannot_route_around_the_window(self):
        """
        The face-match fallback approves through `manual_mark`, so it inherits
        the window rather than needing its own copy of the rule.
        """
        from attendance.live import decide_manual_mark
        from attendance.models import ManualMarkRequest

        request = ManualMarkRequest.objects.create(
            session=self.session, student=self.student, reason="no match")
        self._age_session(45)
        with self.assertRaises(AttendanceError) as caught:
            decide_manual_mark(request_obj=request, teacher=self.teacher,
                               approve=True)
        self.assertEqual(caught.exception.code, "MANUAL_WINDOW_CLOSED")


@override_settings(ATTENDANCE={"MANUAL_MARK_MINUTES": 0})
class ManualMarkingDisabledTests(ManualMarkFixture):
    def test_zero_minutes_switches_manual_marking_off(self):
        with self.assertRaises(AttendanceError) as caught:
            manual_mark(session=self.session, student=self.student,
                        teacher=self.teacher)
        self.assertEqual(caught.exception.code, "MANUAL_DISABLED")


@override_settings(ATTENDANCE={"MANUAL_MARK_MINUTES": 30})
class ManualCountTests(ManualMarkFixture):
    """The manual share, wherever a present figure is reported."""

    def setUp(self):
        super().setUp()
        self.self_marked = self._student("s2@i.edu", "Bela Roy", "02")
        AttendanceSession.objects.filter(pk=self.session.pk).update(expected_count=2)
        self.session.refresh_from_db()
        # One student marked themselves, one was marked by the teacher.
        AttendanceRecord.objects.create(
            session=self.session, student=self.self_marked,
            status=AttendanceRecord.Status.PRESENT)
        manual_mark(session=self.session, student=self.student,
                    teacher=self.teacher)

    def test_the_session_reports_both_totals(self):
        self.assertEqual(self.session.present_count, 2)
        self.assertEqual(self.session.manual_count, 1)

    def test_manual_is_a_subset_of_present_never_a_third_bucket(self):
        """
        MANUAL is one of the two statuses that count as present. If it were
        ever counted separately, percentages would exceed 100.
        """
        self.assertLessEqual(self.session.manual_count, self.session.present_count)
        self.assertEqual(self.session.percentage, 100.0)

    def test_the_reports_carry_the_split(self):
        from dashboard.filters import ReportFilters
        from dashboard.services import manual_counts, scoped_sessions, student_report

        today = timezone.localdate()
        f = ReportFilters(start=today - dt.timedelta(days=1),
                          end=today + dt.timedelta(days=1))
        counts = manual_counts(scoped_sessions(self.teacher, f))
        self.assertEqual(counts.get((self.student.id, self.subject.id)), 1)
        self.assertIsNone(counts.get((self.self_marked.id, self.subject.id)))

        rows = {r["student_id"]: r for r in student_report(self.teacher, f)}
        self.assertEqual(rows[self.student.id]["manual"], 1)
        self.assertEqual(rows[self.student.id]["attended"], 1)
        self.assertEqual(rows[self.self_marked.id]["manual"], 0)

    def test_every_rollup_carries_the_split(self):
        """
        Batch, department and teacher comparisons each aggregate separately, so
        each needed its own manual query. This walks all three rather than
        trusting that whoever added the third remembered.
        """
        from dashboard.filters import ReportFilters
        from dashboard.services import (
            batch_comparison,
            department_comparison,
            teacher_activity,
        )

        today = timezone.localdate()
        f = ReportFilters(start=today - dt.timedelta(days=1),
                          end=today + dt.timedelta(days=1))
        for name, rows in (("batch", batch_comparison(self.teacher, f)),
                           ("department", department_comparison(self.teacher, f)),
                           ("teacher", teacher_activity(self.teacher, f))):
            with self.subTest(rollup=name):
                self.assertTrue(rows, f"{name} rollup returned nothing")
                row = rows[0]
                self.assertEqual(row["manual"], 1)
                self.assertEqual(row["present"], 2)
                self.assertEqual(row["manual_share"], 50.0)

    def test_the_at_risk_list_carries_the_roll_numbers_and_the_split(self):
        """
        At-risk is the list staff act on — chase a student, call a guardian —
        so it needs the same identifiers and the same manual context as the
        student-wise report it is derived from.
        """
        from dashboard.filters import ReportFilters
        from dashboard.services import low_attendance

        # A second class nobody attends, so both students sit at 50% and
        # actually appear below the threshold.
        AttendanceSession.objects.create(
            teacher=self.teacher, subject=self.subject, batch=self.batch,
            latitude=0, longitude=0, radius_m=50, expected_count=2,
            expires_at=timezone.now() + dt.timedelta(minutes=5))

        today = timezone.localdate()
        f = ReportFilters(start=today - dt.timedelta(days=1),
                          end=today + dt.timedelta(days=1))
        rows = low_attendance(self.teacher, f, threshold=75)
        self.assertTrue(rows, "expected both students below 75%")
        for row in rows:
            with self.subTest(student=row["name"]):
                for key in ("roll", "exam_roll", "manual", "attended", "held"):
                    self.assertIn(key, row)

    def test_a_manual_mark_never_exceeds_the_attended_figure(self):
        from dashboard.filters import ReportFilters
        from dashboard.services import student_report

        today = timezone.localdate()
        f = ReportFilters(start=today - dt.timedelta(days=1),
                          end=today + dt.timedelta(days=1))
        for row in student_report(self.teacher, f):
            with self.subTest(student=row["name"]):
                self.assertLessEqual(row["manual"], row["attended"])
                self.assertLessEqual(row["manual_share"], 100.0)
