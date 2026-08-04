"""
Archiving a batch must make it — and everything derived from it — vanish from
the entire application, and reappear untouched when it is restored.

These tests walk every surface that could leak an archived cohort.
"""
import datetime as dt

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from academics.models import (
    Batch, Department, Enrollment, StudentProfile, Subject, TeacherAssignment,
)
from academics.selectors import batches_for, enrolled_students, students_qs_for, subjects_for
from accounts.models import Institute, User
from attendance.models import AttendanceRecord, AttendanceSession
from attendance.services import AttendanceError, create_session
from dashboard.filters import ReportFilters
from dashboard.services import kpi_summary, scoped_sessions, student_report, subject_report

PW = "Str0ngPass!23"


class ArchivedBatchBase(TestCase):
    def setUp(self):
        self.institute = Institute.objects.create(name="I", code="I", email="i@i.edu")
        self.dept = Department.objects.create(institute=self.institute, name="CSE", code="CSE")
        self.live = Batch.objects.create(department=self.dept, label="2023-27",
                                         start_year=2023, end_year=2027)
        self.doomed = Batch.objects.create(department=self.dept, label="2019-23",
                                           start_year=2019, end_year=2023)
        self.dsa = Subject.objects.create(department=self.dept, code="DSA", name="Data Structures")

        self.head = self._user("head@i.edu", "HEAD")
        self.hod = self._user("hod@i.edu", "HOD")
        self.dept.hod = self.hod
        self.dept.save()
        self.teacher = self._user("t@i.edu", "TEACHER")
        for batch in (self.live, self.doomed):
            TeacherAssignment.objects.create(teacher=self.teacher, subject=self.dsa, batch=batch)

        self.current = self._student("now@i.edu", "Current Student", self.live)
        self.alumni = self._student("old@i.edu", "Alumni Student", self.doomed)

        # one class per batch, both attended
        for batch, student in ((self.live, self.current), (self.doomed, self.alumni)):
            session = AttendanceSession.objects.create(
                teacher=self.teacher, subject=self.dsa, batch=batch,
                latitude=22.5726, longitude=88.3639, radius_m=50,
                session_date=timezone.localdate() - dt.timedelta(days=1),
                expected_count=1, status=AttendanceSession.Status.CLOSED,
                expires_at=timezone.now(),
            )
            AttendanceRecord.objects.create(session=session, student=student,
                                            status=AttendanceRecord.Status.PRESENT)

    def _user(self, email, role):
        return User.objects.create_user(
            email=email, password=PW, full_name=email.split("@")[0].title(), role=role,
            institute=self.institute, department=self.dept, registration_completed=True)

    def _student(self, email, name, batch):
        user = User.objects.create_user(
            email=email, password=PW, full_name=name, role="STUDENT",
            institute=self.institute, department=self.dept, registration_completed=True)
        profile = StudentProfile.objects.create(
            user=user, department=self.dept, batch=batch, class_roll=name[:4],
            mobile="+919812345670", guardian_mobile="+919812345671")
        Enrollment.objects.create(student=profile, subject=self.dsa)
        return profile

    def archive(self):
        self.doomed.is_active = False
        self.doomed.save(update_fields=["is_active"])

    def restore(self):
        self.doomed.is_active = True
        self.doomed.save(update_fields=["is_active"])

    def filters(self):
        class Req:
            GET = {}
        return ReportFilters.from_request(Req())


