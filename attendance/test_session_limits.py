"""
The ceilings on link validity and geo-fence radius.

These are the two knobs that decide whether the geo-fence means anything. A
link that outlives the lesson can be used from the car park; a fence wider than
the building cannot tell the classroom from the corridor. So the limits are
enforced on the server, and this file is mostly about the ways a browser might
try to get round them.

`ATTENDANCE` is overridden wholesale in these tests, which is exactly why
`create_session` reads `settings` on each call rather than the module-level
CONF snapshot — CONF is bound at import and would never see the override.
"""
import datetime as dt

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from academics.models import (
    Batch,
    Department,
    Enrollment,
    StudentProfile,
    Subject,
    TeacherAssignment,
)
from accounts.models import Institute, User
from attendance.models import AttendanceSession
from attendance.services import AttendanceError, create_session, session_limits

LIMITS = {
    "MIN_EXPIRY_MIN": 1, "MAX_EXPIRY_MIN": 30, "DEFAULT_EXPIRY_MIN": 5,
    "MIN_RADIUS_M": 10, "MAX_RADIUS_M": 50, "DEFAULT_RADIUS_M": 50,
    "MAX_GPS_ACCURACY_M": 100000,
}


class SessionLimitFixture(TestCase):
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
        TeacherAssignment.objects.create(teacher=self.teacher, subject=self.subject,
                                         batch=self.batch, is_active=True)
        student = User.objects.create_user(
            email="s@i.edu", password="Str0ngPass!23", role="STUDENT",
            institute=self.institute, department=self.dept, full_name="Asha",
            registration_completed=True, face_enrolled=True)
        profile = StudentProfile.objects.create(
            user=student, department=self.dept, batch=self.batch, class_roll="01")
        Enrollment.objects.create(student=profile, subject=self.subject,
                                  is_active=True)

    def _create(self, **kwargs):
        params = dict(teacher=self.teacher, subject=self.subject, batch=self.batch,
                      latitude=12.9, longitude=77.5, accuracy=10)
        params.update(kwargs)
        return create_session(**params)


@override_settings(ATTENDANCE=LIMITS)
class ValidityCeilingTests(SessionLimitFixture):
    def test_the_ceiling_is_accepted_exactly(self):
        session = self._create(minutes=30)
        self.assertEqual(session.validity_minutes, 30)

    def test_one_minute_over_is_refused(self):
        with self.assertRaises(AttendanceError) as caught:
            self._create(minutes=31)
        self.assertEqual(caught.exception.code, "BAD_EXPIRY")
        self.assertIn("between 1 and 30", caught.exception.message)

    def test_it_refuses_rather_than_clamping(self):
        """
        Silently rounding 300 down to 30 would hand the teacher a link that
        behaves differently from what they asked for, and they would only find
        out when it expired mid-lesson.
        """
        with self.assertRaises(AttendanceError):
            self._create(minutes=300)
        self.assertFalse(AttendanceSession.objects.exists())

    def test_below_the_floor_is_refused(self):
        with self.assertRaises(AttendanceError):
            self._create(minutes=0)

    def test_junk_is_refused_with_a_message_not_a_crash(self):
        for value in ("abc", "10.5", "  ", "-"):
            with self.subTest(value=value):
                with self.assertRaises(AttendanceError) as caught:
                    self._create(minutes=value)
                self.assertEqual(caught.exception.code, "BAD_EXPIRY")

    def test_omitting_it_falls_back_to_the_default(self):
        self.assertEqual(self._create().validity_minutes, 5)

    def test_the_limits_come_from_settings_not_an_import_time_snapshot(self):
        """
        `create_session` used to read a dict bound at import, so overriding
        ATTENDANCE had no effect and a test like this would pass against the
        old ceiling without noticing.
        """
        self.assertEqual(session_limits()["MAX_EXPIRY_MIN"], 30)
        with override_settings(ATTENDANCE=dict(LIMITS, MAX_EXPIRY_MIN=7)):
            self.assertEqual(session_limits()["MAX_EXPIRY_MIN"], 7)
            with self.assertRaises(AttendanceError):
                self._create(minutes=8)


