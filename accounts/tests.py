import re

from django.core import mail
from django.test import TestCase
from django.urls import reverse

from accounts.models import EmailOTP, Institute, Invitation, User


class InstituteSignupTests(TestCase):
    payload = {
        "institute_name": "Test College", "institute_code": "TC",
        "institute_email": "office@tc.edu", "head_name": "Dr. Head",
        "head_email": "head@tc.edu", "password1": "Str0ngPass!23",
        "password2": "Str0ngPass!23",
    }

    def test_signup_requires_otp_and_creates_institute(self):
        res = self.client.post(reverse("accounts:api_signup_start"), self.payload)
        self.assertTrue(res.json()["success"])
        self.assertFalse(Institute.objects.exists())          # nothing yet
        self.assertEqual(len(mail.outbox), 1)

        otp = EmailOTP.objects.get(email="head@tc.edu")  # noqa: F841
        bad = self.client.post(reverse("accounts:api_signup_verify"), {"code": "000000"})
        self.assertEqual(bad.status_code, 400)
        self.assertFalse(Institute.objects.exists())
        otp.refresh_from_db()

        code = re.search(r"\b(\d{6})\b", mail.outbox[0].subject).group(1)
        self.assertEqual(otp.attempts, 1)                     # the bad try was counted
        good = self.client.post(reverse("accounts:api_signup_verify"), {"code": code})
        self.assertTrue(good.json()["success"])
        self.assertTrue(Institute.objects.filter(code="TC").exists())
        head = User.objects.get(email="head@tc.edu")
        self.assertEqual(head.role, User.Role.HEAD)
        self.assertTrue(head.registration_completed)

    def test_duplicate_email_rejected(self):
        User.objects.create_user(email="head@tc.edu", password="x", role="HEAD")
        res = self.client.post(reverse("accounts:api_signup_start"), self.payload)
        self.assertEqual(res.status_code, 400)
        self.assertIn("head_email", res.json()["errors"])


class InvitationTests(TestCase):
    def setUp(self):
        self.institute = Institute.objects.create(name="I", code="I", email="i@i.edu")

    def _invite(self, email="hod@i.edu", role="HOD"):
        from accounts.services import invite_user
        user, inv, _ = invite_user(email=email, role=role, institute=self.institute)
        return user, inv

    def test_only_invited_email_can_register(self):
        res = self.client.post(
            reverse("accounts:api_invite_accept", args=["not-a-real-token"]),
            {"full_name": "X", "password1": "Str0ngPass!23", "password2": "Str0ngPass!23"},
        )
        self.assertEqual(res.status_code, 404)

    def test_accepting_invite_activates_account(self):
        user, inv = self._invite()
        self.assertFalse(user.registration_completed)
        res = self.client.post(
            reverse("accounts:api_invite_accept", args=[inv.token]),
            {"full_name": "Prof X", "phone": "999", "password1": "Str0ngPass!23",
             "password2": "Str0ngPass!23"},
        )
        self.assertTrue(res.json()["success"])
        user.refresh_from_db()
        inv.refresh_from_db()
        self.assertTrue(user.registration_completed)
        self.assertTrue(user.check_password("Str0ngPass!23"))
        self.assertEqual(inv.status, Invitation.Status.ACCEPTED)

    def test_invite_cannot_be_reused(self):
        _, inv = self._invite()
        data = {"full_name": "A", "password1": "Str0ngPass!23", "password2": "Str0ngPass!23"}
        self.client.post(reverse("accounts:api_invite_accept", args=[inv.token]), data)
        self.client.logout()
        again = self.client.post(reverse("accounts:api_invite_accept", args=[inv.token]), data)
        self.assertEqual(again.status_code, 409)

    def test_revoked_invite_blocked(self):
        _, inv = self._invite()
        inv.status = Invitation.Status.REVOKED
        inv.save()
        res = self.client.post(
            reverse("accounts:api_invite_accept", args=[inv.token]),
            {"full_name": "A", "password1": "Str0ngPass!23", "password2": "Str0ngPass!23"},
        )
        self.assertEqual(res.status_code, 403)


class LoginTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="a@b.edu", password="Str0ngPass!23", role="TEACHER",
            registration_completed=True,
        )

    def test_login_ok(self):
        res = self.client.post(reverse("accounts:api_login"),
                               {"email": "A@B.edu", "password": "Str0ngPass!23"})
        self.assertTrue(res.json()["success"])

    def test_login_bad_password(self):
        res = self.client.post(reverse("accounts:api_login"),
                               {"email": "a@b.edu", "password": "nope"})
        self.assertEqual(res.status_code, 400)

    def test_inactive_invitee_cannot_login(self):
        User.objects.create_user(email="c@d.edu", password=None, role="STUDENT")
        res = self.client.post(reverse("accounts:api_login"),
                               {"email": "c@d.edu", "password": "anything"})
        self.assertEqual(res.status_code, 400)


