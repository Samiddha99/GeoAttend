"""The semester filter across the dashboard and reports."""
import datetime as dt

from django.test import RequestFactory, TestCase
from django.utils import timezone

from academics.models import Batch, Department, Enrollment, StudentProfile, Subject, TeacherAssignment
from accounts.models import Institute, InstituteAffiliation, User
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


class SemesterOptionsTests(TestCase):
    """
    The dropdown must offer each semester once.

    `Subject.Meta.ordering` is `["semester", "code"]`, and Django appends every
    ordering field to the SELECT — so `.values_list("semester").distinct()` was
    distinct over *(semester, code)* and two subjects in semester 3 produced
    "Semester 3" twice. The fix is `.order_by()` before the values_list, which
    looks like decoration and is the whole thing.
    """

    def setUp(self):
        self.institute = Institute.objects.create(
            name="I2", code="I2", email="i2@i.edu")
        self.cse = Department.objects.create(
            institute=self.institute, name="CSE", code="CSE", discipline="ENGG")
        for code, semester in (("A", 3), ("B", 3), ("C", 3), ("D", 5)):
            Subject.objects.create(department=self.cse, code=code, name=code,
                                   semester=semester)
        self.head = User.objects.create_user(
            email="head2@i.edu", password="Str0ngPass!23", role="HEAD",
            institute=self.institute, registration_completed=True)

    def test_each_semester_appears_once(self):
        from academics.selectors import semester_options

        self.assertEqual(semester_options(self.head), [3, 5])

    def test_the_naive_query_is_what_was_wrong(self):
        """
        The control. If Django stopped dragging the ordering into the SELECT,
        the test above would be asserting nothing and nobody would notice.
        """
        from academics.selectors import subjects_for

        naive = list(subjects_for(self.head).exclude(semester=None)
                     .values_list("semester", flat=True).distinct())
        self.assertNotEqual(sorted(set(naive)), sorted(naive))

    def test_it_offers_only_semesters_that_exist(self):
        """
        A fixed 1..12 list would offer filters that can never match. (There is
        no "no semester" case to test: the column is NOT NULL with a default of
        1, so the `exclude(semester=None)` in the helper is belt-and-braces.)
        """
        from academics.selectors import semester_options

        self.assertNotIn(1, semester_options(self.head))
        self.assertNotIn(8, semester_options(self.head))

    def test_the_page_renders_each_option_once(self):
        self.client.force_login(self.head)
        html = self.client.get("/app/").content.decode()
        self.assertEqual(html.count(">Semester 3</option>"), 1)
        self.assertEqual(html.count(">Semester 5</option>"), 1)


class DisciplineFilterTests(TestCase):
    """The dashboard's new discipline filter, on the server side."""

    def setUp(self):
        self.institute = Institute.objects.create(
            name="I3", code="I3", email="i3@i.edu")
        self.engg = Department.objects.create(
            institute=self.institute, name="CSE", code="CSE", discipline="ENGG")
        self.arts = Department.objects.create(
            institute=self.institute, name="Arts", code="ART",
            discipline="GENERAL")
        # Both disciplines on file, or the departments read as revoked and
        # every scoped query correctly returns nothing — which is what the
        # first version of this fixture was actually testing.
        for discipline in ("ENGG", "GENERAL"):
            InstituteAffiliation.objects.create(
                institute=self.institute, discipline=discipline, university=None)
        self.head = User.objects.create_user(
            email="head3@i.edu", password="Str0ngPass!23", role="HEAD",
            institute=self.institute, registration_completed=True)
        self.teacher = User.objects.create_user(
            email="t3@i.edu", password="Str0ngPass!23", role="TEACHER",
            institute=self.institute, department=self.engg,
            registration_completed=True)

        for department in (self.engg, self.arts):
            batch = Batch.objects.create(
                department=department, label="2022-26",
                start_year=2022, end_year=2026)
            subject = Subject.objects.create(
                department=department, code=f"S{department.code}",
                name="S", semester=1)
            AttendanceSession.objects.create(
                teacher=self.teacher, subject=subject, batch=batch,
                latitude=0, longitude=0, session_date=timezone.localdate(),
                expires_at=timezone.now(), status="CLOSED")
            user = User.objects.create_user(
                email=f"s{department.code}@i.edu", password="Str0ngPass!23",
                role="STUDENT", institute=self.institute)
            profile = StudentProfile.objects.create(
                user=user, department=department, batch=batch, class_roll="1")
            Enrollment.objects.create(student=profile, subject=subject)

    def _filters(self, **params):
        request = RequestFactory().get("/", params)
        return ReportFilters.from_request(request)

    def test_it_narrows_the_sessions_to_one_discipline(self):
        self.assertEqual(
            svc.scoped_sessions(self.head, self._filters(discipline="ENGG")).count(), 1)
        self.assertEqual(
            svc.scoped_sessions(self.head, self._filters()).count(), 2)

    def test_it_narrows_the_students_by_their_department_not_their_subjects(self):
        """
        Unlike degree and subject-type, a student *is* in a discipline — it
        comes from their department. So this is a direct filter, and a student
        with no enrolments is still counted.
        """
        self.assertEqual(
            svc.scoped_students(self.head, self._filters(discipline="GENERAL")).count(), 1)

    def test_an_unknown_discipline_is_ignored_rather_than_matching_nothing(self):
        """A typo in a bookmark should not produce an empty page of real data."""
        self.assertIsNone(self._filters(discipline="NONSENSE").discipline)
        self.assertEqual(
            svc.scoped_sessions(self.head, self._filters(discipline="NONSENSE")).count(), 2)
