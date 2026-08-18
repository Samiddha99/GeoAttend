"""
One teacher, one college at a time — enforced on PAN.

The tests are grouped by the four moments the rule is asked about: adding,
being blocked by somebody else's hold, reactivating, and editing. The fifth
group is verification, which is the only part that leaves the building.

`PAN_verification` is patched throughout. It reaches a KYC provider over the
network; a test suite that depended on that would be testing the provider's
uptime, and the sandbox cannot reach it at all.
"""
import json
from datetime import date
from unittest.mock import patch

from django.test import RequestFactory, TestCase

from academics import views
from academics.models import Batch, Department, Subject
from accounts import pan as pan_rules
from accounts.models import (
    Discipline,
    Institute,
    InstituteAffiliation,
    University,
    UniversityDiscipline,
    User,
)
from core.enums import RowStatus

PAN = "ABCDE1234F"
DOB = date(1985, 4, 12)
VERIFIED = {"verified": True}


def verifies(result=None, error=None):
    """Patch the KYC call — the one thing here that would leave the building."""
    if error is not None:
        return patch("core.utils.PAN_verification", side_effect=error)
    return patch("core.utils.PAN_verification",
                 return_value=result if result is not None else VERIFIED)


class PanFixture(TestCase):
    def setUp(self):
        self.university = University.objects.create(
            name="Engineering University", code="ENGGU", short_name="ENGGU",
            email="e@u.edu", grants_affiliation=True)
        UniversityDiscipline.objects.create(university=self.university,
                                            discipline=Discipline.ENGG)
        self.a = self._college("Alpha College", "ALPHA")
        self.b = self._college("Beta College", "BETA")

    def _college(self, name, code):
        institute = Institute.objects.create(
            name=name, code=code, email=f"{code.lower()}@x.edu",
            status=Institute.Status.APPROVED)
        InstituteAffiliation.objects.create(
            institute=institute, discipline=Discipline.ENGG,
            university=self.university)
        head = User.objects.create_user(
            email=f"head@{code.lower()}.edu", password="Str0ngPass!23",
            role=User.Role.HEAD, institute=institute,
            registration_completed=True)
        department = Department.objects.create(
            institute=institute, code="CSE", name="Computer Science",
            discipline=Discipline.ENGG)
        batch = Batch.objects.create(department=department, label="2022-26",
                                     start_year=2022, end_year=2026)
        subject = Subject.objects.create(
            department=department, code="DSA", name="Data Structures",
            semester=3)
        institute.head, institute.department = head, department
        institute.batch, institute.subject = batch, subject
        return institute

    def call(self, view, user, pk=None, **data):
        request = RequestFactory().post("/", data)
        request.user = user
        request.session = self.client.session
        request.META["HTTP_X_REQUESTED_WITH"] = "XMLHttpRequest"
        return view(request, pk=pk) if pk is not None else view(request)

    def body(self, response):
        return json.loads(response.content)

    def invite(self, college, *, email="t@x.edu", pan=PAN, dob=DOB,
               name="Asha Rao"):
        return self.call(
            views.api_teacher_invite, college.head,
            email=email, full_name=name, pan_number=pan,
            date_of_birth=dob.isoformat() if dob else "",
            department=str(college.department.pk),
            assignments=json.dumps([{"subject_id": str(college.subject.pk),
                                     "batch_id": str(college.batch.pk)}]))

    def teacher(self, email="t@x.edu"):
        return User.objects.get(email=email)


