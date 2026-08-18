"""
Whether generating a link emails the class.

It used to, always. Now the teacher decides and the default is *not* to send:
the class is usually in the room, the link is on their dashboard either way,
and emailing thirty people something that expires in fifteen minutes is the
exception rather than the hourly routine.

The test that matters most is the one about the *absent* parameter. A caller
that simply does not mention `notify` used to email everybody, so silence meant
the louder outcome — which is the wrong way round for something irreversible.
"""
from django.core import mail
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

LIMITS = {
    "MIN_EXPIRY_MIN": 1, "MAX_EXPIRY_MIN": 30, "DEFAULT_EXPIRY_MIN": 5,
    "MIN_RADIUS_M": 10, "MAX_RADIUS_M": 50, "DEFAULT_RADIUS_M": 50,
    "MAX_GPS_ACCURACY_M": 100000,
}


@override_settings(ATTENDANCE=LIMITS)
class NotifyChoiceTests(TestCase):
    def setUp(self):
        self.institute = Institute.objects.create(name="I", code="I",
                                                  email="i@i.edu")
        self.dept = Department.objects.create(institute=self.institute,
                                              name="CSE", code="CSE")
        self.batch = Batch.objects.create(department=self.dept,
                                          label="2022-26", start_year=2022,
                                          end_year=2026)
        self.subject = Subject.objects.create(department=self.dept, code="DSA",
                                              name="Data Structures")
        self.teacher = User.objects.create_user(
            email="t@i.edu", password="Str0ngPass!23", role="TEACHER",
            institute=self.institute, department=self.dept,
            registration_completed=True)
        TeacherAssignment.objects.create(teacher=self.teacher,
                                         subject=self.subject,
                                         batch=self.batch, is_active=True)
        self.students = []
        for n in range(3):
            user = User.objects.create_user(
                email=f"s{n}@i.edu", password="Str0ngPass!23", role="STUDENT",
                institute=self.institute, department=self.dept,
                full_name=f"Student {n}", registration_completed=True,
                face_enrolled=True)
            profile = StudentProfile.objects.create(
                user=user, department=self.dept, batch=self.batch,
                class_roll=str(n))
            Enrollment.objects.create(student=profile, subject=self.subject,
                                      is_active=True)
            self.students.append(profile)
        self.client.force_login(self.teacher)
        mail.outbox = []

    def create(self, **overrides):
        data = {"batch": str(self.batch.id), "subject": str(self.subject.id),
                "latitude": "12.9", "longitude": "77.5", "accuracy": "10",
                "minutes": "5", "radius": "50"}
        data.update(overrides)
        return self.client.post(reverse("attendance:api_session_create"), data,
                                HTTP_X_REQUESTED_WITH="XMLHttpRequest")

    # --- the default ------------------------------------------------------- #
    def test_nobody_is_emailed_when_the_box_is_not_ticked(self):
        response = self.create(notify="0")
        self.assertTrue(response.json()["success"], response.content)
        self.assertEqual(mail.outbox, [])

    def test_nobody_is_emailed_when_the_parameter_is_absent(self):
        """
        The one that matters. Silence used to mean "email the whole class",
        so the only way to be quiet was to remember to ask for it.
        """
        self.create()
        self.assertEqual(mail.outbox, [])

    def test_the_answer_reports_that_none_were_emailed(self):
        data = self.create().json()["data"]
        self.assertEqual(data["emailed"], 0)

    # --- opting in --------------------------------------------------------- #
    def test_ticking_the_box_emails_every_enrolled_student(self):
        response = self.create(notify="1")
        self.assertEqual(len(mail.outbox), 3)
        self.assertEqual(response.json()["data"]["emailed"], 3)
        self.assertEqual({m.to[0] for m in mail.outbox},
                         {"s0@i.edu", "s1@i.edu", "s2@i.edu"})

    def test_the_email_carries_the_mark_link(self):
        self.create(notify="1")
        session = AttendanceSession.objects.get()
        self.assertIn(session.token, mail.outbox[0].body)

    def test_an_unregistered_student_is_skipped_either_way(self):
        """Unchanged behaviour, asserted so the change did not disturb it."""
        self.students[0].user.registration_completed = False
        self.students[0].user.save()
        self.create(notify="1")
        self.assertEqual(len(mail.outbox), 2)

    # --- the link is not withheld ------------------------------------------ #
    def test_the_student_still_sees_the_link_on_their_dashboard(self):
        """
        The premise of the whole change. If turning email off hid the link,
        this would be a way of quietly excluding a class rather than a way of
        sending less mail.
        """
        self.create(notify="0")
        session = AttendanceSession.objects.get()
        self.client.force_login(self.students[0].user)
        data = self.client.get(
            reverse("dashboard:api_my_summary"),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest").json()["data"]
        tokens = [s["url"] for s in data["open_sessions"]]
        self.assertIn(f"/attendance/mark/{session.token}/", tokens)

    def test_the_teacher_can_still_send_it_afterwards(self):
        """Changing your mind is a click, not a new session."""
        self.create(notify="0")
        session = AttendanceSession.objects.get()
        response = self.client.post(
            reverse("attendance:api_session_action",
                    args=[session.id, "resend"]),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertTrue(response.json()["success"], response.content)
        self.assertEqual(len(mail.outbox), 3)

    # --- the record -------------------------------------------------------- #
    def test_the_activity_log_says_which_it_was(self):
        """
        Two sessions, then both log lines read at once — rather than "the
        latest", which on this model means ordering by a timestamp two writes
        in the same test can share.
        """
        from accounts.models import ActivityLog

        self.create(notify="0")
        self.create(notify="1")
        details = list(ActivityLog.objects.filter(
            action="SESSION_CREATED").values_list("detail", flat=True))
        self.assertEqual(len(details), 2)
        self.assertTrue(any("no email" in d for d in details), details)
        self.assertTrue(any(d.endswith("emailed") for d in details), details)

    def test_the_session_is_created_regardless(self):
        for choice in ("0", "1"):
            AttendanceSession.objects.all().delete()
            self.create(notify=choice)
            session = AttendanceSession.objects.get()
            self.assertEqual(session.status, AttendanceSession.Status.OPEN)
            self.assertGreater(session.expires_at, timezone.now())