# --------------------------------------------------------------------------- #
#  Selectors — the choke point
# --------------------------------------------------------------------------- #
class SelectorTests(ArchivedBatchBase):
    def test_batches_hidden_but_available_to_the_admin_screen(self):
        self.archive()
        self.assertEqual([b.label for b in batches_for(self.hod)], ["2023-27"])
        self.assertEqual(
            sorted(b.label for b in batches_for(self.hod, include_inactive=True)),
            ["2019-23", "2023-27"])

    def test_students_hidden_for_every_staff_role(self):
        self.archive()
        for user in (self.head, self.hod, self.teacher):
            names = {s.name for s in students_qs_for(user)}
            self.assertEqual(names, {"Current Student"}, user.role)

    def test_enrolled_students_excludes_an_archived_batch(self):
        self.assertEqual(enrolled_students(self.dsa, self.doomed).count(), 1)
        self.archive()
        self.assertEqual(enrolled_students(self.dsa, self.doomed).count(), 0)
        self.assertEqual(enrolled_students(self.dsa, self.live).count(), 1)

    def test_teacher_loses_a_subject_taught_only_to_archived_batches(self):
        TeacherAssignment.objects.filter(teacher=self.teacher, batch=self.live).delete()
        self.assertEqual(subjects_for(self.teacher).count(), 1)
        self.archive()
        self.assertEqual(subjects_for(self.teacher).count(), 0)

    def test_archived_student_sees_nothing_of_their_own(self):
        self.archive()
        self.assertEqual(subjects_for(self.alumni.user).count(), 0)
        self.assertEqual(scoped_sessions(self.alumni.user).count(), 0)


# --------------------------------------------------------------------------- #
#  Analytics
# --------------------------------------------------------------------------- #
class AnalyticsTests(ArchivedBatchBase):
    def test_sessions_disappear_from_analytics(self):
        self.assertEqual(scoped_sessions(self.head, self.filters()).count(), 2)
        self.archive()
        self.assertEqual(scoped_sessions(self.head, self.filters()).count(), 1)

    def test_kpis_drop_the_archived_cohort(self):
        before = kpi_summary(self.head, self.filters())
        self.assertEqual(before["students"], 2)
        self.assertEqual(before["classes_conducted"], 2)
        self.archive()
        after = kpi_summary(self.head, self.filters())
        self.assertEqual(after["students"], 1)
        self.assertEqual(after["classes_conducted"], 1)

    def test_student_report_drops_the_archived_cohort(self):
        self.archive()
        names = {r["name"] for r in student_report(self.head, self.filters())}
        self.assertEqual(names, {"Current Student"})

    def test_subject_report_counts_only_live_classes(self):
        self.archive()
        rows = subject_report(self.head, self.filters())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["batch"], "2023-27")

    def test_restoring_brings_every_figure_back(self):
        before = kpi_summary(self.head, self.filters())
        self.archive()
        self.restore()
        self.assertEqual(kpi_summary(self.head, self.filters()), before)


# --------------------------------------------------------------------------- #
#  Attendance
# --------------------------------------------------------------------------- #
class AttendanceTests(ArchivedBatchBase):
    def test_cannot_open_attendance_for_an_archived_batch(self):
        self.archive()
        with self.assertRaises(AttendanceError) as ctx:
            create_session(teacher=self.teacher, subject=self.dsa, batch=self.doomed,
                           latitude=22.5726, longitude=88.3639)
        self.assertEqual(ctx.exception.code, "BATCH_ARCHIVED")

    def test_live_batch_still_works(self):
        self.archive()
        session = create_session(teacher=self.teacher, subject=self.dsa, batch=self.live,
                                 latitude=22.5726, longitude=88.3639)
        self.assertEqual(session.expected_count, 1)

    def test_session_list_and_detail_hide_archived_sessions(self):
        archived_session = AttendanceSession.objects.get(batch=self.doomed)
        self.client.force_login(self.hod)
        self.archive()
        rows = self.client.get(reverse("attendance:api_sessions")).json()["data"]["rows"]
        self.assertEqual([r["batch"] for r in rows], ["2023-27"])
        for name in ("attendance:api_session_status", "attendance:session_detail",
                     "attendance:api_session_export"):
            self.assertEqual(
                self.client.get(reverse(name, args=[archived_session.id])).status_code,
                404, name)