class AddingTests(PanFixture):
    def test_a_teacher_is_added_with_a_pan_and_a_date_of_birth(self):
        with verifies():
            response = self.invite(self.a)
        self.assertTrue(self.body(response)["success"], response.content)
        teacher = self.teacher()
        self.assertEqual(teacher.pan_number, PAN)
        self.assertEqual(teacher.date_of_birth, DOB)

    def test_the_pan_is_stored_upper_cased_so_it_cannot_be_stored_twice(self):
        with verifies():
            self.invite(self.a, pan="abcde1234f")
        self.assertEqual(self.teacher().pan_number, PAN)

    def test_a_malformed_pan_is_refused_without_calling_the_provider(self):
        """Format first: a typo should not cost a request to the provider."""
        with patch("core.utils.PAN_verification") as called:
            response = self.invite(self.a, pan="NOTAPAN")
        self.assertFalse(self.body(response)["success"])
        called.assert_not_called()
        self.assertFalse(User.objects.filter(email="t@x.edu").exists())

    def test_a_missing_pan_is_refused(self):
        with verifies():
            response = self.invite(self.a, pan="")
        self.assertFalse(self.body(response)["success"])

    def test_a_missing_date_of_birth_is_refused(self):
        with verifies():
            response = self.invite(self.a, dob=None)
        self.assertFalse(self.body(response)["success"])

    def test_a_date_of_birth_under_eighteen_is_refused(self):
        from django.utils import timezone

        recent = timezone.now().date().replace(
            year=timezone.now().year - 10)
        with verifies():
            response = self.invite(self.a, dob=recent)
        self.assertFalse(self.body(response)["success"])

    def test_no_account_is_created_when_the_gate_refuses(self):
        """
        The gate runs before the account exists. Half-creating a teacher and
        then discovering their PAN is spoken for would leave a row nobody
        asked for.
        """
        with verifies():
            self.invite(self.a)
            self.invite(self.b, email="other@x.edu")
        self.assertFalse(User.objects.filter(email="other@x.edu").exists())


class OneCollegeAtATimeTests(PanFixture):
    """The rule itself: who holds a PAN, and who is therefore blocked."""

    def setUp(self):
        super().setUp()
        with verifies():
            self.invite(self.a)
        self.original = self.teacher()

    def test_an_active_teacher_blocks_another_college(self):
        with verifies():
            response = self.invite(self.b, email="second@x.edu")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(User.objects.filter(email="second@x.edu").exists())

    def test_the_refusal_names_the_college_holding_them(self):
        with verifies():
            body = self.body(self.invite(self.b, email="second@x.edu"))
        self.assertIn("Alpha College", body["message"])
        self.assertIn("ALPHA", body["message"])
        self.assertIn("archive", body["message"].lower())

    def test_an_invited_but_unregistered_teacher_still_holds_it(self):
        """The seat is taken the moment it is offered."""
        self.assertFalse(self.original.registration_completed)
        self.assertEqual(self.original.status, RowStatus.INVITED)
        with verifies():
            self.assertEqual(
                self.invite(self.b, email="second@x.edu").status_code, 403)

    def test_a_revoked_teacher_still_holds_it_until_archived(self):
        """
        The requirement in one test: the first college must archive them, not
        merely let their discipline lapse.
        """
        self.original.is_revoked = True
        self.original.save()
        with verifies():
            self.assertEqual(
                self.invite(self.b, email="second@x.edu").status_code, 403)

    def test_a_suspended_teacher_still_holds_it(self):
        """
        Suspension is a sanction, not a resignation — they are still on the
        first college's books.
        """
        self.original.is_suspended = True
        self.original.save()
        with verifies():
            self.assertEqual(
                self.invite(self.b, email="second@x.edu").status_code, 403)

    def test_an_archived_teacher_releases_it(self):
        self.original.is_active = False
        self.original.save()
        self.assertEqual(self.original.status, RowStatus.ARCHIVED)
        with verifies():
            response = self.invite(self.b, email="second@x.edu")
        self.assertTrue(self.body(response)["success"], response.content)
        self.assertEqual(User.objects.get(email="second@x.edu").pan_number, PAN)

    def test_an_archived_and_revoked_teacher_also_releases_it(self):
        self.original.is_revoked = True
        self.original.is_active = False
        self.original.save()
        with verifies():
            self.assertTrue(self.body(
                self.invite(self.b, email="second@x.edu"))["success"])

    def test_a_different_pan_is_never_blocked(self):
        with verifies():
            self.assertTrue(self.body(
                self.invite(self.b, email="second@x.edu",
                            pan="ZZZZZ9999Z"))["success"])

    def test_a_students_row_with_the_same_pan_does_not_block(self):
        """The rule is about teachers. Only teachers hold a teaching post."""
        User.objects.create_user(
            email="s@x.edu", password="Str0ngPass!23", role=User.Role.STUDENT,
            institute=self.b, pan_number="ZZZZZ9999Z",
            registration_completed=True)
        with verifies():
            self.assertTrue(self.body(
                self.invite(self.b, email="second@x.edu",
                            pan="ZZZZZ9999Z"))["success"])


