"""
Staff-controlled release of a student's device binding.

The binding is what stops one handset marking attendance for several students,
so who may release it — and whether it leaves a trace — matters.
"""
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from academics.models import Batch, Department, Enrollment, StudentProfile, Subject, TeacherAssignment
from accounts.models import ActivityLog, Institute, User

PW = "Str0ngPass!23"


def allow_self_reset(value):
    from django.conf import settings

    return override_settings(
        ATTENDANCE={**settings.ATTENDANCE, "ALLOW_STUDENT_SELF_DEVICE_RESET": value})


class DeviceUnlinkBase(TestCase):
    def setUp(self):
        self.institute = Institute.objects.create(name="I", code="I", email="i@i.edu")
        self.dept = Department.objects.create(institute=self.institute, name="CSE", code="CSE")
        self.other_dept = Department.objects.create(institute=self.institute, name="ECE", code="ECE")
        self.batch = Batch.objects.create(department=self.dept, label="2022-26",
                                          start_year=2022, end_year=2026)
        self.dsa = Subject.objects.create(department=self.dept, code="DSA", name="Data Structures")

        self.head = self._user("head@i.edu", "HEAD", self.dept)
        self.hod = self._user("hod@i.edu", "HOD", self.dept)
        self.dept.hod = self.hod
        self.dept.save()
        self.teacher = self._user("t@i.edu", "TEACHER", self.dept)
        TeacherAssignment.objects.create(teacher=self.teacher, subject=self.dsa, batch=self.batch)
        self.outsider = self._user("t2@i.edu", "TEACHER", self.other_dept)

        self.student = self._student("s@i.edu", "Ana Sharma")
        self.student.user.device_id = "phone-abc"
        self.student.user.device_bound_at = timezone.now()
        self.student.user.save()

    def _user(self, email, role, dept):
        return User.objects.create_user(
            email=email, password=PW, full_name=email.split("@")[0].title(), role=role,
            institute=self.institute, department=dept, registration_completed=True)

    def _student(self, email, name, roll=None):
        user = User.objects.create_user(
            email=email, password=PW, full_name=name, role="STUDENT",
            institute=self.institute, department=self.dept, registration_completed=True)
        profile = StudentProfile.objects.create(
            user=user, department=self.dept, batch=self.batch,
            class_roll=roll or email.split("@")[0].upper(),
            guardian_mobile="+919812345671")
        Enrollment.objects.create(student=profile, subject=self.dsa)
        return profile

    def url(self, student=None):
        return reverse("academics:api_student_reset_device",
                       args=[(student or self.student).id])


class StaffUnlinkTests(DeviceUnlinkBase):
    def test_head_hod_and_teacher_can_all_unlink(self):
        for actor in (self.head, self.hod, self.teacher):
            self.student.user.device_id = "phone-abc"
            self.student.user.device_bound_at = timezone.now()
            self.student.user.save()

            self.client.force_login(actor)
            response = self.client.post(self.url(), {"reason": "lost phone"})
            self.assertTrue(response.json()["success"], actor.role)
            self.student.user.refresh_from_db()
            self.assertEqual(self.student.user.device_id, "", actor.role)
            self.assertIsNone(self.student.user.device_bound_at, actor.role)

    def test_a_teacher_outside_the_students_subjects_cannot(self):
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.post(self.url()).status_code, 404)
        self.student.user.refresh_from_db()
        self.assertEqual(self.student.user.device_id, "phone-abc")

    def test_students_cannot_unlink_each_other(self):
        other = self._student("s2@i.edu", "Other Student")
        self.client.force_login(other.user)
        self.assertEqual(self.client.post(self.url()).status_code, 403)

    def test_unlinking_twice_is_reported_not_silently_ok(self):
        self.client.force_login(self.hod)
        self.assertTrue(self.client.post(self.url()).json()["success"])
        second = self.client.post(self.url())
        self.assertEqual(second.status_code, 400)
        self.assertIn("no device linked", second.json()["message"])

    def test_the_action_is_logged_with_actor_and_reason(self):
        self.client.force_login(self.teacher)
        self.client.post(self.url(), {"reason": "handset stolen"})
        entry = ActivityLog.objects.filter(action="DEVICE_UNLINKED").first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.actor, self.teacher)
        self.assertIn("s@i.edu", entry.detail)
        self.assertIn("handset stolen", entry.detail)

    def test_the_student_is_emailed(self):
        self.client.force_login(self.hod)
        self.client.post(self.url(), {"reason": "lost phone"})
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["s@i.edu"])
        self.assertIn("unlinked", message.subject.lower())
        self.assertIn("Hod", message.body)             # who did it
        self.assertIn("lost phone", message.body)

    def test_a_never_activated_student_is_not_emailed(self):
        self.student.user.registration_completed = False
        self.student.user.save()
        self.client.force_login(self.hod)
        self.client.post(self.url())
        self.assertEqual(len(mail.outbox), 0)

    def test_the_list_reports_device_state(self):
        self.client.force_login(self.teacher)
        rows = self.client.get(reverse("academics:api_students")).json()["data"]["rows"]
        row = next(r for r in rows if r["email"] == "s@i.edu")
        self.assertTrue(row["device_bound"])
        self.assertTrue(row["device_bound_at"])

        self.client.post(self.url())
        rows = self.client.get(reverse("academics:api_students")).json()["data"]["rows"]
        row = next(r for r in rows if r["email"] == "s@i.edu")
        self.assertFalse(row["device_bound"])
        self.assertEqual(row["device_bound_at"], "")