# --------------------------------------------------------------------------- #
#  Every JSON surface
# --------------------------------------------------------------------------- #
class ApiSurfaceTests(ArchivedBatchBase):
    def _json(self, name, user=None, **params):
        self.client.force_login(user or self.hod)
        return self.client.get(reverse(name), params).json()["data"]

    def test_student_list_and_export_and_lookups(self):
        self.archive()
        rows = self._json("academics:api_students")["rows"]
        self.assertEqual([r["name"] for r in rows], ["Current Student"])

        lookups = self._json("academics:api_lookups")
        self.assertEqual([b["label"] for b in lookups["batches"]], ["2023-27"])

        self.client.force_login(self.hod)
        response = self.client.get(reverse("academics:api_students_export"))
        body = b"".join(response.streaming_content)
        self.assertGreater(len(body), 0)

    def test_subject_enrolment_count_ignores_archived_students(self):
        rows = self._json("academics:api_subjects")["rows"]
        self.assertEqual(rows[0]["student_count"], 2)
        self.archive()
        rows = self._json("academics:api_subjects")["rows"]
        self.assertEqual(rows[0]["student_count"], 1)

    def test_department_counts_ignore_archived_batches(self):
        rows = self._json("academics:api_departments", user=self.head)["rows"]
        self.assertEqual(rows[0]["student_count"], 2)
        self.assertEqual(rows[0]["batch_count"], 2)
        self.archive()
        rows = self._json("academics:api_departments", user=self.head)["rows"]
        self.assertEqual(rows[0]["student_count"], 1)
        self.assertEqual(rows[0]["batch_count"], 1)

    def test_batch_admin_screen_still_lists_archived_batches(self):
        self.archive()
        rows = self._json("academics:api_batches")["rows"]
        self.assertEqual(len(rows), 2)
        archived = next(r for r in rows if r["label"] == "2019-23")
        self.assertFalse(archived["is_active"])

    def test_teacher_allocation_chips_hide_archived_batches(self):
        self.archive()
        rows = self._json("academics:api_teachers")["rows"]
        teacher = next(r for r in rows if r["email"] == "t@i.edu")
        self.assertEqual([a["batch"] for a in teacher["assignments"]], ["2023-27"])

    def test_teacher_batch_picker_and_dashboard(self):
        self.archive()
        rows = self._json("attendance:api_my_batches", user=self.teacher)["rows"]
        self.assertEqual([b["label"] for b in rows], ["2023-27"])
        self.client.force_login(self.teacher)
        self.assertEqual(
            self.client.get(
                reverse("attendance:api_batch_subjects", args=[self.doomed.id])
            ).status_code, 404)

    def test_alert_recipients_never_include_archived_students(self):
        # A second, unattended class in each batch drops both students to 50%.
        for batch in (self.live, self.doomed):
            AttendanceSession.objects.create(
                teacher=self.teacher, subject=self.dsa, batch=batch,
                latitude=22.5726, longitude=88.3639, radius_m=50,
                session_date=timezone.localdate(), expected_count=1,
                status=AttendanceSession.Status.CLOSED, expires_at=timezone.now())

        data = self._json("notifications:api_recipients", threshold=75)
        self.assertEqual(sorted(r["name"] for r in data["rows"]),
                         ["Alumni Student", "Current Student"])

        self.archive()
        data = self._json("notifications:api_recipients", threshold=75)
        self.assertEqual([r["name"] for r in data["rows"]], ["Current Student"])

    def test_reports_endpoints(self):
        self.archive()
        rows = self._json("dashboard:api_students_report")["rows"]
        self.assertEqual([r["name"] for r in rows], ["Current Student"])
        batches = self._json("dashboard:api_batches_report")["rows"]
        self.assertEqual([b["label"] for b in batches], ["2023-27"])