class ReactivationTests(PanFixture):
    """The same question, asked at the other end."""

    def setUp(self):
        super().setUp()
        with verifies():
            self.invite(self.a)
        self.first = self.teacher()
        self.first.is_active = False
        self.first.save()
        with verifies():
            self.invite(self.b, email="second@x.edu")
        self.second = self.teacher("second@x.edu")

    def test_the_first_college_cannot_reactivate_once_another_has_them(self):
        response = self.call(views.api_teacher_toggle, self.a.head,
                             pk=self.first.pk)
        self.assertEqual(response.status_code, 403)
        self.first.refresh_from_db()
        self.assertFalse(self.first.is_active)

    def test_the_refusal_names_the_college_that_took_them_on(self):
        body = self.body(self.call(views.api_teacher_toggle, self.a.head,
                                   pk=self.first.pk))
        self.assertIn("Beta College", body["message"])

    def test_it_can_be_reactivated_once_the_other_college_archives_them(self):
        self.second.is_active = False
        self.second.save()
        response = self.call(views.api_teacher_toggle, self.a.head,
                             pk=self.first.pk)
        self.assertTrue(self.body(response)["success"], response.content)
        self.first.refresh_from_db()
        self.assertTrue(self.first.is_active)

    def test_archiving_is_never_blocked(self):
        """Releasing somebody is always allowed."""
        response = self.call(views.api_teacher_toggle, self.b.head,
                             pk=self.second.pk)
        self.assertTrue(self.body(response)["success"])

    def test_a_teacher_with_no_pan_on_file_is_not_blocked(self):
        """A row that predates this has nothing to compare."""
        legacy = User.objects.create_user(
            email="legacy@x.edu", password="Str0ngPass!23",
            role=User.Role.TEACHER, institute=self.a,
            department=self.a.department, registration_completed=True)
        legacy.is_active = False
        legacy.save()
        self.assertTrue(self.body(self.call(
            views.api_teacher_toggle, self.a.head, pk=legacy.pk))["success"])