class SelfServiceTests(DeviceUnlinkBase):
    @allow_self_reset(False)
    def test_students_cannot_reset_their_own_by_default(self):
        """Otherwise the one-device rule could be sidestepped by anyone, any time."""
        self.client.force_login(self.student.user)
        response = self.client.post(reverse("accounts:api_reset_device"))
        self.assertEqual(response.status_code, 403)
        self.assertIn("Only your department", response.json()["message"])
        self.student.user.refresh_from_db()
        self.assertEqual(self.student.user.device_id, "phone-abc")

    @allow_self_reset(True)
    def test_the_setting_re_enables_self_service(self):
        self.client.force_login(self.student.user)
        self.assertTrue(self.client.post(reverse("accounts:api_reset_device")).json()["success"])
        self.student.user.refresh_from_db()
        self.assertEqual(self.student.user.device_id, "")

    @allow_self_reset(False)
    def test_staff_may_still_reset_their_own_device(self):
        self.teacher.device_id = "teacher-phone"
        self.teacher.device_bound_at = timezone.now()
        self.teacher.save()
        self.client.force_login(self.teacher)
        self.assertTrue(self.client.post(reverse("accounts:api_reset_device")).json()["success"])

    @allow_self_reset(False)
    def test_the_profile_page_explains_who_to_ask(self):
        self.client.force_login(self.student.user)
        body = self.client.get(reverse("accounts:profile")).content.decode()
        self.assertIn("Ask your teacher, HoD or department office", body)
        self.assertNotIn('id="reset-device"', body)


class RebindTests(DeviceUnlinkBase):
    def test_marking_after_an_unlink_binds_the_new_device(self):
        """The whole point: a new phone works once staff release the old one."""
        import datetime as dt

        from attendance.models import AttendanceRecord, AttendanceSession
        from attendance.services import AttendanceError, mark_attendance

        session = AttendanceSession.objects.create(
            teacher=self.teacher, subject=self.dsa, batch=self.batch,
            latitude=22.5726, longitude=88.3639, radius_m=50, expected_count=1,
            expires_at=timezone.now() + dt.timedelta(minutes=5))

        class Req:
            META = {"HTTP_USER_AGENT": "new-phone"}
            user = None

        request = Req()
        request.user = self.student.user

        # the old binding blocks the new handset
        with self.assertRaises(AttendanceError) as ctx:
            mark_attendance(request=request, session=session, latitude=22.5726,
                            longitude=88.3639, accuracy=5, client_hash="new-device")
        self.assertEqual(ctx.exception.code, "DEVICE_MISMATCH")

        self.client.force_login(self.hod)
        self.client.post(self.url(), {"reason": "lost phone"})
        self.student.user.refresh_from_db()
        request.user = self.student.user

        record, _ = mark_attendance(request=request, session=session, latitude=22.5726,
                                    longitude=88.3639, accuracy=5, client_hash="new-device")
        self.assertEqual(record.status, AttendanceRecord.Status.PRESENT)
        self.student.user.refresh_from_db()
        self.assertTrue(self.student.user.device_id)          # rebound to the new phone


