"""
Registration: where an institute is, what it teaches, and who awards its
degrees — plus the university side of the same question.

The interesting cases are the ones a browser cannot be trusted to prevent:
a district posted under the wrong state, an affiliating body posted for a
discipline it does not cover, and a seeded university claimed twice.
"""
from django.test import TestCase
from django.urls import reverse

from accounts.forms import AUTONOMOUS, InstituteSignupForm, UniversitySignupForm
from accounts.models import (
    Discipline,
    Institute,
    InstituteAffiliation,
    University,
    UniversityDiscipline,
    User,
)


def institute_payload(**overrides):
    data = {
        "institute_name": "Acme Institute of Technology",
        "institute_code": "ACME",
        "institute_email": "office@acme.edu",
        "state": "Kerala",
        "district": "Ernakulam",
        "disciplines": ["ENGG"],
        "head_name": "Asha Roy",
        "head_email": "head@acme.edu",
        "password1": "Str0ngPass!23",
        "password2": "Str0ngPass!23",
    }
    data.update(overrides)
    return data


class PlaceValidationTests(TestCase):
    """Feature 2, on the server side."""

    def test_a_real_state_and_district_are_accepted(self):
        form = InstituteSignupForm(institute_payload(affiliation_ENGG=AUTONOMOUS))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["state"], "Kerala")

    def test_a_district_from_the_wrong_state_is_refused(self):
        """
        Validating the two independently would accept this — both strings are
        real, just not together. It is the pairing that has to be checked.
        """
        form = InstituteSignupForm(institute_payload(
            district="Bhopal", affiliation_ENGG=AUTONOMOUS))
        self.assertFalse(form.is_valid())
        self.assertIn("district", form.errors)
        self.assertIn("not a district of Kerala", str(form.errors["district"]))

    def test_an_invented_state_is_refused(self):
        form = InstituteSignupForm(institute_payload(
            state="Atlantis", affiliation_ENGG=AUTONOMOUS))
        self.assertFalse(form.is_valid())
        self.assertIn("state", form.errors)

    def test_a_bad_state_does_not_also_complain_about_the_district(self):
        """One mistake, one message."""
        form = InstituteSignupForm(institute_payload(
            state="Atlantis", district="Nowhere", affiliation_ENGG=AUTONOMOUS))
        self.assertFalse(form.is_valid())
        self.assertNotIn("district", form.errors)


class AffiliationChoiceTests(TestCase):
    """Feature 1: one affiliating body per discipline, or autonomous."""

    def setUp(self):
        self.engg = University.objects.create(
            name="Engineering University", code="ENGGU", email="e@u.edu")
        UniversityDiscipline.objects.create(university=self.engg,
                                            discipline=Discipline.ENGG)
        self.health = University.objects.create(
            name="Health University", code="HEALTHU", email="h@u.edu")
        UniversityDiscipline.objects.create(university=self.health,
                                            discipline=Discipline.PHARMACY)

    def test_a_body_is_accepted_for_a_discipline_it_covers(self):
        form = InstituteSignupForm(institute_payload(
            affiliation_ENGG=str(self.engg.pk)))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["affiliations"],
                         {"ENGG": self.engg})

    def test_a_body_is_refused_for_a_discipline_it_does_not_cover(self):
        """
        The browser only offers bodies for the chosen discipline, so this can
        only arrive by hand — and filing an institute under a university that
        never grants engineering affiliation would be worse than refusing it.
        """
        form = InstituteSignupForm(institute_payload(
            affiliation_ENGG=str(self.health.pk)))
        self.assertFalse(form.is_valid())
        self.assertIn("does not grant affiliation", str(form.errors))

    def test_a_body_that_does_not_affiliate_at_all_is_refused(self):
        self.engg.grants_affiliation = False
        self.engg.save()
        form = InstituteSignupForm(institute_payload(
            affiliation_ENGG=str(self.engg.pk)))
        self.assertFalse(form.is_valid())

    def test_autonomous_is_accepted_and_is_not_a_missing_answer(self):
        form = InstituteSignupForm(institute_payload(
            affiliation_ENGG=AUTONOMOUS))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["affiliations"], {"ENGG": None})

    def test_leaving_a_chosen_discipline_unanswered_is_refused(self):
        form = InstituteSignupForm(institute_payload(affiliation_ENGG=""))
        self.assertFalse(form.is_valid())
        self.assertIn("Choose an affiliating body", str(form.errors))

    def test_two_disciplines_may_answer_to_two_universities(self):
        form = InstituteSignupForm(institute_payload(
            disciplines=["ENGG", "PHARMACY"],
            affiliation_ENGG=str(self.engg.pk),
            affiliation_PHARMACY=str(self.health.pk)))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["affiliations"],
                         {"ENGG": self.engg, "PHARMACY": self.health})

    def test_an_unticked_discipline_is_ignored_even_if_posted(self):
        """
        The affiliation inputs are always in the DOM. Only the ticked ones may
        count, or an institute would be filed under a discipline it did not
        claim.
        """
        form = InstituteSignupForm(institute_payload(
            affiliation_ENGG=AUTONOMOUS,
            affiliation_PHARMACY=str(self.health.pk)))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(list(form.cleaned_data["affiliations"]), ["ENGG"])