class ImmutabilityTests(PanFixture):
    def setUp(self):
        super().setUp()
        with verifies():
            self.invite(self.a)
        self.t = self.teacher()

    def edit(self, **extra):
        data = {"full_name": "Asha Rao", "phone": "",
                "department": str(self.a.department.pk),
                "assignments": json.dumps(
                    [{"subject_id": str(self.a.subject.pk),
                      "batch_id": str(self.a.batch.pk)}])}
        data.update(extra)
        return self.call(views.api_teacher_assignments_save, self.a.head,
                         pk=self.t.pk, **data)

    def test_the_pan_cannot_be_changed(self):
        response = self.edit(pan_number="ZZZZZ9999Z")
        self.assertEqual(response.status_code, 403)
        self.t.refresh_from_db()
        self.assertEqual(self.t.pan_number, PAN)

    def test_the_date_of_birth_cannot_be_changed(self):
        response = self.edit(date_of_birth="1990-01-01")
        self.assertEqual(response.status_code, 403)
        self.t.refresh_from_db()
        self.assertEqual(self.t.date_of_birth, DOB)

    def test_resubmitting_the_same_values_is_fine(self):
        """The form posts them back; that is not an edit."""
        response = self.edit(pan_number=PAN, date_of_birth=DOB.isoformat())
        self.assertTrue(self.body(response)["success"], response.content)

    def test_posting_back_the_masked_pan_is_not_a_change(self):
        """
        The bug this test exists for: the edit form shows `ABCDE****F` — the
        browser is never sent the whole number — and a readonly input still
        submits. So changing a teacher's *phone number* posted the mask back,
        and the server read it as an attempt to change the PAN and refused a
        save that touched nothing else.
        """
        response = self.edit(phone="9876500011",
                             pan_number=pan_rules.masked(PAN))
        self.assertTrue(self.body(response)["success"], response.content)
        self.t.refresh_from_db()
        self.assertEqual(self.t.pan_number, PAN)
        self.assertEqual(self.t.phone, "9876500011")

    def test_a_masked_value_cannot_be_used_to_slip_a_different_pan_past(self):
        """
        The allowance is narrow: only the mask of *this* teacher's own stored
        PAN. Somebody else's mask is still a change, and still refused.
        """
        response = self.edit(pan_number=pan_rules.masked("ZZZZZ9999Z"))
        self.assertEqual(response.status_code, 403)
        self.t.refresh_from_db()
        self.assertEqual(self.t.pan_number, PAN)

    def test_a_starred_value_is_never_accepted_as_a_new_pan(self):
        """A legacy row cannot acquire a masked value as its real PAN."""
        legacy = User.objects.create_user(
            email="legacy@x.edu", password="Str0ngPass!23",
            role=User.Role.TEACHER, institute=self.a,
            department=self.a.department, full_name="Ravi Nair",
            registration_completed=True)
        self.t = legacy
        with verifies():
            response = self.edit(pan_number="ABCDE****F",
                                 date_of_birth="1980-06-01",
                                 full_name="Ravi Nair")
        self.assertEqual(response.status_code, 403)
        legacy.refresh_from_db()
        self.assertEqual(legacy.pan_number, "")

    def test_omitting_the_pan_entirely_is_fine(self):
        """
        What a *disabled* field produces — the form leaves it out altogether,
        which is the truthful thing to send when there is nothing to change.
        """
        response = self.edit(phone="9876500011")
        self.assertTrue(self.body(response)["success"], response.content)
        self.t.refresh_from_db()
        self.assertEqual(self.t.pan_number, PAN)

    def test_omitting_them_leaves_them_alone(self):
        self.assertTrue(self.body(self.edit())["success"])
        self.t.refresh_from_db()
        self.assertEqual(self.t.pan_number, PAN)

    def test_a_legacy_row_may_have_one_filled_in(self):
        """The only way a pre-existing teacher ever acquires a PAN."""
        legacy = User.objects.create_user(
            email="legacy@x.edu", password="Str0ngPass!23",
            role=User.Role.TEACHER, institute=self.a,
            department=self.a.department, full_name="Ravi Nair",
            registration_completed=True)
        self.t = legacy
        with verifies():
            response = self.edit(pan_number="ZZZZZ9999Z",
                                 date_of_birth="1980-06-01",
                                 full_name="Ravi Nair")
        self.assertTrue(self.body(response)["success"], response.content)
        legacy.refresh_from_db()
        self.assertEqual(legacy.pan_number, "ZZZZZ9999Z")
        self.assertEqual(legacy.date_of_birth, date(1980, 6, 1))

    def test_filling_one_in_is_still_subject_to_the_rule(self):
        legacy = User.objects.create_user(
            email="legacy@x.edu", password="Str0ngPass!23",
            role=User.Role.TEACHER, institute=self.a,
            department=self.a.department, full_name="Ravi Nair",
            registration_completed=True)
        self.t = legacy
        with verifies():
            response = self.edit(pan_number=PAN, date_of_birth="1980-06-01",
                                 full_name="Ravi Nair")
        self.assertEqual(response.status_code, 403)
        legacy.refresh_from_db()
        self.assertEqual(legacy.pan_number, "")