# --------------------------------------------------------------------------- #
#  Login device lock
# --------------------------------------------------------------------------- #
def login_lock(value):
    from django.conf import settings

    return override_settings(
        ATTENDANCE={**settings.ATTENDANCE, "ENFORCE_LOGIN_DEVICE_LOCK": value})


class LoginDeviceLockTests(DeviceUnlinkBase):
    """Signing in is refused from anything but the registered device."""

    URL = "/auth/api/login/"

    def sign_in(self, user, ua="Android; Pixel-7", device="device-A"):
        from django.test import Client

        client = Client(HTTP_USER_AGENT=ua)
        response = client.post(self.URL, {
            "email": user.email, "password": PW, "device_hash": device})
        return client, response

    def setUp(self):
        super().setUp()
        self.student.user.device_id = ""
        self.student.user.device_bound_at = None
        self.student.user.save()

    @login_lock(True)
    def test_first_login_binds_the_device(self):
        _, response = self.sign_in(self.student.user)
        self.assertTrue(response.json()["success"])
        self.student.user.refresh_from_db()
        self.assertTrue(self.student.user.device_id)
        self.assertIsNotNone(self.student.user.device_bound_at)

    @login_lock(True)
    def test_same_device_signs_in_again(self):
        self.sign_in(self.student.user)
        _, response = self.sign_in(self.student.user)
        self.assertTrue(response.json()["success"])

    @login_lock(True)
    def test_a_different_device_is_refused(self):
        self.sign_in(self.student.user)
        _, response = self.sign_in(self.student.user, ua="iPhone; Safari", device="device-B")
        self.assertEqual(response.status_code, 403)
        body = response.json()
        self.assertEqual(body["code"], "DEVICE_MISMATCH")
        self.assertIn("registered to a different device", body["message"])

    @login_lock(True)
    def test_a_refused_login_creates_no_session(self):
        self.sign_in(self.student.user)
        client, _ = self.sign_in(self.student.user, ua="iPhone; Safari", device="device-B")
        self.assertEqual(client.get(reverse("dashboard:home")).status_code, 302)

    @login_lock(True)
    def test_a_different_browser_on_the_same_phone_is_also_refused(self):
        """The UA is part of the signature, so Chrome and Firefox differ."""
        self.sign_in(self.student.user, ua="Android; Chrome", device="device-A")
        _, response = self.sign_in(self.student.user, ua="Android; Firefox", device="device-A")
        self.assertEqual(response.status_code, 403)

    @login_lock(True)
    def test_the_block_is_logged_for_staff_to_see(self):
        self.sign_in(self.student.user)
        self.sign_in(self.student.user, ua="iPhone; Safari", device="device-B")
        entry = ActivityLog.objects.filter(action="LOGIN_DEVICE_BLOCKED").first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.actor, self.student.user)
        self.assertIn("unregistered device", entry.detail)

    @login_lock(True)
    def test_staff_are_never_device_locked(self):
        """A HoD works from a desktop, a laptop and a phone."""
        for actor in (self.head, self.hod, self.teacher):
            self.sign_in(actor, ua="Desktop; Chrome", device="pc")
            _, response = self.sign_in(actor, ua="iPhone; Safari", device="phone")
            self.assertTrue(response.json()["success"], actor.role)

    @login_lock(True)
    def test_unlinking_lets_the_new_phone_in(self):
        self.sign_in(self.student.user)
        _, blocked = self.sign_in(self.student.user, ua="iPhone; Safari", device="device-B")
        self.assertEqual(blocked.status_code, 403)

        self.client.force_login(self.teacher)
        self.client.post(self.url(), {"reason": "lost phone"})

        _, allowed = self.sign_in(self.student.user, ua="iPhone; Safari", device="device-B")
        self.assertTrue(allowed.json()["success"])
        self.student.user.refresh_from_db()
        self.assertTrue(self.student.user.device_id)      # rebound to the new phone

    @login_lock(False)
    def test_the_setting_turns_the_login_lock_off(self):
        self.sign_in(self.student.user)
        _, response = self.sign_in(self.student.user, ua="iPhone; Safari", device="device-B")
        self.assertTrue(response.json()["success"])

    @login_lock(True)
    def test_activating_an_invitation_binds_that_device(self):
        from accounts.services import invite_user
        from django.test import Client

        user, invitation, _ = invite_user(
            email="fresh@i.edu", role="STUDENT", institute=self.institute,
            department=self.dept, send=False)
        StudentProfile.objects.create(
            user=user, department=self.dept, batch=self.batch, class_roll="RF",
            guardian_mobile="+919812345671")

        client = Client(HTTP_USER_AGENT="Android; Pixel-7")
        response = client.post(
            reverse("accounts:api_invite_accept", args=[invitation.token]),
            {"full_name": "Fresh Student", "password1": PW, "password2": PW,
             "device_hash": "activation-device"})
        self.assertTrue(response.json()["success"])
        user.refresh_from_db()
        self.assertTrue(user.device_id)

        client2 = Client(HTTP_USER_AGENT="iPhone; Safari")
        blocked = client2.post(self.URL, {
            "email": "fresh@i.edu", "password": PW, "device_hash": "other-device"})
        self.assertEqual(blocked.status_code, 403)


