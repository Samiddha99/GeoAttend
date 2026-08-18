"""
Who owns an institute's name, official email and head login.

The rule: an affiliated institute's identity is the affiliating university's
record, because it is the name on the degrees. An autonomous one keeps its own.

Every test here goes through the endpoint rather than the helper where it can.
A disabled input is presentation; the claim being made is that a head who
posts the fields anyway is refused.
"""
import json

from django.test import RequestFactory, TestCase
from django.urls import reverse

from accounts import university_views, views
from accounts.identity import (
    identity_lock_reason,
    is_autonomous,
    may_edit_identity,
)
from accounts.models import (
    Discipline,
    Institute,
    InstituteAffiliation,
    University,
    UniversityDiscipline,
    User,
)


class IdentityFixture(TestCase):
    def setUp(self):
        self.university = University.objects.create(
            name="Engineering University", code="ENGGU", short_name="ENGGU",
            email="registrar@enggu.ac.in", grants_affiliation=True)
        UniversityDiscipline.objects.create(university=self.university,
                                            discipline=Discipline.ENGG)
        self.admin = User.objects.create_user(
            email="admin@enggu.ac.in", password="Str0ngPass!23",
            role=User.Role.UNIVERSITY, university=self.university,
            registration_completed=True)

        self.affiliated = Institute.objects.create(
            name="Acme College", code="ACME", email="office@acme.edu",
            state="Kerala", district="Ernakulam",
            status=Institute.Status.APPROVED)
        InstituteAffiliation.objects.create(
            institute=self.affiliated, discipline=Discipline.ENGG,
            university=self.university)
        self.affiliated_head = User.objects.create_user(
            email="head@acme.edu", password="Str0ngPass!23",
            role=User.Role.HEAD, institute=self.affiliated,
            registration_completed=True)

        self.autonomous = Institute.objects.create(
            name="Free College", code="FREE", email="office@free.edu",
            state="Kerala", district="Thrissur",
            status=Institute.Status.APPROVED)
        InstituteAffiliation.objects.create(
            institute=self.autonomous, discipline=Discipline.ENGG,
            university=None)
        self.autonomous_head = User.objects.create_user(
            email="head@free.edu", password="Str0ngPass!23",
            role=User.Role.HEAD, institute=self.autonomous,
            registration_completed=True)

    def _post(self, view, user, pk=None, **data):
        request = RequestFactory().post("/", data)
        request.user = user
        request.session = self.client.session
        request.META["HTTP_X_REQUESTED_WITH"] = "XMLHttpRequest"
        return view(request, pk=pk) if pk is not None else view(request)

    def _body(self, response):
        return json.loads(response.content)


class AutonomyTests(IdentityFixture):
    def test_an_institute_with_no_university_is_autonomous(self):
        self.assertTrue(is_autonomous(self.autonomous))

    def test_an_affiliated_institute_is_not(self):
        self.assertFalse(is_autonomous(self.affiliated))

    def test_one_affiliated_discipline_is_enough_to_lock_it(self):
        """
        Half-affiliated is not autonomous. There is still a university whose
        name is on the certificates, and it is the one that should be
        correcting the record.
        """
        InstituteAffiliation.objects.create(
            institute=self.affiliated, discipline=Discipline.PHARMACY,
            university=None)
        self.assertFalse(is_autonomous(self.affiliated))

    def test_an_institute_with_nothing_on_file_can_still_fix_itself(self):
        """
        No disciplines at all means nobody to ask. Locking it would leave it
        unable to correct its own name forever.
        """
        orphan = Institute.objects.create(
            name="Orphan College", code="ORPH", email="o@orph.edu")
        self.assertTrue(is_autonomous(orphan))


class PermissionTests(IdentityFixture):
    def test_an_autonomous_head_may_edit_their_own_identity(self):
        self.assertTrue(may_edit_identity(self.autonomous_head, self.autonomous))

    def test_an_affiliated_head_may_not(self):
        self.assertFalse(may_edit_identity(self.affiliated_head, self.affiliated))

    def test_the_university_may_edit_an_institute_it_affiliates(self):
        self.assertTrue(may_edit_identity(self.admin, self.affiliated))

    def test_a_head_cannot_edit_somebody_elses_institute(self):
        self.assertFalse(may_edit_identity(self.autonomous_head, self.affiliated))

    def test_a_hod_is_not_offered_this_at_all(self):
        hod = User.objects.create_user(
            email="hod@free.edu", password="Str0ngPass!23",
            role=User.Role.HOD, institute=self.autonomous)
        self.assertFalse(may_edit_identity(hod, self.autonomous))

    def test_the_lock_message_names_who_to_ask(self):
        reason = identity_lock_reason(self.affiliated_head, self.affiliated)
        self.assertIn("ENGGU", reason)

    def test_there_is_no_lock_message_when_there_is_no_lock(self):
        self.assertIsNone(
            identity_lock_reason(self.autonomous_head, self.autonomous))