class InstituteCreationTests(TestCase):
    """What the signup actually writes, and whether the head may sign in."""

    def setUp(self):
        self.university = University.objects.create(
            name="Engineering University", code="ENGGU", email="e@u.edu")
        UniversityDiscipline.objects.create(university=self.university,
                                            discipline=Discipline.ENGG)

    def _create(self, affiliations):
        from accounts.services import create_institute_and_head

        return create_institute_and_head({
            "institute_name": "Acme", "institute_code": "ACME",
            "institute_email": "office@acme.edu",
            "state": "Kerala", "district": "Ernakulam",
            "head_name": "Asha", "head_email": "head@acme.edu",
            "password": "Str0ngPass!23",
            "affiliations": affiliations,
        })

    def test_naming_a_university_leaves_the_institute_pending(self):
        institute, head = self._create({"ENGG": self.university.pk})
        self.assertEqual(institute.status, Institute.Status.PENDING)
        self.assertEqual(institute.state, "Kerala")
        self.assertEqual(institute.affiliations.count(), 1)

    def test_being_autonomous_everywhere_needs_no_approval(self):
        """There is nobody left to ask, so waiting would be waiting forever."""
        institute, head = self._create({"ENGG": None})
        self.assertEqual(institute.status, Institute.Status.APPROVED)
        self.assertTrue(institute.affiliations.first().is_autonomous)

    def test_a_pending_head_cannot_sign_in_and_is_told_why(self):
        from accounts.institute_approval import sign_in_blocked_reason

        institute, head = self._create({"ENGG": self.university.pk})
        reason = sign_in_blocked_reason(head)
        self.assertIsNotNone(reason)
        self.assertIn("waiting for approval", reason)
        self.assertIn("Engineering University", reason)

    def test_an_autonomous_head_may_sign_in_immediately(self):
        from accounts.institute_approval import sign_in_blocked_reason

        institute, head = self._create({"ENGG": None})
        self.assertIsNone(sign_in_blocked_reason(head))

    def test_approval_and_rejection_move_the_institute_and_keep_the_reason(self):
        from accounts.institute_approval import (
            approve_institute,
            reject_institute,
            sign_in_blocked_reason,
        )

        institute, head = self._create({"ENGG": self.university.pk})
        admin = User.objects.create_user(
            email="admin@u.edu", password="Str0ngPass!23",
            role=User.Role.UNIVERSITY, university=self.university)

        reject_institute(institute=institute, actor=admin,
                         reason="Affiliation certificate not on file.")
        institute.refresh_from_db()
        self.assertEqual(institute.status, Institute.Status.REJECTED)
        head.refresh_from_db()
        self.assertIn("certificate", sign_in_blocked_reason(head))

        approve_institute(institute=institute, actor=admin)
        institute.refresh_from_db()
        head.refresh_from_db()
        self.assertIsNone(sign_in_blocked_reason(head))
        # The reason survives: a decision history is worth more than a tidy row.
        self.assertIn("certificate", institute.rejection_reason)

    # Mail is queued to a thread pool, so `mail.outbox` is still empty when the
    # call returns. Asserting at the boundary this module actually owns — "we
    # asked to send X to Y, and the template rendered" — is both accurate and
    # free of a sleep. Whether the transport delivers belongs to the mailer.
    def _sent(self, fn, *args, **kwargs):
        from unittest.mock import patch

        from django.template.loader import render_to_string

        calls = []

        def record(subject, to, template, context=None, **rest):
            # Rendered for real, so a broken template still fails the test.
            body = render_to_string(f"emails/{template}.txt", context or {})
            calls.append({"subject": subject, "to": to,
                          "template": template, "body": body})

        with patch("notifications.mailer.send_template_mail", record):
            fn(*args, **kwargs)
        return calls

    def test_the_university_is_emailed_when_an_institute_registers(self):
        """
        Mail failures are swallowed on purpose — a provider outage must not
        roll back a signup — which means nothing else would notice if the
        template stopped rendering. This is what notices.
        """
        admin = User.objects.create_user(
            email="registrar@u.edu", password="Str0ngPass!23",
            role=User.Role.UNIVERSITY, university=self.university)

        calls = self._sent(self._create, {"ENGG": self.university.pk})
        self.assertEqual(len(calls), 1)
        # The login, not `University.email`. See accounts/recipients.py: a
        # seeded university's official address is on `.invalid` and cannot
        # receive anything.
        self.assertEqual(calls[0]["to"], [admin.email])
        self.assertIn("Acme", calls[0]["subject"])
        self.assertIn("Ernakulam", calls[0]["body"])

    def test_the_head_is_emailed_the_verdict_including_the_reason(self):
        from accounts.institute_approval import approve_institute, reject_institute

        institute, head = self._create({"ENGG": self.university.pk})
        admin = User.objects.create_user(
            email="admin2@u.edu", password="Str0ngPass!23",
            role=User.Role.UNIVERSITY, university=self.university)

        calls = self._sent(reject_institute, institute=institute, actor=admin,
                           reason="Affiliation certificate not on file.")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["to"], [head.email])
        self.assertIn("certificate", calls[0]["body"])

        calls = self._sent(approve_institute, institute=institute, actor=admin)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["to"], [head.email])
        self.assertIn("approved", calls[0]["subject"].lower())

    def test_an_autonomous_signup_asks_nobody_for_approval(self):
        """Nothing to approve, so nothing to ask."""
        calls = self._sent(self._create, {"ENGG": None})
        self.assertEqual(calls, [])

    def test_the_otp_payload_is_plain_json(self):
        """
        The regression this file did not catch the first time.

        Every other test here uses Autonomous, which stores `None` — so the
        affiliation id was never exercised. On MongoDB a raw primary key is a
        BSON ObjectId: it writes into the JSONField subdocument happily and
        then fails on the way back out, one request later, reported to the user
        as "your signup session expired".

        Asserting the payload is JSON-serialisable catches it on any backend,
        including sqlite where the key is an integer and the bug is invisible.
        """
        import json

        from django.urls import reverse

        response = self.client.post(reverse("accounts:api_signup_start"), {
            "institute_name": "Acme", "institute_code": "ACME2",
            "institute_email": "office2@acme.edu",
            "state": "Kerala", "district": "Ernakulam",
            "disciplines": ["ENGG"],
            "affiliation_ENGG": str(self.university.pk),
            "head_name": "Asha", "head_email": "head2@acme.edu",
            "password1": "Str0ngPass!23", "password2": "Str0ngPass!23",
        }, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertTrue(response.json()["success"], response.json())

        from accounts.models import EmailOTP

        payload = EmailOTP.objects.get(email="head2@acme.edu").payload
        json.dumps(payload)          # raises if anything in there is not JSON
        stored = payload["affiliations"]["ENGG"]
        self.assertIsInstance(stored, str)
        self.assertEqual(stored, str(self.university.pk))

    def test_an_affiliated_signup_survives_the_round_trip(self):
        """Start to verify, with a real university rather than Autonomous."""
        from django.urls import reverse

        from accounts.models import EmailOTP

        self.client.post(reverse("accounts:api_signup_start"), {
            "institute_name": "Beta", "institute_code": "BETA",
            "institute_email": "office@beta.edu",
            "state": "Kerala", "district": "Ernakulam",
            "disciplines": ["ENGG"],
            "affiliation_ENGG": str(self.university.pk),
            "head_name": "Bela", "head_email": "head@beta.edu",
            "password1": "Str0ngPass!23", "password2": "Str0ngPass!23",
        }, HTTP_X_REQUESTED_WITH="XMLHttpRequest")

        otp = EmailOTP.objects.get(email="head@beta.edu")
        # The session must still resolve to this row on the next request —
        # that lookup is what was failing.
        from accounts.views import pending_signup_otp

        request = self.client.get(reverse("accounts:signup")).wsgi_request
        self.assertIsNotNone(pending_signup_otp(request))

        institute = Institute.objects.filter(code="BETA").first()
        self.assertIsNone(institute)      # nothing created before verification

    def test_a_rejection_needs_a_reason(self):
        from accounts.institute_approval import reject_institute

        institute, head = self._create({"ENGG": self.university.pk})
        with self.assertRaises(ValueError):
            reject_institute(institute=institute, actor=None, reason="   ")


class UniversitySignupTests(TestCase):
    """Feature 3."""

    def _payload(self, **overrides):
        data = {
            "university_name": "Anna University",
            "university_code": "ANNA",
            "university_email": "registrar@annauniv.edu",
            "state": "Tamil Nadu", "district": "Chennai",
            "disciplines": ["ENGG"],
            "grants_affiliation": "on",
            "admin_name": "R. Kumar",
            "admin_email": "admin@annauniv.edu",
            "password1": "Str0ngPass!23", "password2": "Str0ngPass!23",
        }
        data.update(overrides)
        return data

    def test_a_new_university_registers_with_its_disciplines(self):
        from accounts.services import create_university_and_admin

        form = UniversitySignupForm(self._payload())
        self.assertTrue(form.is_valid(), form.errors)
        university, admin = create_university_and_admin({
            **form.cleaned_data, "password": form.cleaned_data["password1"]})
        self.assertTrue(university.grants_affiliation)
        self.assertTrue(university.is_claimed)
        self.assertEqual(admin.role, User.Role.UNIVERSITY)
        self.assertEqual(admin.university_id, university.pk)
        self.assertEqual(
            sorted(university.disciplines.values_list("discipline", flat=True)),
            ["ENGG"])

    def test_claiming_a_seeded_body_reuses_its_row(self):
        """
        The whole point of the shipped list: one real university is one
        account, not two that cannot see each other's institutes.
        """
        from accounts.services import create_university_and_admin

        seeded = University.objects.create(
            name="Anna University", code="anna-university",
            email="anna-university@unclaimed.invalid", is_seeded=True)
        form = UniversitySignupForm(self._payload(existing=str(seeded.pk)))
        self.assertTrue(form.is_valid(), form.errors)
        university, _ = create_university_and_admin({
            **form.cleaned_data, "password": form.cleaned_data["password1"]})
        self.assertEqual(university.pk, seeded.pk)
        self.assertEqual(University.objects.count(), 1)
        self.assertEqual(university.email, "registrar@annauniv.edu")

    def test_a_claimed_body_cannot_be_claimed_again(self):
        from django.utils import timezone

        seeded = University.objects.create(
            name="Anna University", code="anna-university",
            email="taken@annauniv.edu", is_seeded=True,
            claimed_at=timezone.now())
        form = UniversitySignupForm(self._payload(existing=str(seeded.pk)))
        self.assertFalse(form.is_valid())
        self.assertIn("already been registered", str(form.errors))

    def test_registering_a_name_that_exists_is_refused(self):
        University.objects.create(name="Anna University", code="X",
                                  email="x@u.edu")
        form = UniversitySignupForm(self._payload())
        self.assertFalse(form.is_valid())
        self.assertIn("already registered", str(form.errors))

    def test_a_university_that_does_not_affiliate_is_hidden_from_institutes(self):
        """
        Feature 3's flag: it decides whether the body appears on an institute's
        registration form at all.
        """
        from accounts.services import create_university_and_admin

        form = UniversitySignupForm(self._payload(grants_affiliation=""))
        self.assertTrue(form.is_valid(), form.errors)
        university, _ = create_university_and_admin({
            **form.cleaned_data, "password": form.cleaned_data["password1"]})
        self.assertFalse(university.grants_affiliation)

        response = self.client.get(reverse("accounts:api_affiliating_bodies"),
                                   {"discipline": "ENGG"})
        names = [r["name"] for r in response.json()["data"]["rows"]]
        self.assertNotIn("Anna University", names)


class AffiliatingBodyEndpointTests(TestCase):
    def setUp(self):
        self.engg = University.objects.create(
            name="Engineering University", code="E", email="e@u.edu")
        UniversityDiscipline.objects.create(university=self.engg,
                                            discipline=Discipline.ENGG)

    def test_it_lists_only_bodies_for_the_discipline_asked_for(self):
        response = self.client.get(reverse("accounts:api_affiliating_bodies"),
                                   {"discipline": "ENGG"})
        self.assertEqual([r["name"] for r in response.json()["data"]["rows"]],
                         ["Engineering University"])
        response = self.client.get(reverse("accounts:api_affiliating_bodies"),
                                   {"discipline": "PHARMACY"})
        self.assertEqual(response.json()["data"]["rows"], [])

    def test_an_unknown_discipline_yields_nothing_rather_than_everything(self):
        response = self.client.get(reverse("accounts:api_affiliating_bodies"),
                                   {"discipline": "ASTROLOGY"})
        self.assertEqual(response.json()["data"]["rows"], [])