class TeacherDirectoryTests(TestCase):
    """
    Teachers may *see* the Teachers panel; they may not manage it.

    The template hides the controls, but that is cosmetic — what matters is
    that every write endpoint refuses a teacher on its own, so posting directly
    achieves nothing.
    """

    def setUp(self):
        from academics.models import Batch, Department, Subject, TeacherAssignment
        from accounts.models import Institute, User

        self.institute = Institute.objects.create(name="I", code="I", email="i@i.edu")
        self.cse = Department.objects.create(institute=self.institute, name="CSE", code="CSE")
        self.ece = Department.objects.create(institute=self.institute, name="ECE", code="ECE")
        self.dsa = Subject.objects.create(department=self.cse, code="DSA", name="Data Structures")
        self.batch = Batch.objects.create(department=self.cse, label="2022-26",
                                          start_year=2022, end_year=2026)

        def teacher(email, dept, phone=""):
            return User.objects.create_user(
                email=email, password="Str0ngPass!23", role="TEACHER",
                institute=self.institute, department=dept, phone=phone,
                registration_completed=True, full_name=email.split("@")[0].title())

        self.me = teacher("me@i.edu", self.cse, "9876543210")
        self.peer = teacher("peer@i.edu", self.cse, "9876500011")
        self.outsider = teacher("out@i.edu", self.ece)
        TeacherAssignment.objects.create(teacher=self.peer, subject=self.dsa, batch=self.batch)

        self.hod = User.objects.create_user(
            email="hod@i.edu", password="Str0ngPass!23", role="HOD",
            institute=self.institute, department=self.cse, registration_completed=True)

    def _login(self, user):
        c = self.client_class()
        c.force_login(user)
        return c

    # ------------------------------------------------------------- read
    def test_a_teacher_can_open_the_teachers_page(self):
        res = self._login(self.me).get(reverse("academics:teachers"))
        self.assertEqual(res.status_code, 200)

    def test_a_teacher_sees_every_teacher_in_the_institute(self):
        """Read scope is institute-wide; editing is what stays departmental."""
        res = self._login(self.me).get(reverse("academics:api_teachers"))
        self.assertEqual(res.status_code, 200)
        emails = {r["email"] for r in res.json()["data"]["rows"]}
        self.assertEqual(emails, {"me@i.edu", "peer@i.edu", "out@i.edu"})

    def test_a_teacher_may_edit_nobody(self):
        rows = self._login(self.me).get(reverse("academics:api_teachers")).json()["data"]["rows"]
        self.assertTrue(all(r["can_edit"] is False for r in rows))

    def test_the_mobile_number_is_returned_ready_to_dial(self):
        res = self._login(self.hod).get(reverse("academics:api_teachers"))
        row = next(r for r in res.json()["data"]["rows"] if r["email"] == "me@i.edu")
        self.assertEqual(row["phone"], "9876543210")
        self.assertTrue(row["phone_dial"]["tel"].endswith("9876543210"))
        self.assertTrue(row["phone_dial"]["wa"])       # wa.me form, no '+'

    def test_teachers_are_not_given_invitation_ids(self):
        """They cannot resend, so the id is nothing but leaked detail."""
        rows = self._login(self.me).get(reverse("academics:api_teachers")).json()["data"]["rows"]
        self.assertTrue(all(r["invitation_id"] is None for r in rows))

    # ------------------------------------------------------------ write
    # These call the view directly rather than going through reverse(). The
    # role guard is the thing under test, and calling it straight keeps the
    # test independent of URL routing (and of primary-key shape).
    def _call(self, view, user, pk=None, **payload):
        from django.test import RequestFactory

        request = RequestFactory().post("/x/", payload or {})
        request.user = user
        return view(request, pk=pk) if pk is not None else view(request)

    def test_every_write_endpoint_refuses_a_teacher(self):
        from django.core.exceptions import PermissionDenied

        from academics import views

        attempts = [
            ("api_teacher_invite", None, {"email": "x@i.edu", "assignments": "[]"}),
            ("api_teacher_assignments_save", self.peer.pk, {"assignments": "[]"}),
            ("api_teacher_toggle", self.peer.pk, {}),
        ]
        for name, pk, payload in attempts:
            with self.subTest(endpoint=name):
                with self.assertRaises(PermissionDenied,
                                       msg=f"{name} let a teacher through"):
                    self._call(getattr(views, name), self.me, pk=pk, **payload)
        self.peer.refresh_from_db()
        self.assertTrue(self.peer.is_active)           # nothing actually changed

    def test_the_hod_can_still_manage(self):
        from academics import views

        res = self._call(views.api_teacher_toggle, self.hod, pk=self.peer.pk)
        self.assertEqual(res.status_code, 200)
        self.peer.refresh_from_db()
        self.assertFalse(self.peer.is_active)


