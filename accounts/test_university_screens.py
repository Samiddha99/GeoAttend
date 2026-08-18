"""
A university acting as a university: inviting institutes, deciding on them,
and the two things it is deliberately *not* allowed to do.

The last of those matters most. A university has a head's read and write reach
over institute data — that is the requirement — so the interesting tests are
the boundaries: it may not borrow an institute's WhatsApp wording, and it may
not get hold of a head's login.
"""
from django.test import TestCase
from django.urls import reverse

from accounts.models import (
    Discipline,
    Institute,
    InstituteAffiliation,
    Invitation,
    University,
    UniversityDiscipline,
    User,
)
from accounts.scoping import institutes_for


class UniversityFixture(TestCase):
    def setUp(self):
        self.university = University.objects.create(
            name="Engineering University", code="ENGGU", email="e@u.edu",
            grants_affiliation=True)
        UniversityDiscipline.objects.create(university=self.university,
                                            discipline=Discipline.ENGG)
        self.admin = User.objects.create_user(
            email="admin@u.edu", password="Str0ngPass!23",
            role=User.Role.UNIVERSITY, university=self.university,
            registration_completed=True)

        # An institute that registered itself and named this university.
        self.applicant = Institute.objects.create(
            name="Acme College", code="ACME", email="office@acme.edu",
            state="Kerala", district="Ernakulam",
            status=Institute.Status.PENDING)
        InstituteAffiliation.objects.create(
            institute=self.applicant, discipline=Discipline.ENGG,
            university=self.university)
        self.applicant_head = User.objects.create_user(
            email="head@acme.edu", password="Str0ngPass!23",
            role=User.Role.HEAD, institute=self.applicant,
            registration_completed=True)

        # Somebody else's institute, to prove scoping bites.
        self.stranger = Institute.objects.create(
            name="Other College", code="OTHER", email="office@other.edu")

    def sign_in(self):
        self.client.force_login(self.admin)


class InstituteInvitationTests(UniversityFixture):
    """Feature 4."""

    def _invite(self, **overrides):
        data = {
            "institute_name": "Invited College", "institute_code": "INV",
            "institute_email": "office@invited.edu",
            "head_email": "principal@invited.edu",
            "state": "Kerala", "district": "Thrissur",
            "disciplines": ["ENGG"],
        }
        data.update(overrides)
        return self.client.post(reverse("accounts:api_institute_invite"), data,
                                HTTP_X_REQUESTED_WITH="XMLHttpRequest")

    def test_a_university_creates_an_institute_and_invites_its_head(self):
        self.sign_in()
        response = self._invite()
        self.assertTrue(response.json()["success"], response.json())

        institute = Institute.objects.get(code="INV")
        self.assertEqual(institute.invited_by_id, self.university.pk)
        self.assertEqual(institute.state, "Kerala")
        # Approved on creation: the university inviting *is* the approval, and
        # a queue containing only what you put there yourself is not a queue.
        self.assertEqual(institute.status, Institute.Status.APPROVED)

        head = User.objects.get(email="principal@invited.edu")
        self.assertEqual(head.role, User.Role.HEAD)
        self.assertFalse(head.registration_completed)
        self.assertTrue(Invitation.objects.filter(
            email="principal@invited.edu",
            status=Invitation.Status.PENDING).exists())

    def test_the_university_is_recorded_as_the_affiliating_body(self):
        self.sign_in()
        self._invite()
        institute = Institute.objects.get(code="INV")
        affiliation = institute.affiliations.get(discipline=Discipline.ENGG)
        self.assertEqual(affiliation.university_id, self.university.pk)

    def test_a_university_that_does_not_affiliate_can_still_invite(self):
        """
        Explicit in the requirement, and worth a test: inviting is not
        affiliating. The institute is simply autonomous.
        """
        self.university.grants_affiliation = False
        self.university.save()
        self.sign_in()
        self.assertTrue(self._invite().json()["success"])
        institute = Institute.objects.get(code="INV")
        self.assertTrue(institute.affiliations.get(
            discipline=Discipline.ENGG).is_autonomous)
        self.assertIn(institute, list(institutes_for(self.admin)))

    def test_a_discipline_the_university_does_not_cover_is_autonomous(self):
        self.sign_in()
        self._invite(disciplines=["ENGG", "PHARMACY"])
        institute = Institute.objects.get(code="INV")
        self.assertEqual(
            institute.affiliations.get(discipline=Discipline.ENGG).university_id,
            self.university.pk)
        self.assertTrue(institute.affiliations.get(
            discipline=Discipline.PHARMACY).is_autonomous)

    def test_a_district_from_the_wrong_state_is_refused(self):
        self.sign_in()
        response = self._invite(district="Bhopal")
        self.assertFalse(response.json()["success"])
        self.assertFalse(Institute.objects.filter(code="INV").exists())

    def test_only_a_university_may_invite_an_institute(self):
        self.client.force_login(self.applicant_head)
        self.assertEqual(self._invite().status_code, 403)


