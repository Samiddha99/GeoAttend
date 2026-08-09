"""
Guardian sign-in.

The bulk of this file is about what a guardian *cannot* do. That is deliberate:
the feature is a new authenticated role with no password, and the interesting
failure is not "the dashboard did not load" but "the read-only account changed
something". `GuardianWriteRefusalTests` walks every student write endpoint and
insists on a refusal from the server, not from the template.
"""
import datetime as dt

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from academics.models import Batch, Department, Enrollment, StudentProfile, Subject
from accounts.guardians import (
    account_for_number,
    acting_profile,
    children_for_number,
)
from accounts.models import ActivityLog, PhoneOTP, User
from accounts.models import Institute


class PhoneOTPTests(TestCase):
    """The code itself: how long it lives, how often it may be tried or resent."""

    def test_a_fresh_code_verifies_once(self):
        otp, code = PhoneOTP.issue("+919876543210")
        good, _ = otp.verify(code)
        self.assertTrue(good)
        # A second use fails: the code is spent, not merely correct.
        good, message = otp.verify(code)
        self.assertFalse(good)
        self.assertIn("already been used", message)

    def test_issuing_a_new_code_retires_the_old_one(self):
        """
        Otherwise two live codes exist for one number, and the older one keeps
        working after the guardian has asked for a replacement.
        """
        first, first_code = PhoneOTP.issue("+919876543210")
        PhoneOTP.issue("+919876543210")
        first.refresh_from_db()
        self.assertTrue(first.is_used)
        self.assertFalse(first.verify(first_code)[0])

    @override_settings(OTP_MAX_ATTEMPTS=3)
    def test_wrong_codes_run_out(self):
        otp, code = PhoneOTP.issue("+919876543210")
        for _ in range(3):
            self.assertFalse(otp.verify("000000")[0])
        # Even the right code is refused now — the lockout is on the record,
        # not on the guess.
        good, message = otp.verify(code)
        self.assertFalse(good)
        self.assertIn("Too many", message)

    def test_an_expired_code_is_refused(self):
        otp, code = PhoneOTP.issue("+919876543210")
        otp.expires_at = timezone.now() - dt.timedelta(seconds=1)
        otp.save(update_fields=["expires_at"])
        self.assertFalse(otp.verify(code)[0])

    @override_settings(PHONE_OTP_RESEND_SECONDS=60)
    def test_a_resend_is_held_for_the_cooldown(self):
        otp, _ = PhoneOTP.issue("+919876543210")
        code, error = otp.resend()
        self.assertIsNone(code)
        self.assertIn("wait", error)

    @override_settings(PHONE_OTP_RESEND_SECONDS=0, PHONE_OTP_MAX_SENDS=3)
    def test_resends_are_capped_so_a_number_cannot_be_flooded(self):
        """
        The ceiling lives on the OTP record rather than in the session, because
        the session belongs to whoever is doing the flooding.
        """
        otp, _ = PhoneOTP.issue("+919876543210")     # sends == 1
        self.assertIsNotNone(otp.resend()[0])        # 2
        self.assertIsNotNone(otp.resend()[0])        # 3
        code, error = otp.resend()
        self.assertIsNone(code)
        self.assertIn("Too many codes", error)

    @override_settings(PHONE_OTP_RESEND_SECONDS=0)
    def test_a_resend_clears_the_failed_attempts(self):
        """A new code deserves a fresh set of tries; the send ceiling still bites."""
        otp, _ = PhoneOTP.issue("+919876543210")
        otp.verify("000000")
        self.assertEqual(otp.attempts, 1)
        new_code, _ = otp.resend()
        self.assertEqual(otp.attempts, 0)
        self.assertTrue(otp.verify(new_code)[0])


# A well-formed ObjectId that matches no row. The URL converter insists on 24
# hex characters, and these endpoints must refuse a guardian before they ever
# look the id up — so "not found" would be the wrong answer, and a 403 the right
# one.
NOWHERE_ID = "0" * 24