class TeacherEditScopeTests(TestCase):
    """
    Who may change what:
      head    — any teacher in the institute, including moving departments
      HoD     — only teachers in their own department, never the department
      teacher — nothing
    """

    def setUp(self):
        from academics.models import Batch, Department, Subject, TeacherAssignment
        from accounts.models import Institute, User

        self.institute = Institute.objects.create(name="I", code="I", email="i@i.edu")
        self.cse = Department.objects.create(institute=self.institute, name="CSE", code="CSE")
        self.ece = Department.objects.create(institute=self.institute, name="ECE", code="ECE")
        self.dsa = Subject.objects.create(department=self.cse, code="DSA", name="Data Structures")
        self.batch = Batch.objects.create(department=self.cse, label="2022-26",
                                          start_year=2022, end_year=2026)

        def user(email, role, dept):
            return User.objects.create_user(
                email=email, password="Str0ngPass!23", role=role, institute=self.institute,
                department=dept, registration_completed=True, full_name="Old Name")

        self.head = user("head@i.edu", "HEAD", None)
        self.cse_hod = user("cse-hod@i.edu", "HOD", self.cse)
        self.ece_hod = user("ece-hod@i.edu", "HOD", self.ece)
        self.teacher = user("t@i.edu", "TEACHER", self.cse)
        self.plain = user("plain@i.edu", "TEACHER", self.cse)
        TeacherAssignment.objects.create(teacher=self.teacher, subject=self.dsa, batch=self.batch)

    def _save(self, actor, target, **payload):
        from django.test import RequestFactory

        from academics import views

        payload.setdefault("assignments", "[]")
        request = RequestFactory().post("/x/", payload)
        request.user = actor
        return views.api_teacher_assignments_save(request, pk=target.pk)

    # --------------------------------------------------------- name / mobile
    def test_hod_can_edit_name_and_mobile_in_their_department(self):
        res = self._save(self.cse_hod, self.teacher,
                         full_name="New Name", phone="9876543210")
        self.assertEqual(res.status_code, 200)
        self.teacher.refresh_from_db()
        self.assertEqual(self.teacher.full_name, "New Name")
        self.assertTrue(self.teacher.phone.endswith("9876543210"))

    def test_an_invalid_mobile_is_rejected_not_stored(self):
        res = self._save(self.cse_hod, self.teacher, phone="12")
        self.assertEqual(res.status_code, 400)
        self.teacher.refresh_from_db()
        self.assertEqual(self.teacher.phone, "")

    # ------------------------------------------------------------- scope
    def test_a_hod_cannot_touch_another_departments_teacher(self):
        from django.http import Http404

        with self.assertRaises(Http404):
            self._save(self.ece_hod, self.teacher, full_name="Hijacked")
        self.teacher.refresh_from_db()
        self.assertEqual(self.teacher.full_name, "Old Name")

    def test_the_head_can_edit_any_teacher(self):
        res = self._save(self.head, self.teacher, full_name="Head Renamed")
        self.assertEqual(res.status_code, 200)
        self.teacher.refresh_from_db()
        self.assertEqual(self.teacher.full_name, "Head Renamed")

    # -------------------------------------------------------- department move
    def test_a_hod_cannot_move_a_teacher_to_another_department(self):
        res = self._save(self.cse_hod, self.teacher, department=str(self.ece.pk))
        self.assertEqual(res.status_code, 403)
        self.teacher.refresh_from_db()
        self.assertEqual(self.teacher.department_id, self.cse.pk)

    def test_the_head_can_move_a_teacher_and_old_allocations_retire(self):
        from academics.models import TeacherAssignment

        res = self._save(self.head, self.teacher, department=str(self.ece.pk))
        self.assertEqual(res.status_code, 200)
        self.teacher.refresh_from_db()
        self.assertEqual(self.teacher.department_id, self.ece.pk)
        # The DSA/2022-26 allocation belongs to CSE and cannot survive the move.
        self.assertFalse(
            TeacherAssignment.objects.filter(teacher=self.teacher, is_active=True).exists())

    def test_resubmitting_the_same_department_is_not_treated_as_a_move(self):
        res = self._save(self.cse_hod, self.teacher, department=str(self.cse.pk))
        self.assertEqual(res.status_code, 200)

    # ------------------------------------------------------------- teachers
    def test_a_teacher_cannot_edit_anyone(self):
        from django.core.exceptions import PermissionDenied

        with self.assertRaises(PermissionDenied):
            self._save(self.plain, self.teacher, full_name="Nope")
        self.teacher.refresh_from_db()
        self.assertEqual(self.teacher.full_name, "Old Name")


