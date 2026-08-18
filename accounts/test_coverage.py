"""
A university adding and withdrawing the disciplines it awards.

The interesting half is withdrawal, because three different things sit inside
a covered discipline and they are not equally the university's to dispose of.
The tests are grouped by which of the three they are about.
"""
import json

from django.test import RequestFactory, TestCase
from django.urls import reverse

from academics import catalogue
from academics.models import (
    Batch,
    Department,
    Subject,
    UniversityBatch,
    UniversityDepartment,
    UniversitySubject,
)
from accounts import coverage, views
from accounts.models import (
    Discipline,
    Institute,
    InstituteAffiliation,
    University,
    UniversityDiscipline,
    User,
)
from core.enums import RowStatus


class CoverageFixture(TestCase):
    def setUp(self):
        self.university = University.objects.create(
            name="Engineering University", code="ENGGU", short_name="ENGGU",
            email="e@u.edu", grants_affiliation=True)
        for code in (Discipline.ENGG, Discipline.PHARMACY):
            UniversityDiscipline.objects.create(university=self.university,
                                                discipline=code)
        self.admin = User.objects.create_user(
            email="admin@u.edu", password="Str0ngPass!23",
            role=User.Role.UNIVERSITY, university=self.university,
            registration_completed=True)

        self.institute = Institute.objects.create(
            name="Acme", code="ACME", email="o@a.edu", status="APPROVED")
        InstituteAffiliation.objects.create(
            institute=self.institute, discipline=Discipline.ENGG,
            university=self.university)
        self.head = User.objects.create_user(
            email="head@a.edu", password="Str0ngPass!23", role=User.Role.HEAD,
            institute=self.institute, registration_completed=True)

        self.entry = UniversityDepartment.objects.create(
            university=self.university, discipline=Discipline.ENGG,
            name="Computer Science", code="CSE")
        UniversityBatch.objects.create(department=self.entry, label="2022-26",
                                       start_year=2022, end_year=2026)
        UniversitySubject.objects.create(department=self.entry, code="DSA",
                                         name="Data Structures", semester=3)
        self.adopted = catalogue.adopt(institute=self.institute,
                                       entry=self.entry)

    def call(self, view, user, code=None, **data):
        request = RequestFactory().post("/", data)
        request.user = user
        request.session = self.client.session
        request.META["HTTP_X_REQUESTED_WITH"] = "XMLHttpRequest"
        return view(request, code=code) if code else view(request)

    def body(self, response):
        return json.loads(response.content)


class AddTests(CoverageFixture):
    def test_a_discipline_is_added(self):
        response = self.call(views.api_add_coverage, self.admin,
                             disciplines=[Discipline.MEDICAL])
        self.assertTrue(self.body(response)["success"], response.content)
        self.assertTrue(self.university.disciplines.filter(
            discipline=Discipline.MEDICAL).exists())

    def test_several_at_once(self):
        self.call(views.api_add_coverage, self.admin,
                  disciplines=[Discipline.MEDICAL, Discipline.AGRI])
        self.assertEqual(self.university.disciplines.count(), 4)

    def test_one_already_on_file_is_reported_not_raised_on(self):
        """
        A person ticking four boxes of which one was already there meant to end
        up with all four, not with an error and nothing saved.
        """
        response = self.call(views.api_add_coverage, self.admin,
                             disciplines=[Discipline.ENGG, Discipline.MEDICAL])
        body = self.body(response)
        self.assertTrue(body["success"])
        self.assertEqual(body["data"]["added"], ["Medical, Health Sciences, "
                                                 "Ayush, Nursing & Paramedical"])
        self.assertTrue(body["data"]["existing"])
        self.assertTrue(self.university.disciplines.filter(
            discipline=Discipline.MEDICAL).exists())

    def test_an_unknown_code_is_refused(self):
        response = self.call(views.api_add_coverage, self.admin,
                             disciplines=["ASTROLOGY"])
        self.assertFalse(self.body(response)["success"])
        self.assertEqual(self.university.disciplines.count(), 2)

    def test_an_empty_choice_is_refused(self):
        self.assertFalse(self.body(
            self.call(views.api_add_coverage, self.admin))["success"])

    def test_an_institute_head_cannot_change_a_universitys_coverage(self):
        response = self.call(views.api_add_coverage, self.head,
                             disciplines=[Discipline.MEDICAL])
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.university.disciplines.count(), 2)

    def test_the_add_list_offers_only_what_is_not_covered(self):
        offered = {d["value"] for d in coverage.available(self.university)}
        self.assertNotIn(Discipline.ENGG, offered)
        self.assertIn(Discipline.MEDICAL, offered)