class InvitedHeadAcceptanceTests(UniversityFixture):
    """Feature 4's second half: what the invited head may set."""

    def setUp(self):
        super().setUp()
        from accounts.services import invite_institute

        self.institute, self.head, self.invitation = invite_institute(
            university=self.university, name="Invited College", code="INV",
            email="office@invited.edu", head_email="principal@invited.edu",
            state="Kerala", district="Thrissur",
            affiliations={Discipline.ENGG: self.university.pk},
            invited_by=self.admin)

    def _accept(self, **overrides):
        data = {
            "institute_email": "real@invited.edu",
            "phone": "+91 480 111 2222",
            "website": "https://invited.edu",
            "address": "12 College Road",
            "full_name": "Dr. Principal",
            "phone_head": "+91 90000 00000",
            "password1": "Str0ngPass!23", "password2": "Str0ngPass!23",
        }
        data.update(overrides)
        return self.client.post(
            reverse("accounts:api_invite_accept", args=[self.invitation.token]),
            data, HTTP_X_REQUESTED_WITH="XMLHttpRequest")

    def test_accepting_sets_the_institute_contact_details_and_the_head(self):
        response = self._accept()
        self.assertTrue(response.json()["success"], response.json())

        self.institute.refresh_from_db()
        self.assertEqual(self.institute.email, "real@invited.edu")
        self.assertEqual(self.institute.website, "https://invited.edu")
        self.assertEqual(self.institute.address, "12 College Road")

        self.head.refresh_from_db()
        self.assertEqual(self.head.full_name, "Dr. Principal")
        self.assertEqual(self.head.phone, "+91 90000 00000")
        self.assertTrue(self.head.registration_completed)

    def test_the_head_cannot_move_the_institute_to_another_place(self):
        """
        State and district are not on the form at all — not disabled, absent.
        Posting them anyway must change nothing, or an institute could walk out
        from under the university that placed it.
        """
        self._accept(state="Bihar", district="Patna")
        self.institute.refresh_from_db()
        self.assertEqual(self.institute.state, "Kerala")
        self.assertEqual(self.institute.district, "Thrissur")

    def test_the_head_cannot_rename_the_institute_or_change_its_code(self):
        self._accept(institute_name="Renamed", institute_code="NEW")
        self.institute.refresh_from_db()
        self.assertEqual(self.institute.name, "Invited College")
        self.assertEqual(self.institute.code, "INV")

    def test_an_invited_head_may_sign_in_at_once(self):
        """No approval step: the university that invited them already decided."""
        from accounts.institute_approval import sign_in_blocked_reason

        self._accept()
        self.head.refresh_from_db()
        self.assertIsNone(sign_in_blocked_reason(self.head))


