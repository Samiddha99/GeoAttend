import datetime as dt

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from academics.models import (
    Batch, Department, Enrollment, StudentProfile, Subject, TeacherAssignment,
)
from accounts.models import Institute, User
from attendance.models import AttendanceRecord, AttendanceSession, MarkAttempt
from attendance.services import AttendanceError, create_session

PW = "Str0ngPass!23"
LAT, LNG = 22.572600, 88.363900
# ~0.001 deg latitude ≈ 111 m — comfortably outside a 50 m fence
FAR_LAT = LAT + 0.001


class AttendanceBase(TestCase):
    def setUp(self):
        self.institute = Institute.objects.create(name="I", code="I", email="i@i.edu")
        self.dept = Department.objects.create(institute=self.institute, name="CSE", code="CSE")
        self.batch = Batch.objects.create(department=self.dept, label="2022-26",
                                          start_year=2022, end_year=2026)
        self.other_batch = Batch.objects.create(department=self.dept, label="2023-27",
                                                start_year=2023, end_year=2027)
        self.dsa = Subject.objects.create(department=self.dept, code="DSA", name="Data Structures")
        self.ai = Subject.objects.create(department=self.dept, code="AI", name="AI")
        self.teacher = User.objects.create_user(
            email="t@i.edu", password=PW, full_name="T", role="TEACHER",
            institute=self.institute, department=self.dept, registration_completed=True,
        )
        TeacherAssignment.objects.create(teacher=self.teacher, subject=self.dsa, batch=self.batch)
        self.students = []
        for i in range(3):
            u = User.objects.create_user(
                email=f"s{i}@i.edu", password=PW, full_name=f"S{i}", role="STUDENT",
                institute=self.institute, department=self.dept, registration_completed=True,
            )
            p = StudentProfile.objects.create(user=u, department=self.dept, batch=self.batch,
                                              class_roll=f"R{i}")
            Enrollment.objects.create(student=p, subject=self.dsa)
            self.students.append(p)

    def make_session(self, minutes=5, radius=50):
        return create_session(
            teacher=self.teacher, subject=self.dsa, batch=self.batch,
            latitude=LAT, longitude=LNG, accuracy=8, minutes=minutes, radius=radius,
        )

    def mark(self, student, session, lat=LAT, lng=LNG, accuracy=10, device="dev-a"):
        c = self.client_for(student)
        return c.post(reverse("attendance:api_mark", args=[session.token]), {
            "latitude": lat, "longitude": lng, "accuracy": accuracy, "device_hash": device,
        }, HTTP_USER_AGENT="pytest")

    def client_for(self, student):
        from django.test import Client
        c = Client()
        c.force_login(student.user)
        return c


class SessionCreationTests(AttendanceBase):
    def test_teacher_must_be_assigned(self):
        with self.assertRaises(AttendanceError) as ctx:
            create_session(teacher=self.teacher, subject=self.ai, batch=self.batch,
                           latitude=LAT, longitude=LNG)
        self.assertEqual(ctx.exception.code, "NOT_ASSIGNED")

    def test_location_required(self):
        with self.assertRaises(AttendanceError) as ctx:
            create_session(teacher=self.teacher, subject=self.dsa, batch=self.batch,
                           latitude=None, longitude=None)
        self.assertEqual(ctx.exception.code, "NO_LOCATION")

    def test_expiry_bounds_enforced(self):
        with self.assertRaises(AttendanceError):
            create_session(teacher=self.teacher, subject=self.dsa, batch=self.batch,
                           latitude=LAT, longitude=LNG, minutes=9999)

    def test_defaults_are_five_minutes_and_fifty_metres(self):
        s = create_session(teacher=self.teacher, subject=self.dsa, batch=self.batch,
                           latitude=LAT, longitude=LNG)
        self.assertEqual(s.radius_m, 50)
        self.assertAlmostEqual(s.seconds_left, 300, delta=5)
        self.assertEqual(s.expected_count, 3)

    def test_new_session_closes_the_previous_open_one(self):
        first = self.make_session()
        self.make_session()
        first.refresh_from_db()
        self.assertEqual(first.status, AttendanceSession.Status.CLOSED)