class CountsTests(CoverageFixture):
    def test_the_counts_describe_the_catalogue_and_the_colleges(self):
        counts = coverage.contents_of(self.university, Discipline.ENGG)
        self.assertEqual(counts["departments"], 1)
        self.assertEqual(counts["batches"], 1)
        self.assertEqual(counts["subjects"], 1)
        self.assertEqual(counts["institutes"], 1)

    def test_an_already_archived_entry_is_not_counted(self):
        """It is not something the person is about to lose."""
        UniversitySubject.objects.update(status=RowStatus.ARCHIVED)
        self.assertEqual(
            coverage.contents_of(self.university, Discipline.ENGG)["subjects"],
            0)

    def test_an_empty_discipline_counts_zero_rather_than_failing(self):
        counts = coverage.contents_of(self.university, Discipline.PHARMACY)
        self.assertEqual(set(counts.values()), {0})

    def test_the_endpoint_reports_them(self):
        self.client.force_login(self.admin)
        data = self.client.get(
            reverse("accounts:api_coverage_contents", args=[Discipline.ENGG]),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest").json()["data"]
        self.assertEqual(data["counts"]["institutes"], 1)
        self.assertFalse(data["only_one"])


class WithdrawTests(CoverageFixture):
    """The three things inside a discipline, one group of tests each."""

    def withdraw(self, code=Discipline.ENGG, contents="keep"):
        return self.call(views.api_remove_coverage, self.admin,
                         discipline=code, contents=contents)

    # 1. The university's own catalogue — the part it is asked about.
    def test_keeping_leaves_the_catalogue_exactly_as_it_was(self):
        self.assertTrue(self.body(self.withdraw(contents="keep"))["success"])
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.status, RowStatus.ACTIVE)
        self.assertEqual(UniversitySubject.objects.get().status,
                         RowStatus.ACTIVE)

    def test_archiving_marks_the_catalogue_and_deletes_nothing(self):
        self.assertTrue(self.body(self.withdraw(contents="archive"))["success"])
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.status, RowStatus.ARCHIVED)
        self.assertEqual(UniversityBatch.objects.get().status,
                         RowStatus.ARCHIVED)
        self.assertEqual(UniversitySubject.objects.get().status,
                         RowStatus.ARCHIVED)
        self.assertEqual(UniversityDepartment.objects.count(), 1)

    def test_the_choice_is_required_rather_than_defaulted(self):
        """
        Neither answer is one a person should inherit by clicking through a
        modal.
        """
        response = self.withdraw(contents="")
        self.assertFalse(self.body(response)["success"])
        self.assertTrue(self.university.disciplines.filter(
            discipline=Discipline.ENGG).exists())

    # 2. The affiliated colleges — delinked, not asked about.
    def test_the_college_becomes_autonomous_in_it(self):
        self.withdraw()
        affiliation = InstituteAffiliation.objects.get(
            institute=self.institute, discipline=Discipline.ENGG)
        self.assertIsNone(affiliation.university_id)

    def test_the_affiliation_row_is_not_deleted(self):
        """
        Dropping it would claim the college had stopped teaching engineering,
        which is not what happened.
        """
        self.withdraw()
        self.assertTrue(InstituteAffiliation.objects.filter(
            institute=self.institute, discipline=Discipline.ENGG).exists())

    def test_delinking_happens_even_when_the_catalogue_is_kept(self):
        self.withdraw(contents="keep")
        self.assertIsNone(InstituteAffiliation.objects.get(
            institute=self.institute).university_id)

    def test_the_answer_says_how_many_colleges_it_reached(self):
        body = self.body(self.withdraw())
        self.assertEqual(body["data"]["delinked"], 1)
        self.assertIn("1 institute(s) now autonomous", body["message"])
        self.assertIn("Nothing was deleted", body["message"])

    # 3. The colleges' own data — untouched, and not on offer.
    def test_the_colleges_department_keeps_running(self):
        self.withdraw(contents="archive")
        self.adopted.refresh_from_db()
        self.assertTrue(self.adopted.is_active)
        self.assertEqual(self.adopted.status, RowStatus.ACTIVE)

    def test_the_colleges_batches_and_subjects_are_not_archived_with_it(self):
        self.withdraw(contents="archive")
        self.assertTrue(Batch.objects.get(department=self.adopted).is_active)
        self.assertTrue(Subject.objects.get(department=self.adopted).is_active)

    def test_the_department_becomes_the_colleges_own_to_edit(self):
        """
        `release` clears `source`, so the wing carries on under new ownership
        rather than being locked to a university that has walked away.
        """
        from academics.curriculum import is_read_only

        self.withdraw()
        self.adopted.refresh_from_db()
        self.assertIsNone(self.adopted.source_id)
        self.assertFalse(is_read_only(
            Subject.objects.get(department=self.adopted), self.head))

    # Refusals.
    def test_the_last_discipline_cannot_be_withdrawn(self):
        self.university.disciplines.filter(
            discipline=Discipline.PHARMACY).delete()
        response = self.withdraw()
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.university.disciplines.count(), 1)

    def test_a_discipline_not_on_file_is_refused(self):
        self.assertEqual(self.withdraw(code=Discipline.AGRI).status_code, 403)

    def test_an_unknown_code_is_refused(self):
        self.assertEqual(self.withdraw(code="ASTROLOGY").status_code, 403)

    def test_an_institute_head_cannot_withdraw_a_universitys_discipline(self):
        response = self.call(views.api_remove_coverage, self.head,
                             discipline=Discipline.ENGG, contents="archive")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.university.disciplines.count(), 2)

    def test_another_universitys_coverage_is_untouched(self):
        other = University.objects.create(name="O", code="OTH", email="o@o.edu")
        UniversityDiscipline.objects.create(university=other,
                                            discipline=Discipline.ENGG)
        self.withdraw()
        self.assertTrue(other.disciplines.filter(
            discipline=Discipline.ENGG).exists())