class VerificationTests(PanFixture):
    def test_the_provider_is_called_with_the_pan_name_and_dob(self):
        with verifies() as called:
            self.invite(self.a)
        called.assert_called_once_with(PAN, "Asha Rao", "1985-04-12")

    def test_an_unverified_pan_is_refused_and_nothing_is_saved(self):
        with verifies({"verified": False}):
            response = self.invite(self.a)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(User.objects.filter(email="t@x.edu").exists())

    def test_a_provider_outage_reads_as_an_outage_not_a_rejection(self):
        """
        Two different problems. One is the teacher's, the other is nobody's,
        and telling somebody their PAN is invalid when the provider is down
        sends them to check a card that is perfectly fine.
        """
        with verifies(error=OSError("connection refused")):
            body = self.body(self.invite(self.a))
        self.assertFalse(body["success"])
        self.assertIn("could not be reached", body["message"])
        self.assertNotIn("invalid", body["message"].lower())
        self.assertFalse(User.objects.filter(email="t@x.edu").exists())

    def test_an_empty_response_is_treated_as_unverified(self):
        with verifies({}):
            self.assertEqual(self.invite(self.a).status_code, 403)


class HelperTests(TestCase):
    def test_masking_shows_enough_to_recognise_not_enough_to_copy(self):
        self.assertEqual(pan_rules.masked("ABCDE1234F"), "ABCDE****F")

    def test_masking_leaves_a_malformed_value_alone(self):
        self.assertEqual(pan_rules.masked("SHORT"), "SHORT")

    def test_normalising_strips_spaces_and_upper_cases(self):
        self.assertEqual(pan_rules.normalise(" abcde 1234f "), "ABCDE1234F")

    def test_the_format_check_accepts_a_real_shape(self):
        self.assertEqual(pan_rules.check_format("abcde1234f"), "ABCDE1234F")

    def test_the_format_check_rejects_near_misses(self):
        for bad in ("ABCDE1234", "ABCD01234F", "ABCDE12345", "1BCDE1234F"):
            with self.assertRaises(pan_rules.PanError, msg=bad):
                pan_rules.check_format(bad)