# --------------------------------------------------------------------------- #
#  Write paths must refuse archived batches
# --------------------------------------------------------------------------- #
class WriteGuardTests(ArchivedBatchBase):
    def test_cannot_move_a_student_into_an_archived_batch(self):
        self.archive()
        self.client.force_login(self.hod)
        response = self.client.post(
            reverse("academics:api_student_save", args=[self.current.id]),
            {"full_name": "Current Student", "batch_id": self.doomed.id,
             "guardian_mobile": "+919812345671"},
        )
        self.assertEqual(response.status_code, 404)
        self.current.refresh_from_db()
        self.assertEqual(self.current.batch, self.live)

    def test_cannot_allocate_a_teacher_to_an_archived_batch(self):
        import json

        self.archive()
        self.client.force_login(self.hod)
        response = self.client.post(reverse("academics:api_teacher_invite"), {
            "email": "new@i.edu",
            "assignments": json.dumps([{"subject_id": self.dsa.id,
                                        "batch_id": self.doomed.id}]),
        })
        self.assertEqual(response.status_code, 403)

    def test_import_into_an_archived_batch_is_refused(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from academics.importer import import_students, read_rows

        self.archive()
        header = ("Name,Mobile Number,Email,Batch,Subjects Enrolled,"
                  "Guardian Mobile,Guardian Name,Roll Number")
        upload = SimpleUploadedFile(
            "r.csv",
            (header + "\nNew Student,1,new@i.edu,2019-23,DSA,+919812345670,G,R9").encode(),
            content_type="text/csv")
        rows, _ = read_rows(upload)
        job = import_students(rows, self.dept, self.hod, send_invites=False)
        self.assertEqual(job.error_count, 1)
        self.assertIn("archived", job.report["rows"][0]["messages"][0])

    def test_toggle_endpoint_archives_and_restores(self):
        self.client.force_login(self.hod)
        url = reverse("academics:api_batch_toggle", args=[self.doomed.id])

        response = self.client.post(url)
        self.assertFalse(response.json()["data"]["is_active"])
        self.doomed.refresh_from_db()
        self.assertFalse(self.doomed.is_active)
        self.assertEqual(len(student_report(self.head, self.filters())), 1)

        response = self.client.post(url)
        self.assertTrue(response.json()["data"]["is_active"])
        self.assertEqual(len(student_report(self.head, self.filters())), 2)

    def test_nothing_is_deleted_by_archiving(self):
        self.archive()
        self.assertEqual(StudentProfile.objects.count(), 2)
        self.assertEqual(AttendanceSession.objects.count(), 2)
        self.assertEqual(AttendanceRecord.objects.count(), 2)
        self.assertEqual(Enrollment.objects.count(), 2)


class ArchivedStudentOwnViewTests(ArchivedBatchBase):
    """A student whose batch is archived is told, not shown a silent zero."""

    def test_flag_is_reported(self):
        self.client.force_login(self.alumni.user)
        data = self.client.get(reverse("dashboard:api_my_summary")).json()["data"]
        self.assertFalse(data["batch_archived"])

        self.archive()
        data = self.client.get(reverse("dashboard:api_my_summary")).json()["data"]
        self.assertTrue(data["batch_archived"])
        self.assertEqual(data["batch_label"], "2019-23")
        self.assertEqual(data["overall"]["held"], 0)

    def test_unaffected_student_is_not_flagged(self):
        self.archive()
        self.client.force_login(self.current.user)
        data = self.client.get(reverse("dashboard:api_my_summary")).json()["data"]
        self.assertFalse(data["batch_archived"])
        self.assertEqual(data["overall"]["held"], 1)

    def test_archived_student_cannot_mark_attendance(self):
        """The link still resolves, but the session is invisible to them."""
        from attendance.models import AttendanceSession

        session = AttendanceSession.objects.get(batch=self.doomed)
        session.status = AttendanceSession.Status.OPEN
        session.expires_at = timezone.now() + dt.timedelta(minutes=5)
        session.save()
        AttendanceRecord.objects.filter(session=session).delete()
        self.archive()

        self.client.force_login(self.alumni.user)
        response = self.client.post(
            reverse("attendance:api_mark", args=[session.token]),
            {"latitude": 22.5726, "longitude": 88.3639, "accuracy": 5})
        self.assertFalse(response.json()["success"])
        self.assertFalse(AttendanceRecord.objects.filter(session=session).exists())