@override_settings(ATTENDANCE=LIMITS)
class RadiusCeilingTests(SessionLimitFixture):
    def test_the_ceiling_is_accepted_exactly(self):
        self.assertEqual(self._create(radius=50).radius_m, 50)

    def test_one_metre_over_is_refused(self):
        with self.assertRaises(AttendanceError) as caught:
            self._create(radius=51)
        self.assertEqual(caught.exception.code, "BAD_RADIUS")
        self.assertIn("between 10 and 50", caught.exception.message)

    def test_a_wide_fence_is_refused_rather_than_clamped(self):
        with self.assertRaises(AttendanceError):
            self._create(radius=5000)
        self.assertFalse(AttendanceSession.objects.exists())

    def test_below_the_floor_is_refused(self):
        with self.assertRaises(AttendanceError):
            self._create(radius=1)


@override_settings(ATTENDANCE=LIMITS)
class BypassTests(SessionLimitFixture):
    """The routes a browser might take around the front-end check."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.teacher)

    def _post(self, **overrides):
        data = {"batch": str(self.batch.id), "subject": str(self.subject.id),
                "latitude": "12.9", "longitude": "77.5", "accuracy": "10",
                "minutes": "5", "radius": "50", "notify": "0"}
        data.update(overrides)
        return self.client.post(reverse("attendance:api_session_create"), data,
                                HTTP_X_REQUESTED_WITH="XMLHttpRequest")

    def test_posting_past_the_ceiling_directly_is_refused(self):
        """Editing the number in devtools, or curling the endpoint."""
        response = self._post(minutes="240")
        self.assertFalse(response.json()["success"])
        self.assertFalse(AttendanceSession.objects.exists())

    def test_posting_a_wide_fence_directly_is_refused(self):
        response = self._post(radius="10000")
        self.assertFalse(response.json()["success"])
        self.assertFalse(AttendanceSession.objects.exists())

    def _extend(self, session, minutes):
        """
        Call the view directly rather than through `reverse()`.

        The URL converter insists on a 24-character ObjectId, which is right
        for MongoDB but means a URL cannot be built for a row this harness
        created. The view is the thing under test, not the routing.
        """
        from django.test import RequestFactory

        from attendance.views import api_session_action

        request = RequestFactory().post(
            "/x/", {"minutes": str(minutes)},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        request.user = self.teacher
        return api_session_action(request, pk=session.pk, action="extend")

    def test_extending_cannot_outrun_the_total_window(self):
        """
        The interesting bypass: "+5 minutes" clicked repeatedly. The cap is on
        the whole window measured from creation, not on one extension, so the
        clicks stop mattering once the ceiling is reached.
        """
        session = self._create(minutes=25)
        for _ in range(10):
            self._extend(session, 5)
        session.refresh_from_db()
        self.assertLessEqual(session.validity_minutes, 30)

    def test_a_single_huge_extension_is_refused(self):
        session = self._create(minutes=5)
        response = self._extend(session, 600)
        self.assertEqual(response.status_code, 400)
        session.refresh_from_db()
        self.assertEqual(session.validity_minutes, 5)

    def test_extending_a_maxed_out_link_says_so(self):
        session = self._create(minutes=30)
        response = self._extend(session, 5)
        self.assertEqual(response.status_code, 409)
        import json

        self.assertEqual(json.loads(response.content)["code"],
                         "MAX_VALIDITY_REACHED")

    def test_the_model_caps_the_window_even_if_a_caller_forgets(self):
        """
        The view checks first, but `extend()` is the last line of defence — any
        future caller inherits the cap rather than needing to remember it.
        """
        session = self._create(minutes=25)
        session.extend(500)
        self.assertLessEqual(session.validity_minutes, 30)


@override_settings(ATTENDANCE=LIMITS)
class SessionRowTests(SessionLimitFixture):
    def test_the_session_list_reports_what_each_link_was_set_to(self):
        self.client.force_login(self.teacher)
        self._create(minutes=12, radius=25)
        rows = self.client.get(reverse("attendance:api_sessions")).json()["data"]["rows"]
        self.assertEqual(rows[0]["validity_minutes"], 12)
        self.assertEqual(rows[0]["radius_m"], 25)

    def test_an_older_session_still_reports_its_own_looser_settings(self):
        """
        The column shows what the session used, not today's ceiling — a link
        created before the limit was tightened should read honestly rather than
        appear to have obeyed a rule that did not exist yet.
        """
        with override_settings(ATTENDANCE=dict(LIMITS, MAX_EXPIRY_MIN=180,
                                               MAX_RADIUS_M=500)):
            session = self._create(minutes=120, radius=400)
        self.client.force_login(self.teacher)
        rows = self.client.get(reverse("attendance:api_sessions")).json()["data"]["rows"]
        row = next(r for r in rows if str(r["id"]) == str(session.id))
        self.assertEqual(row["validity_minutes"], 120)
        self.assertEqual(row["radius_m"], 400)
