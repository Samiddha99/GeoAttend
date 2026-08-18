"""
Where notification email is addressed.

The rule: the login address, never the letterhead one. This has teeth because
`seed_universities` writes `<code>@unclaimed.invalid` as the official address of
every one of the 112 shipped universities — a domain reserved by RFC 2606 so
that it can never resolve. Addressing the approval queue there meant the mail
was generated, accepted, and thrown away, 112 times over, with nothing on
screen to say so.

These tests read `mail.outbox`, so they assert what a mail server would
actually receive rather than what the code appears to intend.
"""
from django.core import mail
from django.test import TestCase

from accounts.institute_approval import (
    approve_institute,
    reject_institute,
    request_institute_approval,
)
from accounts.models import (
    Discipline,
    Institute,
    InstituteAffiliation,
    University,
    UniversityDiscipline,
    User,
)
from accounts.recipients import institute_recipients, university_recipients


class Fixture(TestCase):
    def setUp(self):
        mail.outbox = []
        self.university = University.objects.create(
            name="Engineering University", code="ENGGU",
            email="registrar@unclaimed.invalid",     # as seeded
            grants_affiliation=True, is_seeded=True)
        UniversityDiscipline.objects.create(university=self.university,
                                            discipline=Discipline.ENGG)
        self.admin = User.objects.create_user(
            email="enggu@university.geoattend.local", password="Str0ngPass!23",
            role=User.Role.UNIVERSITY, university=self.university,
            registration_completed=True)

        self.institute = Institute.objects.create(
            name="Acme College", code="ACME",
            email="office@acme.edu",                 # official, on the website
            state="Kerala", district="Ernakulam",
            status=Institute.Status.PENDING)
        InstituteAffiliation.objects.create(
            institute=self.institute, discipline=Discipline.ENGG,
            university=self.university)
        self.head = User.objects.create_user(
            email="principal@acme.edu", password="Str0ngPass!23",
            role=User.Role.HEAD, institute=self.institute,
            registration_completed=True)

    def sent_to(self):
        return [address for message in mail.outbox for address in message.to]


class UniversityAddressTests(Fixture):
    def test_the_approval_request_goes_to_the_login_not_the_letterhead(self):
        request_institute_approval(self.institute)
        self.assertEqual(self.sent_to(), ["enggu@university.geoattend.local"])
        self.assertNotIn("registrar@unclaimed.invalid", self.sent_to())

    def test_every_administrator_is_told_not_just_the_first(self):
        """
        A queue that only ever pings whoever registered first stalls the day
        that person leaves.
        """
        User.objects.create_user(
            email="deputy@university.geoattend.local", password="Str0ngPass!23",
            role=User.Role.UNIVERSITY, university=self.university,
            registration_completed=True)
        request_institute_approval(self.institute)
        self.assertEqual(sorted(self.sent_to()),
                         ["deputy@university.geoattend.local",
                          "enggu@university.geoattend.local"])

    def test_a_deactivated_administrator_is_not_emailed(self):
        self.admin.is_active = False
        self.admin.save()
        request_institute_approval(self.institute)
        self.assertEqual(self.sent_to(), [])

    def test_an_unreachable_official_address_is_never_used_as_a_fallback(self):
        """
        With no login left, the fallback would reach for `University.email` —
        but `.invalid` cannot receive mail, so sending there costs a send and
        earns a bounce. Better to send nothing and log it.
        """
        self.admin.delete()
        self.assertEqual(university_recipients(self.university), [])

    def test_a_real_official_address_is_used_when_there_is_no_login(self):
        """A real letterhead address beats nobody hearing at all."""
        self.admin.delete()
        self.university.email = "registrar@enggu.ac.in"
        self.university.save()
        self.assertEqual(university_recipients(self.university),
                         ["registrar@enggu.ac.in"])

    def test_the_fallback_can_be_refused_outright(self):
        self.admin.delete()
        self.university.email = "registrar@enggu.ac.in"
        self.university.save()
        self.assertEqual(
            university_recipients(self.university, fallback=False), [])


class InstituteAddressTests(Fixture):
    def test_approval_reaches_the_head_login_not_the_office_address(self):
        approve_institute(institute=self.institute, actor=self.admin)
        self.assertEqual(self.sent_to(), ["principal@acme.edu"])
        self.assertNotIn("office@acme.edu", self.sent_to())

    def test_rejection_reaches_the_head_login_and_carries_the_reason(self):
        reject_institute(institute=self.institute, actor=self.admin,
                         reason="The affiliation certificate is not on file.")
        self.assertEqual(self.sent_to(), ["principal@acme.edu"])
        self.assertIn("not on file", mail.outbox[0].body)

    def test_only_heads_are_told_not_every_member_of_staff(self):
        """
        A rejection reason is the head's business. Teachers do not need to read
        that their institute was turned down and why.
        """
        User.objects.create_user(
            email="teacher@acme.edu", password="Str0ngPass!23",
            role=User.Role.TEACHER, institute=self.institute,
            registration_completed=True)
        self.assertEqual(institute_recipients(self.institute),
                         ["principal@acme.edu"])

    def test_an_invited_head_who_has_not_accepted_yet_is_still_reachable(self):
        """
        Their account exists and is unverified, but the address is the one the
        invitation went to — it is exactly where they are expecting to hear.
        """
        self.head.registration_completed = False
        self.head.email_verified = False
        self.head.save()
        self.assertEqual(institute_recipients(self.institute),
                         ["principal@acme.edu"])