class CrossDepartmentVisibilityTests(TestCase):
    """
    Staff see every teacher in the institute; only their own department is
    editable. The `can_edit` flag drives the UI, but the write endpoints are
    what actually enforce it — so both are checked here.
    """

    def setUp(self):
        from academics.models import Department
        from accounts.models import Institute, User

        self.institute = Institute.objects.create(name="I", code="I", email="i@i.edu")
        self.cse = Department.objects.create(institute=self.institute, name="CSE", code="CSE")
        self.ece = Department.objects.create(institute=self.institute, name="ECE", code="ECE")

        def user(email, role, dept):
            return User.objects.create_user(
                email=email, password="Str0ngPass!23", role=role, institute=self.institute,
                department=dept, registration_completed=True, full_name="Name")

        self.head = user("head@i.edu", "HEAD", None)
        self.cse_hod = user("cse-hod@i.edu", "HOD", self.cse)
        self.cse_teacher = user("cse-t@i.edu", "TEACHER", self.cse)
        self.ece_teacher = user("ece-t@i.edu", "TEACHER", self.ece)

    def _rows(self, actor):
        c = self.client_class()
        c.force_login(actor)
        res = c.get(reverse("academics:api_teachers"))
        self.assertEqual(res.status_code, 200)
        return {r["email"]: r for r in res.json()["data"]["rows"]}

    def test_a_hod_sees_both_departments(self):
        rows = self._rows(self.cse_hod)
        self.assertEqual(set(rows), {"cse-t@i.edu", "ece-t@i.edu"})

    def test_a_hod_may_only_edit_their_own_department(self):
        rows = self._rows(self.cse_hod)
        self.assertTrue(rows["cse-t@i.edu"]["can_edit"])
        self.assertFalse(rows["ece-t@i.edu"]["can_edit"])

    def test_the_head_may_edit_everyone(self):
        rows = self._rows(self.head)
        self.assertTrue(all(r["can_edit"] for r in rows.values()))

    def test_can_edit_false_is_backed_by_the_server_refusing(self):
        """The flag is a hint; this is the guarantee behind it."""
        from django.http import Http404
        from django.test import RequestFactory

        from academics import views

        for view, payload in [(views.api_teacher_assignments_save, {"assignments": "[]"}),
                              (views.api_teacher_toggle, {})]:
            with self.subTest(view=view.__name__):
                request = RequestFactory().post("/x/", payload)
                request.user = self.cse_hod
                with self.assertRaises(Http404):
                    view(request, pk=self.ece_teacher.pk)
        self.ece_teacher.refresh_from_db()
        self.assertTrue(self.ece_teacher.is_active)

    def test_visible_and_manageable_scopes_are_different_sizes(self):
        from academics.selectors import teachers_for, visible_teachers_for

        self.assertEqual(visible_teachers_for(self.cse_hod).count(), 2)
        self.assertEqual(teachers_for(self.cse_hod).count(), 1)
        self.assertEqual(visible_teachers_for(self.cse_teacher).count(), 2)
        self.assertEqual(teachers_for(self.cse_teacher).count(), 0)