class ProfilePageTests(IdentityFixture):
    """Feature 1: the details are actually on the page."""

    def _page(self, user):
        self.client.force_login(user)
        return self.client.get(reverse("accounts:profile"))

    def test_an_institute_head_sees_the_name_and_official_email(self):
        page = self._page(self.autonomous_head).content.decode()
        self.assertIn("Free College", page)
        self.assertIn("office@free.edu", page)

    def test_a_university_sees_its_own_name_and_official_email(self):
        page = self._page(self.admin).content.decode()
        self.assertIn("Engineering University", page)
        self.assertIn("registrar@enggu.ac.in", page)

    def test_an_affiliated_head_sees_them_too_but_locked(self):
        """Shown, not hidden — knowing the record is the point of the panel."""
        page = self._page(self.affiliated_head).content.decode()
        self.assertIn("Acme College", page)
        self.assertIn("office@acme.edu", page)
        self.assertIn("(locked)", page)


class InstituteSelfEditTests(IdentityFixture):
    """Feature 2, at the endpoint."""

    def test_an_autonomous_head_can_rename_their_institute(self):
        response = self._post(
            views.api_organisation_update, self.autonomous_head,
            name="Free University College", code="FREE",
            email="registrar@free.edu", phone="", website="", address="")
        self.assertTrue(self._body(response)["success"], response.content)
        self.autonomous.refresh_from_db()
        self.assertEqual(self.autonomous.name, "Free University College")
        self.assertEqual(self.autonomous.email, "registrar@free.edu")

    def test_an_affiliated_head_is_refused_and_told_why(self):
        response = self._post(
            views.api_organisation_update, self.affiliated_head,
            name="Renamed By Me", code="ACME", email="new@acme.edu",
            phone="", website="", address="")
        self.assertEqual(response.status_code, 403)
        self.assertIn("ENGGU", self._body(response)["message"])
        self.affiliated.refresh_from_db()
        self.assertEqual(self.affiliated.name, "Acme College")
        self.assertEqual(self.affiliated.email, "office@acme.edu")

    def test_an_autonomous_head_can_change_the_code(self):
        """Unlocked this turn — it was read-only for everyone before."""
        response = self._post(
            views.api_organisation_update, self.autonomous_head,
            name="Free College", code="FREECOL", email="office@free.edu",
            phone="", website="", address="")
        self.assertTrue(self._body(response)["success"], response.content)
        self.autonomous.refresh_from_db()
        self.assertEqual(self.autonomous.code, "FREECOL")

    def test_a_university_can_change_its_own_code_and_login(self):
        response = self._post(
            views.api_organisation_update, self.admin,
            name="Engineering University", short_name="ENGGU", code="EU",
            email="registrar@enggu.ac.in", phone="", website="", address="")
        self.assertTrue(self._body(response)["success"], response.content)
        self.university.refresh_from_db()
        self.assertEqual(self.university.code, "EU")

        moved = self._post(views.api_login_email_update, self.admin,
                           email="vc@enggu.ac.in")
        self.assertTrue(self._body(moved)["success"], moved.content)
        self.admin.refresh_from_db()
        self.assertEqual(self.admin.email, "vc@enggu.ac.in")

    def test_an_affiliated_head_still_cannot_change_the_code(self):
        response = self._post(
            views.api_organisation_update, self.affiliated_head,
            name="Acme College", code="HIJACK", email="office@acme.edu",
            phone="", website="", address="")
        self.assertEqual(response.status_code, 403)
        self.affiliated.refresh_from_db()
        self.assertEqual(self.affiliated.code, "ACME")

    def test_a_university_can_change_an_affiliated_institutes_code(self):
        response = self._post(
            university_views.api_institute_update, self.admin,
            pk=self.affiliated.pk, name="Acme College", code="ACMET",
            email="office@acme.edu", phone="", website="", address="")
        self.assertTrue(self._body(response)["success"], response.content)
        self.affiliated.refresh_from_db()
        self.assertEqual(self.affiliated.code, "ACMET")

    def test_an_autonomous_head_can_move_their_login(self):
        response = self._post(views.api_login_email_update,
                              self.autonomous_head, email="principal@free.edu")
        self.assertTrue(self._body(response)["success"], response.content)
        self.autonomous_head.refresh_from_db()
        self.assertEqual(self.autonomous_head.email, "principal@free.edu")

    def test_an_affiliated_head_can_move_their_login_too(self):
        """
        Changed deliberately, and it was the other way round.

        Grouping the login with the name and the official email was wrong.
        Those two are the university's record — they appear on the degrees —
        but a login is not a record of anything, it is how one person reaches
        their own account. Locking it meant a head whose email had changed had
        to ask an outside body for permission to keep signing in.
        """
        response = self._post(views.api_login_email_update,
                              self.affiliated_head, email="mine@acme.edu")
        self.assertTrue(self._body(response)["success"], response.content)
        self.affiliated_head.refresh_from_db()
        self.assertEqual(self.affiliated_head.email, "mine@acme.edu")

    def test_the_name_and_official_email_stay_locked_for_them(self):
        """The freeing above is narrow: only the login moved out of the lock."""
        response = self._post(
            views.api_organisation_update, self.affiliated_head,
            name="Renamed", code="ACME", email="new@acme.edu",
            phone="", website="", address="")
        self.assertEqual(response.status_code, 403)

    def test_a_teacher_still_cannot_move_a_login(self):
        teacher = User.objects.create_user(
            email="t@acme.edu", password="Str0ngPass!23",
            role=User.Role.TEACHER, institute=self.affiliated)
        response = self._post(views.api_login_email_update, teacher,
                              email="other@acme.edu")
        self.assertEqual(response.status_code, 403)

    def test_a_login_address_already_in_use_is_refused(self):
        response = self._post(views.api_login_email_update,
                              self.autonomous_head, email="head@acme.edu")
        body = self._body(response)
        self.assertFalse(body["success"])
        self.assertIn("already uses", str(body["errors"]))

    def test_a_university_editing_its_own_record_goes_down_its_own_branch(self):
        response = self._post(
            views.api_organisation_update, self.admin,
            name="Engineering University of Kerala", short_name="EUK",
            code="ENGGU", email="registrar@enggu.ac.in", phone="", website="",
            address="")
        self.assertTrue(self._body(response)["success"], response.content)
        self.university.refresh_from_db()
        self.assertEqual(self.university.short_name, "EUK")