class PruneInvitationsTests(TestCase):
    """The clean-up command must remove dead links without touching live data."""

    def setUp(self):
        from academics.models import Department

        self.institute = Institute.objects.create(name="I", code="I", email="i@i.edu")
        self.dept = Department.objects.create(institute=self.institute, name="CSE", code="CSE")

    def _invite(self, email, *, expired_days=None, status=None, role="TEACHER"):
        """Create an invitation whose expiry sits `expired_days` in the past."""
        import datetime as dt

        from accounts.services import invite_user
        from django.utils import timezone

        user, inv, _ = invite_user(email=email, role=role, institute=self.institute,
                                   department=self.dept, send=False)
        if expired_days is not None:
            inv.expires_at = timezone.now() - dt.timedelta(days=expired_days)
            inv.save(update_fields=["expires_at"])
        if status:
            inv.status = status
            inv.save(update_fields=["status"])
        return user, inv

    def _run(self, **kwargs):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command("prune_invitations", stdout=out, **kwargs)
        return out.getvalue()

    def test_dry_run_changes_nothing(self):
        self._invite("old@i.edu", expired_days=60)
        self._run(days=30, dry_run=True)
        self.assertEqual(Invitation.objects.count(), 1)
        self.assertEqual(Invitation.objects.first().status, Invitation.Status.PENDING)

    def test_deletes_only_links_older_than_n_days(self):
        self._invite("old@i.edu", expired_days=60)
        self._invite("recent@i.edu", expired_days=5)      # expired, but not old enough
        self._invite("live@i.edu")                        # still valid
        self._run(days=30)
        remaining = set(Invitation.objects.values_list("email", flat=True))
        self.assertEqual(remaining, {"recent@i.edu", "live@i.edu"})

    def test_accepted_invitations_are_never_deleted(self):
        user, inv = self._invite("joined@i.edu", expired_days=365)
        inv.accept(user)
        self._run(days=30, include_revoked=True)
        self.assertTrue(Invitation.objects.filter(email="joined@i.edu").exists())

    def test_revoked_only_deleted_with_flag(self):
        self._invite("revoked@i.edu", expired_days=60, status=Invitation.Status.REVOKED)
        self._run(days=30)
        self.assertTrue(Invitation.objects.filter(email="revoked@i.edu").exists())
        self._run(days=30, include_revoked=True)
        self.assertFalse(Invitation.objects.filter(email="revoked@i.edu").exists())

    def test_stale_pending_rows_are_flagged_expired(self):
        self._invite("stale@i.edu", expired_days=2)       # expired, too recent to delete
        self._run(days=30)
        self.assertEqual(Invitation.objects.get(email="stale@i.edu").status,
                         Invitation.Status.EXPIRED)

    def test_institute_scope(self):
        other = Institute.objects.create(name="J", code="J", email="j@j.edu")
        from accounts.services import invite_user
        import datetime as dt
        from django.utils import timezone
        _, inv, _ = invite_user(email="x@j.edu", role="HOD", institute=other, send=False)
        inv.expires_at = timezone.now() - dt.timedelta(days=90)
        inv.save(update_fields=["expires_at"])
        self._invite("y@i.edu", expired_days=90)
        self._run(days=30, institute="I")
        self.assertTrue(Invitation.objects.filter(email="x@j.edu").exists())
        self.assertFalse(Invitation.objects.filter(email="y@i.edu").exists())

    def test_orphan_user_removed_only_with_purge_flag(self):
        self._invite("ghost@i.edu", expired_days=60)
        self._run(days=30)
        self.assertTrue(User.objects.filter(email="ghost@i.edu").exists())

        self._invite("ghost2@i.edu", expired_days=60)
        self._run(days=30, purge_users=True, yes=True)
        self.assertFalse(User.objects.filter(email="ghost2@i.edu").exists())

    def test_purge_refuses_users_holding_academic_data(self):
        """A teacher who never registered but has allocations must survive."""
        from academics.models import Batch, Subject, TeacherAssignment

        user, _ = self._invite("teacher@i.edu", expired_days=60)
        batch = Batch.objects.create(department=self.dept, label="2022-26",
                                     start_year=2022, end_year=2026)
        subject = Subject.objects.create(department=self.dept, code="DSA", name="DS")
        TeacherAssignment.objects.create(teacher=user, subject=subject, batch=batch)

        self._run(days=30, purge_users=True, yes=True)
        self.assertTrue(User.objects.filter(email="teacher@i.edu").exists())
        self.assertTrue(TeacherAssignment.objects.filter(teacher=user).exists())
        self.assertFalse(Invitation.objects.filter(email="teacher@i.edu").exists())

    def test_purge_refuses_students_with_attendance(self):
        from academics.models import Batch, Enrollment, StudentProfile, Subject

        user, _ = self._invite("stu@i.edu", expired_days=60, role="STUDENT")
        batch = Batch.objects.create(department=self.dept, label="2022-26",
                                     start_year=2022, end_year=2026)
        subject = Subject.objects.create(department=self.dept, code="DSA", name="DS")
        profile = StudentProfile.objects.create(user=user, department=self.dept, batch=batch)
        Enrollment.objects.create(student=profile, subject=subject)

        self._run(days=30, purge_users=True, yes=True)
        self.assertTrue(User.objects.filter(email="stu@i.edu").exists())
        self.assertTrue(StudentProfile.objects.filter(user=user).exists())

    def test_activated_users_are_never_purged(self):
        user, inv = self._invite("real@i.edu", expired_days=60)
        user.registration_completed = True
        user.set_password("Str0ngPass!23")
        user.save()
        self._run(days=30, purge_users=True, yes=True)
        self.assertTrue(User.objects.filter(email="real@i.edu").exists())