class ProfilePageTests(CoverageFixture):
    def test_the_page_lists_what_the_university_awards(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("accounts:profile"))
        rows = {r["discipline"]: r for r in response.context["coverage_rows"]}
        self.assertEqual(rows[Discipline.ENGG]["departments"], 1)
        self.assertEqual(rows[Discipline.ENGG]["institutes"], 1)
        self.assertEqual(rows[Discipline.PHARMACY]["departments"], 0)

    def test_the_counts_do_not_multiply_each_other(self):
        """
        Two grouped queries, not two filtered annotations on one queryset.
        Several filtered `Count`s over different relations fan out on MongoDB
        and inflate every figure — a bug this project has had twice.
        """
        UniversityDepartment.objects.create(
            university=self.university, discipline=Discipline.ENGG,
            name="Electronics", code="ECE")
        second = Institute.objects.create(name="B", code="BEE", email="b@b.edu",
                                          status="APPROVED")
        InstituteAffiliation.objects.create(
            institute=second, discipline=Discipline.ENGG,
            university=self.university)
        rows = {r["discipline"]: r
                for r in coverage.rows_for(self.university)}
        self.assertEqual(rows[Discipline.ENGG]["departments"], 2)
        self.assertEqual(rows[Discipline.ENGG]["institutes"], 2)

    def test_an_institute_head_sees_no_coverage_card(self):
        self.client.force_login(self.head)
        response = self.client.get(reverse("accounts:profile"))
        self.assertEqual(response.context["coverage_rows"], [])
        self.assertNotContains(response, "Disciplines you award")

    def test_the_university_sees_the_card_and_the_add_form(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("accounts:profile"))
        self.assertContains(response, "Disciplines you award")
        self.assertContains(response, "coverage-form")