class StudentTeacherDirectoryTests(TestCase):
    """A student gets a directory: who teaches what. Nothing actionable."""

    def setUp(self):
        from academics.models import Batch, Department, Enrollment, StudentProfile, Subject, TeacherAssignment
        from accounts.models import Institute, User

        self.institute = Institute.objects.create(name="I", code="I", email="i@i.edu")
        self.cse = Department.objects.create(institute=self.institute, name="CSE", code="CSE")
        self.ece = Department.objects.create(institute=self.institute, name="ECE", code="ECE")
        self.dsa = Subject.objects.create(department=self.cse, code="DSA", name="Data Structures")
        self.batch = Batch.objects.create(department=self.cse, label="2022-26",
                                          start_year=2022, end_year=2026)

        def teacher(email, dept, active=True, registered=True):
            return User.objects.create_user(
                email=email, password="Str0ngPass!23", role="TEACHER",
                institute=self.institute, department=dept, phone="9876543210",
                is_active=active, registration_completed=registered, full_name="T")

        self.mine = teacher("mine@i.edu", self.cse)
        self.other_dept = teacher("other@i.edu", self.ece)
        self.dormant = teacher("gone@i.edu", self.cse, active=False)
        self.unclaimed = teacher("new@i.edu", self.cse, registered=False)
        TeacherAssignment.objects.create(teacher=self.mine, subject=self.dsa, batch=self.batch)

        self.student_user = User.objects.create_user(
            email="s@i.edu", password="Str0ngPass!23", role="STUDENT",
            institute=self.institute, department=self.cse, registration_completed=True)
        profile = StudentProfile.objects.create(
            user=self.student_user, department=self.cse, batch=self.batch, class_roll="01")
        Enrollment.objects.create(student=profile, subject=self.dsa)

    def _client(self):
        c = self.client_class()
        c.force_login(self.student_user)
        return c

    def test_a_student_can_open_the_page(self):
        self.assertEqual(self._client().get(reverse("academics:teachers")).status_code, 200)

    def test_a_student_sees_teachers_from_every_department(self):
        rows = self._client().get(reverse("academics:api_teachers")).json()["data"]["rows"]
        self.assertIn("other@i.edu", {r["email"] for r in rows})

    def test_dormant_and_unclaimed_accounts_are_hidden_from_students(self):
        """Listing a teacher who has left, or never signed up, is misleading."""
        emails = {r["email"] for r in
                  self._client().get(reverse("academics:api_teachers")).json()["data"]["rows"]}
        self.assertEqual(emails, {"mine@i.edu", "other@i.edu"})

    def test_a_student_may_edit_nobody(self):
        rows = self._client().get(reverse("academics:api_teachers")).json()["data"]["rows"]
        self.assertTrue(all(r["can_edit"] is False for r in rows))

    def test_staff_mobile_numbers_are_not_sent_to_students(self):
        """Hiding the column is not enough — the number must not be in the JSON."""
        rows = self._client().get(reverse("academics:api_teachers")).json()["data"]["rows"]
        self.assertTrue(all(r["phone"] == "" for r in rows))
        self.assertTrue(all(r["phone_dial"] is None for r in rows))

    def test_staff_still_receive_the_mobile(self):
        from accounts.models import User

        hod = User.objects.create_user(
            email="hod@i.edu", password="Str0ngPass!23", role="HOD",
            institute=self.institute, department=self.cse, registration_completed=True)
        c = self.client_class()
        c.force_login(hod)
        rows = c.get(reverse("academics:api_teachers")).json()["data"]["rows"]
        row = next(r for r in rows if r["email"] == "mine@i.edu")
        self.assertEqual(row["phone"], "9876543210")
        self.assertIsNotNone(row["phone_dial"])

    def test_a_student_cannot_reach_any_write_endpoint(self):
        from django.core.exceptions import PermissionDenied
        from django.test import RequestFactory

        from academics import views

        for view, pk in [(views.api_teacher_invite, None),
                         (views.api_teacher_assignments_save, self.mine.pk),
                         (views.api_teacher_toggle, self.mine.pk)]:
            with self.subTest(view=view.__name__):
                request = RequestFactory().post("/x/", {"assignments": "[]"})
                request.user = self.student_user
                with self.assertRaises(PermissionDenied):
                    view(request, pk=pk) if pk is not None else view(request)