class GeoFenceTests(AttendanceBase):
    def test_inside_fence_marks_present(self):
        s = self.make_session()
        res = self.mark(self.students[0], s)
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["success"])
        rec = AttendanceRecord.objects.get(session=s, student=self.students[0])
        self.assertEqual(rec.status, AttendanceRecord.Status.PRESENT)
        self.assertLess(rec.distance_m, 50)

    def test_outside_fence_rejected(self):
        s = self.make_session()
        res = self.mark(self.students[0], s, lat=FAR_LAT)
        self.assertEqual(res.status_code, 403)
        body = res.json()
        self.assertEqual(body["code"], "OUT_OF_RANGE")
        self.assertIn("not present in the class", body["message"])
        self.assertFalse(AttendanceRecord.objects.filter(session=s).exists())
        self.assertTrue(MarkAttempt.objects.filter(reason=MarkAttempt.Reason.OUT_OF_RANGE).exists())

    def test_expired_link_rejected(self):
        s = self.make_session()
        AttendanceSession.objects.filter(pk=s.pk).update(
            expires_at=timezone.now() - dt.timedelta(seconds=1))
        s.refresh_from_db()
        res = self.mark(self.students[0], s)
        self.assertEqual(res.status_code, 410)
        self.assertEqual(res.json()["code"], "EXPIRED")

    def test_poor_gps_accuracy_rejected(self):
        s = self.make_session()
        res = self.mark(self.students[0], s, accuracy=500)
        self.assertEqual(res.json()["code"], "LOW_ACCURACY")

    def test_missing_coordinates_rejected(self):
        s = self.make_session()
        c = self.client_for(self.students[0])
        res = c.post(reverse("attendance:api_mark", args=[s.token]), {"device_hash": "d"})
        self.assertEqual(res.json()["code"], "NO_LOCATION")

    def test_duplicate_mark_rejected(self):
        s = self.make_session()
        self.mark(self.students[0], s)
        res = self.mark(self.students[0], s)
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.json()["code"], "DUPLICATE")
        self.assertEqual(AttendanceRecord.objects.filter(session=s).count(), 1)

    def test_student_not_enrolled_rejected(self):
        outsider_user = User.objects.create_user(
            email="out@i.edu", password=PW, role="STUDENT", institute=self.institute,
            department=self.dept, registration_completed=True)
        outsider = StudentProfile.objects.create(user=outsider_user, department=self.dept,
                                                 batch=self.batch, class_roll="RX")
        s = self.make_session()
        res = self.mark(outsider, s)
        self.assertEqual(res.json()["code"], "NOT_ENROLLED")

    def test_wrong_batch_rejected(self):
        s = self.make_session()
        self.students[0].batch = self.other_batch
        self.students[0].save()
        res = self.mark(self.students[0], s)
        self.assertEqual(res.json()["code"], "WRONG_BATCH")

    def test_anonymous_is_redirected_to_login_then_back(self):
        s = self.make_session()
        res = self.client.get(reverse("attendance:mark", args=[s.token]))
        self.assertEqual(res.status_code, 302)
        self.assertIn("/auth/login/", res.url)
        self.assertIn(s.token, res.url)

    def test_teacher_account_cannot_mark(self):
        s = self.make_session()
        from django.test import Client
        c = Client()
        c.force_login(self.teacher)
        res = c.post(reverse("attendance:api_mark", args=[s.token]),
                     {"latitude": LAT, "longitude": LNG, "accuracy": 5})
        self.assertEqual(res.json()["code"], "NOT_STUDENT")


class AntiProxyTests(AttendanceBase):
    def test_device_is_bound_on_first_mark_and_enforced_after(self):
        s1 = self.make_session()
        self.mark(self.students[0], s1, device="phone-1")
        self.students[0].user.refresh_from_db()
        self.assertTrue(self.students[0].user.device_id)

        s2 = self.make_session()
        res = self.mark(self.students[0], s2, device="phone-2")
        self.assertEqual(res.json()["code"], "DEVICE_MISMATCH")

    def test_two_students_cannot_share_one_device_in_a_session(self):
        s = self.make_session()
        self.mark(self.students[0], s, device="shared")
        res = self.mark(self.students[1], s, device="shared")
        self.assertEqual(res.json()["code"], "SHARED_DEVICE")