class GuardianFixture(TestCase):
    """One institute, two siblings on one number, one unrelated student."""

    NUMBER = "+919876543210"

    def setUp(self):
        self.institute = Institute.objects.create(name="I", code="I", email="i@i.edu")
        self.dept = Department.objects.create(institute=self.institute,
                                              name="CSE", code="CSE")
        self.batch = Batch.objects.create(department=self.dept, label="2022-26",
                                          start_year=2022, end_year=2026)
        self.subject = Subject.objects.create(department=self.dept, code="DSA",
                                              name="Data Structures")
        self.older = self._student("older@i.edu", "Asha Roy", "01",
                                   guardian="98765 43210")
        self.younger = self._student("younger@i.edu", "Bela Roy", "02",
                                     guardian="+91 98765 43210")
        self.stranger = self._student("other@i.edu", "Chandra Sen", "03",
                                      guardian="9000000000")

    def _student(self, email, name, roll, guardian):
        user = User.objects.create_user(
            email=email, password="Str0ngPass!23", role="STUDENT",
            institute=self.institute, department=self.dept, full_name=name,
            registration_completed=True, face_enrolled=True)
        profile = StudentProfile.objects.create(
            user=user, department=self.dept, batch=self.batch, class_roll=roll,
            guardian_mobile=guardian, guardian_name="R. Roy")
        Enrollment.objects.create(student=profile, subject=self.subject,
                                  is_active=True)
        return profile

    def sign_in(self):
        """Log a guardian in the way the endpoints do, without WhatsApp."""
        guardian = account_for_number(self.NUMBER, institute=self.institute,
                                      name="R. Roy")
        self.client.force_login(guardian)
        return guardian


class ChildResolutionTests(GuardianFixture):
    def test_one_number_written_three_ways_finds_the_same_family(self):
        """
        "98765 43210", "+91 98765 43210" and "09876543210" are one number.
        Matching on the raw column would have made them three guardians.
        """
        children = list(children_for_number("09876543210"))
        self.assertEqual({c.id for c in children},
                         {self.older.id, self.younger.id})

    def test_a_stranger_s_number_reaches_only_their_own_child(self):
        children = list(children_for_number("9000000000"))
        self.assertEqual([c.id for c in children], [self.stranger.id])

    def test_an_unknown_number_reaches_nobody(self):
        self.assertEqual(children_for_number("+919999999999").count(), 0)

    def test_a_deactivated_student_drops_out(self):
        self.older.is_active = False
        self.older.save()
        self.assertEqual({c.id for c in children_for_number(self.NUMBER)},
                         {self.younger.id})

    def test_an_archived_batch_drops_out(self):
        self.batch.is_active = False
        self.batch.save()
        self.assertEqual(children_for_number(self.NUMBER).count(), 0)

    def test_an_unparseable_number_stores_blank_and_matches_nothing(self):
        """
        Blank means "no guardian can sign in against this student", which is
        the safe reading of a number we could not make sense of.
        """
        self.older.guardian_mobile = "not a phone"
        self.older.save()
        self.older.refresh_from_db()
        self.assertEqual(self.older.guardian_mobile_e164, "")
        self.assertEqual({c.id for c in children_for_number(self.NUMBER)},
                         {self.younger.id})


class GuardianAccountTests(GuardianFixture):
    def test_the_account_never_holds_a_usable_password(self):
        guardian = account_for_number(self.NUMBER, institute=self.institute)
        self.assertFalse(guardian.has_usable_password())
        self.assertEqual(guardian.role, User.Role.GUARDIAN)
        self.assertTrue(guardian.registration_completed)

    def test_one_number_is_one_account_however_many_children(self):
        first = account_for_number(self.NUMBER, institute=self.institute)
        second = account_for_number(self.NUMBER, institute=self.institute)
        self.assertEqual(first.pk, second.pk)

    def test_signing_in_with_a_password_is_impossible(self):
        """
        An unusable password, not a blank one: authenticate() can never succeed
        against this row whatever is posted.
        """
        from django.contrib.auth import authenticate

        guardian = account_for_number(self.NUMBER, institute=self.institute)
        for attempt in ("", " ", "password", "!"):
            self.assertIsNone(authenticate(username=guardian.email,
                                           password=attempt))