class UniversityEditsInstituteTests(IdentityFixture):
    """Feature 3."""

    def test_the_university_can_rename_an_institute_and_move_the_head(self):
        response = self._post(
            university_views.api_institute_update, self.admin,
            pk=self.affiliated.pk,
            name="Acme Institute of Technology", code="ACME",
            email="registrar@acme.edu", head_email="principal@acme.edu",
            phone="", website="", address="")
        body = self._body(response)
        self.assertTrue(body["success"], response.content)

        self.affiliated.refresh_from_db()
        self.assertEqual(self.affiliated.name, "Acme Institute of Technology")
        self.assertEqual(self.affiliated.email, "registrar@acme.edu")
        self.affiliated_head.refresh_from_db()
        self.assertEqual(self.affiliated_head.email, "principal@acme.edu")

    def test_the_answer_says_the_head_has_not_been_told(self):
        """
        No password is reset and no mail is sent, so the university has to know
        it owns telling them — otherwise the head is simply locked out.
        """
        response = self._post(
            university_views.api_institute_update, self.admin,
            pk=self.affiliated.pk, name="Acme College", code="ACME",
            email="office@acme.edu", head_email="principal@acme.edu",
            phone="", website="", address="")
        self.assertIn("we have not", self._body(response)["message"])

    def test_leaving_the_head_email_unchanged_touches_nothing(self):
        response = self._post(
            university_views.api_institute_update, self.admin,
            pk=self.affiliated.pk, name="Acme College", code="ACME",
            email="office@acme.edu", head_email="head@acme.edu", phone="",
            website="", address="")
        self.assertTrue(self._body(response)["success"])
        self.affiliated_head.refresh_from_db()
        self.assertEqual(self.affiliated_head.email, "head@acme.edu")

    def test_a_head_login_that_collides_is_refused_and_nothing_is_saved(self):
        """
        The name and the login move together or not at all — a rename that
        half-succeeded would leave the university unsure what it had changed.
        """
        response = self._post(
            university_views.api_institute_update, self.admin,
            pk=self.affiliated.pk, name="Renamed", code="ACME",
            email="office@acme.edu", head_email="head@free.edu", phone="",
            website="", address="")
        body = self._body(response)
        self.assertFalse(body["success"])
        self.assertIn("head_email", body["errors"])
        self.affiliated.refresh_from_db()
        self.assertEqual(self.affiliated.name, "Acme College")

    def test_a_university_cannot_edit_an_institute_it_does_not_reach(self):
        stranger = Institute.objects.create(
            name="Other College", code="OTHER", email="o@other.edu")
        with self.assertRaises(Exception):
            self._post(university_views.api_institute_update, self.admin,
                       pk=stranger.pk, name="Hijacked", code="OTHER",
                       email="x@other.edu", phone="", website="", address="")

    def test_the_university_can_also_edit_an_autonomous_institute_it_invited(self):
        """
        Invited, so within reach. The institute can edit itself too — the two
        permissions are not exclusive, and both are legitimate here.
        """
        self.autonomous.invited_by = self.university
        self.autonomous.save()
        response = self._post(
            university_views.api_institute_update, self.admin,
            pk=self.autonomous.pk, name="Free College Renamed", code="FREE",
            email="office@free.edu", phone="", website="", address="")
        self.assertTrue(self._body(response)["success"], response.content)