class PayloadTests(PanFixture):
    def setUp(self):
        super().setUp()
        with verifies():
            self.invite(self.a)

    def rows(self, user):
        from django.urls import reverse

        self.client.force_login(user)
        return {r["email"]: r for r in self.client.get(
            reverse("academics:api_teachers"),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest").json()["data"]["rows"]}

    def test_staff_see_the_pan_masked_never_whole(self):
        row = self.rows(self.a.head)["t@x.edu"]
        self.assertEqual(row["pan"], "ABCDE****F")
        self.assertNotIn(PAN, json.dumps(row))
        self.assertTrue(row["has_pan"])

    def test_the_date_of_birth_travels_for_the_edit_form(self):
        row = self.rows(self.a.head)["t@x.edu"]
        self.assertEqual(row["date_of_birth_value"], "1985-04-12")


class OwnNameTests(PanFixture):
    """
    A teacher's name is not theirs to change.

    It was verified against their PAN when the institute added them, and it is
    what a college's records, a suspension notice and any certificate are
    written against. A teacher who could edit it could make the verified
    identity and the account disagree, with nothing downstream noticing.
    """

    def setUp(self):
        super().setUp()
        with verifies():
            self.invite(self.a)
        # Not assigned to `self.teacher` — that is the fixture's *method*, and
        # shadowing it would break the next caller in a way that reads as a
        # missing attribute rather than as this line.
        self.teacher_row = self.teacher()
        self.teacher_row.registration_completed = True
        self.teacher_row.set_password("Str0ngPass!23")
        self.teacher_row.save()

    def post_profile(self, user, **data):
        from django.urls import reverse

        self.client.force_login(user)
        return self.client.post(reverse("accounts:api_profile_update"), data,
                                HTTP_X_REQUESTED_WITH="XMLHttpRequest")

    def test_a_teacher_cannot_change_their_own_name(self):
        response = self.post_profile(self.teacher_row,
                                     full_name="Someone Else", phone="")
        # Accepted, because the *phone* half of the form is legitimate — the
        # name is simply not a field this form has. Silently ignoring beats
        # erroring on a value the UI never offered.
        self.assertEqual(response.status_code, 200)
        self.teacher_row.refresh_from_db()
        self.assertEqual(self.teacher_row.full_name, "Asha Rao")

    def test_a_teacher_may_still_change_their_mobile(self):
        self.post_profile(self.teacher_row, phone="9876500011")
        self.teacher_row.refresh_from_db()
        self.assertEqual(self.teacher_row.phone, "9876500011")

    def test_a_head_may_still_change_their_own_name(self):
        """Nothing was verified against a head's name; it is theirs."""
        self.post_profile(self.a.head, full_name="New Head Name", phone="")
        self.a.head.refresh_from_db()
        self.assertEqual(self.a.head.full_name, "New Head Name")

    def test_the_form_does_not_offer_the_field_to_a_teacher(self):
        from accounts.forms import ProfileForm

        self.assertNotIn("full_name",
                         ProfileForm(instance=self.teacher_row).fields)
        self.assertIn("full_name", ProfileForm(instance=self.a.head).fields)

    def test_the_profile_page_explains_why_it_is_locked(self):
        from django.urls import reverse

        self.client.force_login(self.teacher_row)
        response = self.client.get(reverse("accounts:profile"))
        self.assertContains(response, "verified against your PAN")

    def test_the_institute_can_still_correct_it(self):
        """
        The permission moves, it does not disappear. A head editing a teacher
        from the Teachers page is somebody accountable, and it is logged.
        """
        response = self.call(
            views.api_teacher_assignments_save, self.a.head,
            pk=self.teacher_row.pk, full_name="Asha R. Rao", phone="",
            department=str(self.a.department.pk),
            assignments=json.dumps([{"subject_id": str(self.a.subject.pk),
                                     "batch_id": str(self.a.batch.pk)}]))
        self.assertTrue(self.body(response)["success"], response.content)
        self.teacher_row.refresh_from_db()
        self.assertEqual(self.teacher_row.full_name, "Asha R. Rao")


class InviteAcceptanceNameTests(PanFixture):
    """The same rule one step earlier — the moment nobody is watching."""

    def test_a_teacher_is_not_asked_for_a_name_when_accepting(self):
        from accounts.forms import InviteAcceptForm

        form = InviteAcceptForm(role=User.Role.TEACHER)
        self.assertNotIn("full_name", form.fields)

    def test_everyone_else_still_is(self):
        from accounts.forms import InviteAcceptForm

        for role in (User.Role.HOD, User.Role.STUDENT):
            self.assertIn("full_name", InviteAcceptForm(role=role).fields, role)

    def test_a_posted_name_cannot_overwrite_the_verified_one(self):
        from accounts.forms import InviteAcceptForm

        form = InviteAcceptForm(
            {"full_name": "Someone Else", "password1": "Str0ngPass!23",
             "password2": "Str0ngPass!23"},
            role=User.Role.TEACHER)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertNotIn("full_name", form.cleaned_data)


class StudentVisibilityTests(PanFixture):
    """
    A PAN is a national identifier. The directory a student reads has no
    business carrying one, masked or otherwise.
    """

    def setUp(self):
        super().setUp()
        with verifies():
            self.invite(self.a)
        # Registered, because an unclaimed account is hidden from students
        # anyway — leaving it invited would make this pass for the wrong
        # reason.
        teacher = self.teacher()
        teacher.registration_completed = True
        teacher.full_name = "Asha Rao"
        teacher.save()

        from academics.models import StudentProfile

        self.student = User.objects.create_user(
            email="student@x.edu", password="Str0ngPass!23",
            role=User.Role.STUDENT, institute=self.a,
            department=self.a.department, registration_completed=True,
            face_enrolled=True)
        StudentProfile.objects.create(
            user=self.student, department=self.a.department,
            batch=self.a.batch, class_roll="1")

    def test_a_student_receives_no_pan_and_no_date_of_birth(self):
        from django.urls import reverse

        self.client.force_login(self.student)
        row = {r["email"]: r for r in self.client.get(
            reverse("academics:api_teachers"),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest").json()["data"]["rows"]
        }["t@x.edu"]
        self.assertEqual(row["pan"], "")
        self.assertEqual(row["date_of_birth"], "")
        self.assertEqual(row["date_of_birth_value"], "")