class GuardianLoginEndpointTests(GuardianFixture):
    def test_an_unknown_number_gets_the_same_answer_as_a_known_one(self):
        """
        Otherwise the endpoint is a lookup service: try numbers, learn which
        families attend this institute.
        """
        known = self.client.post(reverse("accounts:api_guardian_start"),
                                 {"mobile": "9876543210"})
        unknown = self.client.post(reverse("accounts:api_guardian_start"),
                                   {"mobile": "9999999999"})
        self.assertEqual(known.status_code, unknown.status_code)
        self.assertEqual(known.json()["message"], unknown.json()["message"])

    def test_no_code_is_issued_for_an_unknown_number(self):
        """The reply is identical; the side effect is not."""
        self.client.post(reverse("accounts:api_guardian_start"),
                         {"mobile": "9999999999"})
        self.assertFalse(PhoneOTP.objects.filter(mobile="+919999999999").exists())

    def test_a_number_that_is_not_a_number_is_called_out(self):
        """A typo is not an enumeration attempt, and silence would strand them."""
        response = self.client.post(reverse("accounts:api_guardian_start"),
                                    {"mobile": "hello"})
        self.assertFalse(response.json()["success"])

    def test_verifying_without_starting_is_refused(self):
        response = self.client.post(reverse("accounts:api_guardian_verify"),
                                    {"code": "123456"})
        self.assertEqual(response.status_code, 410)

    def _verify(self):
        """Drive the real endpoint, rather than force_login()."""
        otp, code = PhoneOTP.issue(self.NUMBER)
        session = self.client.session
        session["guardian_otp_id"] = str(otp.id)
        session.save()
        return self.client.post(reverse("accounts:api_guardian_verify"),
                                {"code": code})

    @override_settings(AUTHENTICATION_BACKENDS=["accounts.backends.EmailBackend"])
    def test_the_session_survives_the_very_next_request(self):
        """
        The regression that `force_login` cannot catch.

        `login()` records which auth backend signed the user in, and on the
        next request `auth.get_user()` discards any backend not listed in
        AUTHENTICATION_BACKENDS — handing back an AnonymousUser. Naming a real
        but uninstalled backend therefore produces a sign-in that reports
        success and then bounces to the login page on the first click.

        `force_login` picks a configured backend for you, so every test that
        used it passed while the actual flow was broken. This one signs in for
        real and then asks for a page.
        """
        self.assertEqual(self._verify().status_code, 200)
        response = self.client.get(reverse("dashboard:home"))
        self.assertEqual(response.status_code, 200,
                         "signed in, then bounced — check the login() backend")

    @override_settings(AUTHENTICATION_BACKENDS=["accounts.backends.EmailBackend"])
    def test_the_recorded_backend_is_one_that_is_actually_installed(self):
        from django.conf import settings as live
        from django.contrib.auth import BACKEND_SESSION_KEY

        self._verify()
        self.assertIn(self.client.session[BACKEND_SESSION_KEY],
                      live.AUTHENTICATION_BACKENDS)

    def test_a_child_removed_between_code_and_verify_closes_the_door(self):
        """
        The children are re-read after the code checks out. A code must not be
        a key to a door that has since closed.
        """
        otp, code = PhoneOTP.issue(self.NUMBER)
        session = self.client.session
        session["guardian_otp_id"] = str(otp.id)
        session.save()

        StudentProfile.objects.filter(
            guardian_mobile_e164=self.NUMBER).update(guardian_mobile_e164="")

        response = self.client.post(reverse("accounts:api_guardian_verify"),
                                    {"code": code})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json().get("code"), "NO_CHILDREN")


class ChildSwitchingTests(GuardianFixture):
    def test_a_guardian_starts_on_one_child_and_can_move_to_the_sibling(self):
        guardian = self.sign_in()
        response = self.client.post(reverse("accounts:api_guardian_switch_child"),
                                    {"student": str(self.younger.id)})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.session["guardian_child_id"],
                         str(self.younger.id))

    def test_a_guardian_cannot_point_the_session_at_someone_else_s_child(self):
        self.sign_in()
        response = self.client.post(reverse("accounts:api_guardian_switch_child"),
                                    {"student": str(self.stranger.id)})
        self.assertEqual(response.status_code, 403)

    def test_a_forged_session_value_falls_back_rather_than_leaking(self):
        """
        The session records *which* child was chosen; whether that is still
        allowed is recomputed from the student table on every request.
        """
        self.sign_in()
        session = self.client.session
        session["guardian_child_id"] = str(self.stranger.id)
        session.save()
        self.client.get(reverse("dashboard:home"))
        # Resolved to one of their own, never to the stranger.
        self.assertIn(self.client.session["guardian_child_id"],
                      {str(self.older.id), str(self.younger.id)})


