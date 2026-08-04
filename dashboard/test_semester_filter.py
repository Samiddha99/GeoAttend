"""The semester filter across the dashboard and reports."""
import datetime as dt

from django.test import RequestFactory, TestCase
from django.utils import timezone

from academics.models import Batch, Department, Enrollment, StudentProfile, Subject, TeacherAssignment
from accounts.models import Institute, User
from attendance.models import AttendanceRecord, AttendanceSession
from dashboard import services as svc
from dashboard.filters import ReportFilters


class SemesterFilterTests(TestCase):
    def setUp(self):
        self.institute = Institute.objects.create(name="I", code="I", email="i@i.edu")
        self.cse = Department.objects.create(institute=self.institute, name="CSE", code="CSE")
        self.batch = Batch.objects.create(department=self.cse, label="2022-26",
                                          start_year=2022, end_year=2026)
        # Two subjects in different semesters.
        self.sem3 = Subject.objects.create(department=self.cse, code="DSA", name="DS", semester=3)
        self.sem5 = Subject.objects.create(department=self.cse, code="CN", name="Networks", semester=5)

        self.hod = User.objects.create_user(
            email="hod@i.edu", password="Str0ngPass!23", role="HOD",
            institute=self.institute, department=self.cse, registration_completed=True)
        self.teacher = User.objects.create_user(
            email="t@i.edu", password="Str0ngPass!23", role="TEACHER",
            institute=self.institute, department=self.cse, registration_completed=True)
        for subject in (self.sem3, self.sem5):
            TeacherAssignment.objects.create(teacher=self.teacher, subject=subject,
                                             batch=self.batch)

        def student(email, subjects):
            user = User.objects.create_user(
                email=email, password="Str0ngPass!23", role="STUDENT",
                institute=self.institute, department=self.cse,
                registration_completed=True, full_name=email)
            profile = StudentProfile.objects.create(
                user=user, department=self.cse, batch=self.batch, class_roll="1")
            for s in subjects:
                Enrollment.objects.create(student=profile, subject=s)
            return profile

        self.third = student("third@i.edu", [self.sem3])
        self.fifth = student("fifth@i.edu", [self.sem5])
        self.both = student("both@i.edu", [self.sem3, self.sem5])

        today = timezone.localdate()
        for subject in (self.sem3, self.sem5):
            session = AttendanceSession.objects.create(
                teacher=self.teacher, subject=subject, batch=self.batch,
                session_date=today, latitude=22.5, longitude=88.3,
                expires_at=timezone.now() + dt.timedelta(minutes=5), expected_count=2)
            AttendanceRecord.objects.create(session=session, student=self.both,
                                            status="PRESENT")

    def _filters(self, **params):
        request = RequestFactory().get("/", params)
        return ReportFilters.from_request(request)

    # --------------------------------------------------------------- parsing
    def test_the_semester_is_parsed_as_a_number(self):
        self.assertEqual(self._filters(semester="3").semester, 3)
        self.assertIsNone(self._filters(semester="").semester)
        self.assertIsNone(self._filters(semester="all").semester)
        self.assertIsNone(self._filters(semester="third").semester)

    # -------------------------------------------------------------- sessions
    def test_sessions_are_filtered(self):
        self.assertEqual(svc.scoped_sessions(self.hod, self._filters()).count(), 2)
        self.assertEqual(
            svc.scoped_sessions(self.hod, self._filters(semester="3")).count(), 1)

    # -------------------------------------------------------------- students
    def test_students_are_filtered_by_the_subjects_they_take(self):
        everyone = {s.email for s in svc.scoped_students(self.hod, self._filters())}
        self.assertEqual(everyone, {"third@i.edu", "fifth@i.edu", "both@i.edu"})

        third = {s.email for s in svc.scoped_students(self.hod, self._filters(semester="3"))}
        self.assertEqual(third, {"third@i.edu", "both@i.edu"})

        fifth = {s.email for s in svc.scoped_students(self.hod, self._filters(semester="5"))}
        self.assertEqual(fifth, {"fifth@i.edu", "both@i.edu"})

    def test_a_student_taking_both_is_not_duplicated(self):
        rows = svc.student_report(self.hod, self._filters(semester="3"))
        emails = [r["email"] for r in rows]
        self.assertEqual(len(emails), len(set(emails)))

    # --------------------------------------------------------------- reports
    def test_the_subject_report_honours_it(self):
        codes = {r["code"] for r in svc.subject_report(self.hod, self._filters(semester="3"))}
        self.assertEqual(codes, {"DSA"})

    def test_the_kpis_honour_it(self):
        both = svc.kpi_summary(self.hod, self._filters())
        one = svc.kpi_summary(self.hod, self._filters(semester="3"))
        self.assertEqual(both["classes_conducted"], 2)
        self.assertEqual(one["classes_conducted"], 1)
        self.assertEqual(one["students"], 2)          # third + both

    def test_it_combines_with_the_subject_filter(self):
        """
        Semester 5 with a semester-3 subject is a legitimate empty result.

        The subject is set on the filter directly rather than through a query
        string: parsing runs ids through clean_object_id, which expects the
        24-char hex of a real ObjectId, and the sqlite test harness uses
        integer keys.
        """
        f = self._filters(semester="5")
        f.subject = self.sem3.pk
        self.assertEqual(svc.scoped_sessions(self.hod, f).count(), 0)

        f.semester = 3
        self.assertEqual(svc.scoped_sessions(self.hod, f).count(), 1)

    # ------------------------------------------------------------------ page
    def test_the_page_offers_only_semesters_that_exist(self):
        from django.urls import reverse

        client = self.client_class()
        client.force_login(self.hod)
        for name in ("dashboard:home", "dashboard:reports"):
            with self.subTest(page=name):
                html = client.get(reverse(name)).content.decode()
                self.assertIn('id="f-semester"', html)
                self.assertIn("Semester 3", html)
                self.assertIn("Semester 5", html)
                self.assertNotIn("Semester 1<", html)   # no empty semesters

    def test_the_export_honours_it(self):
        from django.urls import reverse

        client = self.client_class()
        client.force_login(self.hod)
        body = client.get(reverse("dashboard:export_subjects"),
                          {"semester": "3"}).content.decode()
        self.assertIn("DSA", body)
        self.assertNotIn("CN", body)