class TeacherControlTests(AttendanceBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.teacher)

    def test_manual_mark_and_unmark(self):
        s = self.make_session()
        res = self.client.post(reverse("attendance:api_manual_mark", args=[s.id]),
                               {"student": self.students[0].id, "present": "1"})
        self.assertTrue(res.json()["success"])
        rec = AttendanceRecord.objects.get(session=s)
        self.assertEqual(rec.status, AttendanceRecord.Status.MANUAL)
        self.assertEqual(rec.marked_by, self.teacher)

        self.client.post(reverse("attendance:api_manual_mark", args=[s.id]),
                         {"student": self.students[0].id, "present": "0"})
        self.assertFalse(AttendanceRecord.objects.filter(session=s).exists())

    def test_close_and_extend(self):
        s = self.make_session()
        self.client.post(reverse("attendance:api_session_action", args=[s.id, "close"]))
        s.refresh_from_db()
        self.assertEqual(s.effective_status, "CLOSED")
        self.client.post(reverse("attendance:api_session_action", args=[s.id, "extend"]),
                         {"minutes": 10})
        s.refresh_from_db()
        self.assertEqual(s.effective_status, "OPEN")

    def test_teacher_cannot_touch_someone_elses_session(self):
        other = User.objects.create_user(email="t2@i.edu", password=PW, role="TEACHER",
                                         institute=self.institute, department=self.dept,
                                         registration_completed=True)
        s = self.make_session()
        from django.test import Client
        c = Client()
        c.force_login(other)
        self.assertEqual(c.get(reverse("attendance:api_session_status", args=[s.id])).status_code, 404)

    def test_status_endpoint_reports_present_and_absent(self):
        s = self.make_session()
        self.mark(self.students[0], s)
        data = self.client.get(reverse("attendance:api_session_status", args=[s.id])).json()["data"]
        self.assertEqual(data["present_count"], 1)
        self.assertEqual(data["absent_count"], 2)
        self.assertEqual(data["expected"], 3)

    def test_csv_export(self):
        s = self.make_session()
        self.mark(self.students[0], s)
        res = self.client.get(reverse("attendance:api_session_export", args=[s.id]))
        self.assertEqual(res.status_code, 200)
        body = res.content.decode()
        self.assertIn("PRESENT", body)
        self.assertIn("ABSENT", body)


class ReportMathTests(AttendanceBase):
    def test_percentages(self):
        from dashboard.filters import ReportFilters
        from dashboard.services import student_report, subject_report

        class Req:
            GET = {}

        for _ in range(4):
            s = self.make_session()
            self.mark(self.students[0], s)          # 4 / 4
            s.close()
        s = self.make_session()
        self.mark(self.students[1], s)              # student 1: 1 / 5
        s.close()

        f = ReportFilters.from_request(Req())
        rows = {r["student_id"]: r for r in student_report(self.teacher, f)}
        self.assertEqual(rows[self.students[0].id]["held"], 5)
        self.assertEqual(rows[self.students[0].id]["attended"], 4)
        self.assertEqual(rows[self.students[0].id]["percentage"], 80.0)
        self.assertEqual(rows[self.students[1].id]["percentage"], 20.0)
        self.assertEqual(rows[self.students[2].id]["percentage"], 0.0)

        subj = subject_report(self.teacher, f)[0]
        self.assertEqual(subj["classes"], 5)
        self.assertEqual(subj["enrolled"], 3)
        self.assertEqual(subj["percentage"], round(5 * 100 / 15, 2))


class StudentSelfViewTests(AttendanceBase):
    """The numbers a student sees must match what staff see for that student."""

    def test_student_and_staff_agree(self):
        from dashboard.filters import ReportFilters
        from dashboard.services import student_detail, student_report

        class Req:
            GET = {}

        for _ in range(3):
            s = self.make_session()
            self.mark(self.students[0], s)
            s.close()
        s = self.make_session()
        s.close()                                    # a class they missed

        f = ReportFilters.from_request(Req())
        staff = {r["student_id"]: r for r in student_report(self.teacher, f)}[self.students[0].id]
        own = student_detail(self.students[0].user, f, self.students[0])["overall"]
        self.assertEqual(staff["held"], own["held"])
        self.assertEqual(staff["attended"], own["attended"])
        self.assertEqual(own["held"], 4)
        self.assertEqual(own["attended"], 3)
        self.assertEqual(own["percentage"], 75.0)