class GuardianReadAccessTests(GuardianFixture):
    """What a guardian is meant to be able to open."""

    PAGES = ["dashboard:home", "attendance:my_attendance",
             "attendance:my_absence_reasons", "feedback:my_feedback"]

    def test_the_student_screens_load(self):
        self.sign_in()
        for name in self.PAGES:
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)

    def test_the_dashboard_reports_the_child_s_figures(self):
        self.sign_in()
        response = self.client.get(reverse("dashboard:api_my_summary"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["student"]["name"],
                         self.older.name)

    def test_every_link_the_sidebar_offers_actually_opens(self):
        """
        The nav and the role gates have to agree, and nothing makes them.

        The Teachers link shipped in the guardian sidebar while
        `teachers_page` still listed only HEAD/HOD/TEACHER/STUDENT — a menu
        entry whose sole outcome was "403 Access denied". Rather than add one
        assertion per page and wait to forget the next one, this reads the
        rendered sidebar and follows everything in it.
        """
        import re

        self.sign_in()
        html = self.client.get(reverse("dashboard:home")).content.decode()
        # The sidebar is the nav partial; take every internal href on the page.
        links = sorted({h for h in re.findall(r'href="(/[^"#?]*)"', html)
                        if not h.startswith(("/static/", "/media/"))})
        self.assertTrue(links, "no links found — did the layout change?")

        broken = []
        for href in links:
            if "logout" in href:
                continue           # ends the session and breaks the loop
            status = self.client.get(href).status_code
            if status not in (200, 302):
                broken.append(f"{href} -> {status}")
        self.assertEqual(broken, [],
                         "sidebar offers links a guardian cannot open")

    def test_the_teacher_directory_opens_and_withholds_mobile_numbers(self):
        """
        A guardian reads this list exactly as a student does: no manage
        controls, and no personal mobile numbers. The reasons for withholding
        them do not change with who is doing the reading.
        """
        self.sign_in()
        self.assertEqual(self.client.get(reverse("academics:teachers")).status_code,
                         200)
        rows = self.client.get(reverse("academics:api_teachers")).json()["data"]["rows"]
        for row in rows:
            with self.subTest(teacher=row["email"]):
                self.assertEqual(row["phone"], "")
                self.assertIsNone(row["phone_dial"])
                self.assertFalse(row.get("can_edit"))

    def test_the_profile_page_is_closed_entirely(self):
        """
        Everything on it — password, email, device, face — is a setting a
        guardian does not have. A page of inert controls is worse than no page.
        """
        self.sign_in()
        response = self.client.get(reverse("accounts:profile"))
        self.assertEqual(response.status_code, 302)

    def test_staff_screens_stay_shut(self):
        self.sign_in()
        for name in ("academics:students", "attendance:sessions",
                     "dashboard:reports", "feedback:feedback"):
            with self.subTest(page=name):
                self.assertIn(self.client.get(reverse(name)).status_code,
                              (302, 403))


class GuardianWriteRefusalTests(GuardianFixture):
    """
    Every write a student can make, refused for a guardian by the server.

    Hiding a button is presentation. These assert the endpoint itself says no.
    """

    def setUp(self):
        super().setUp()
        self.sign_in()

    def _post(self, url, data=None):
        return self.client.post(url, data or {},
                                HTTP_X_REQUESTED_WITH="XMLHttpRequest")

    def test_cannot_mark_attendance(self):
        from attendance.models import AttendanceSession

        teacher = User.objects.create_user(
            email="t@i.edu", password="Str0ngPass!23", role="TEACHER",
            institute=self.institute, department=self.dept,
            registration_completed=True)
        session = AttendanceSession.objects.create(
            teacher=teacher, subject=self.subject, batch=self.batch,
            latitude=0, longitude=0, radius_m=50,
            expires_at=timezone.now() + dt.timedelta(minutes=10))
        for name in ("attendance:api_mark", "attendance:api_mark_start"):
            with self.subTest(endpoint=name):
                response = self._post(reverse(name, args=[session.token]))
                self.assertEqual(response.status_code, 403)

    def test_cannot_submit_an_absence_reason(self):
        # A syntactically valid id that matches nothing. The role gate fires
        # before any lookup, so a 403 here proves the gate rather than a 404.
        response = self._post(
            reverse("attendance:api_absence_reason_submit", args=[NOWHERE_ID]),
            {"reason": "was ill"})
        self.assertEqual(response.status_code, 403)

    def test_cannot_file_or_cancel_a_planned_absence(self):
        self.assertEqual(
            self._post(reverse("attendance:api_planned_absence_submit"),
                       {"from_date": "2026-09-01", "to_date": "2026-09-02",
                        "reason": "wedding"}).status_code, 403)

    def test_cannot_submit_feedback_as_their_child(self):
        response = self._post(reverse("feedback:api_submit", args=[NOWHERE_ID]))
        self.assertEqual(response.status_code, 403)

    def test_cannot_set_a_password(self):
        response = self._post(reverse("accounts:api_change_password"),
                              {"old_password": "x", "new_password1": "Str0ngPass!23",
                               "new_password2": "Str0ngPass!23"})
        self.assertEqual(response.status_code, 403)

    def test_cannot_edit_a_profile_or_unbind_a_device_or_enrol_a_face(self):
        for name in ("accounts:api_profile_update", "accounts:api_reset_device",
                     "accounts:api_face_enrol"):
            with self.subTest(endpoint=name):
                self.assertEqual(self._post(reverse(name)).status_code, 403)

    def test_a_password_reset_cannot_be_started_for_a_guardian(self):
        """
        Their email is synthetic and undeliverable, so a reset could never
        complete — but no code should be minted for one either.
        """
        guardian = User.objects.get(guardian_mobile=self.NUMBER)
        self.client.logout()
        self.client.post(reverse("accounts:api_forgot_start"),
                         {"email": guardian.email})
        from accounts.models import EmailOTP

        self.assertFalse(EmailOTP.objects.filter(email=guardian.email).exists())