class ApprovalScreenTests(UniversityFixture):
    """Feature 5."""

    def _decide(self, institute, action, **data):
        """
        Call the view directly rather than through `reverse()`.

        The URL converter insists on a 24-character ObjectId, which is right
        for MongoDB and impossible for the integer keys this harness gets from
        sqlite. The view is what is under test, not the routing.
        """
        from django.test import RequestFactory

        from accounts import university_views

        request = RequestFactory().post("/x/", data,
                                        HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        request.user = self.admin
        view = getattr(university_views, f"api_institute_{action}")
        return view(request, pk=institute.pk)

    def test_the_list_counts_each_tab_from_the_same_rows(self):
        self.sign_in()
        data = self.client.get(reverse("accounts:api_institutes")).json()["data"]
        self.assertEqual(data["counts"]["pending"], 1)
        self.assertEqual(data["counts"]["all"], len(data["rows"]))
        names = [r["name"] for r in data["rows"]]
        self.assertIn("Acme College", names)
        # Scoping bites: somebody else's institute is not in the list.
        self.assertNotIn("Other College", names)

    def test_a_rejected_row_carries_its_reason(self):
        from accounts.institute_approval import reject_institute

        reject_institute(institute=self.applicant, actor=self.admin,
                         reason="Certificate missing.")
        self.sign_in()
        rows = self.client.get(reverse("accounts:api_institutes")).json()["data"]["rows"]
        row = next(r for r in rows if r["name"] == "Acme College")
        self.assertEqual(row["status"], "REJECTED")
        self.assertEqual(row["rejection_reason"], "Certificate missing.")

    def test_approving_lets_the_head_in(self):
        from accounts.institute_approval import sign_in_blocked_reason

        response = self._decide(self.applicant, "approve")
        self.assertEqual(response.status_code, 200)
        self.applicant_head.refresh_from_db()
        self.assertIsNone(sign_in_blocked_reason(self.applicant_head))

    def test_rejecting_without_a_reason_is_refused(self):
        response = self._decide(self.applicant, "reject", reason="   ")
        self.assertEqual(response.status_code, 400)
        self.applicant.refresh_from_db()
        self.assertEqual(self.applicant.status, Institute.Status.PENDING)

    def test_a_university_cannot_decide_on_an_institute_it_does_not_reach(self):
        from django.http import Http404

        with self.assertRaises(Http404):
            self._decide(self.stranger, "approve")
        self.stranger.refresh_from_db()
        self.assertEqual(self.stranger.status, Institute.Status.APPROVED)

    def test_the_pending_badge_counts_only_this_university_s_queue(self):
        from core.context_processors import pending_institute_count

        request = type("R", (), {"user": self.admin})()
        self.assertEqual(pending_institute_count(request), 1)


class UniversityBoundaryTests(UniversityFixture):
    """Feature 7's two exceptions."""

    def test_a_university_sees_only_its_own_whatsapp_templates(self):
        """
        The one place university access is *narrower* than a head's. An
        institute's approved wording is registered against that institute's
        own sender, so a university borrowing it would send messages that
        appear to come from the college.
        """
        from notifications.models import WhatsAppTemplate
        from notifications.template_service import templates_for

        WhatsAppTemplate.objects.create(
            institute=self.applicant, name="Theirs", twilio_name="theirs",
            audience=WhatsAppTemplate.Audience.STUDENT, body="x")
        mine = WhatsAppTemplate.objects.create(
            university=self.university, name="Mine", twilio_name="mine",
            audience=WhatsAppTemplate.Audience.STUDENT, body="x")

        visible = list(templates_for(self.admin))
        self.assertEqual([t.pk for t in visible], [mine.pk])

        # And the head still sees theirs, not the university's.
        head_visible = list(templates_for(self.applicant_head))
        self.assertEqual([t.name for t in head_visible], ["Theirs"])

    def test_a_template_must_have_exactly_one_owner(self):
        """
        This was a CheckConstraint until MongoDB pointed out it has none —
        Django accepts such a constraint and enforces nothing, which reads like
        a guarantee and is not one. The rule lives in `clean()`/`save()` now,
        so it needs a test that the database is no longer providing.
        """
        from django.core.exceptions import ValidationError

        from notifications.models import WhatsAppTemplate

        base = dict(name="X", twilio_name="x",
                    audience=WhatsAppTemplate.Audience.STUDENT, body="x")

        with self.assertRaises(ValidationError):      # neither
            WhatsAppTemplate.objects.create(**base)
        with self.assertRaises(ValidationError):      # both
            WhatsAppTemplate.objects.create(
                institute=self.applicant, university=self.university, **base)

        # Either one alone is fine.
        WhatsAppTemplate.objects.create(institute=self.applicant, **base)
        WhatsAppTemplate.objects.create(
            university=self.university, **dict(base, twilio_name="y"))

    def test_a_university_cannot_start_a_password_reset_for_a_head(self):
        """
        Reading an institute's data is the role. Taking over its head's login
        is not — and a reset code is a takeover.
        """
        from accounts.models import EmailOTP

        self.sign_in()
        self.client.post(reverse("accounts:api_forgot_start"),
                         {"email": self.applicant_head.email})
        self.assertFalse(EmailOTP.objects.filter(
            email=self.applicant_head.email).exists())

    def test_a_signed_out_visitor_can_still_reset_a_head_password(self):
        """The guard is about who is asking, not about heads being unresettable."""
        from accounts.models import EmailOTP

        self.client.post(reverse("accounts:api_forgot_start"),
                         {"email": self.applicant_head.email})
        self.assertTrue(EmailOTP.objects.filter(
            email=self.applicant_head.email).exists())

    def test_a_university_reaches_its_institutes_data_like_a_head(self):
        """The other side of the boundary: everything else *is* allowed."""
        self.sign_in()
        for name in ("academics:students", "academics:teachers",
                     "attendance:sessions", "dashboard:reports"):
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)


class InstituteFocusTests(UniversityFixture):
    """Feature 8's mechanism, which the Institutes screen drives."""

    def test_focusing_narrows_the_scope_and_clearing_restores_it(self):
        from accounts.scoping import active_institute, visible_institutes

        self.sign_in()
        response = self.client.post(
            reverse("accounts:api_switch_institute"),
            {"institute": str(self.applicant.id)},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertTrue(response.json()["success"])

        request = self.client.get(reverse("dashboard:home")).wsgi_request
        self.assertEqual(active_institute(request).pk, self.applicant.pk)
        self.assertEqual([i.pk for i in visible_institutes(request)],
                         [self.applicant.pk])

        self.client.post(reverse("accounts:api_switch_institute"), {"institute": ""},
                         HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        request = self.client.get(reverse("dashboard:home")).wsgi_request
        self.assertIsNone(active_institute(request))

    def test_focusing_an_institute_out_of_reach_is_refused(self):
        """Focus can only ever narrow reach — never widen it."""
        self.sign_in()
        response = self.client.post(
            reverse("accounts:api_switch_institute"),
            {"institute": str(self.stranger.id)},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 403)
