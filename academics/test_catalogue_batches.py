"""
Batches published by a university, and what an institute may do with them.

The rule mirrors departments but with one deliberate difference, and it is the
one worth reading for: **archiving a cohort does reach the colleges running
it.** A department archived centrally leaves working colleges alone — closing
one mid-term would be unrecoverable. A cohort is different: it is the
university's own record of who is enrolled in which year, and letting it be
archived centrally while twelve colleges still ran it would make the same label
mean two different things depending on who was looking.
"""
import json

from django.test import RequestFactory, TestCase
from django.urls import reverse

from academics import catalogue, catalogue_views, views
from academics.models import (
    Batch,
    Department,
    UniversityBatch,
    UniversityDepartment,
)
from accounts.models import (
    Discipline,
    Institute,
    InstituteAffiliation,
    University,
    UniversityDiscipline,
    User,
)
from core.enums import RowStatus


class BatchFixture(TestCase):
    def setUp(self):
        self.university = University.objects.create(
            name="Engineering University", code="ENGGU", short_name="ENGGU",
            email="e@u.edu", grants_affiliation=True)
        UniversityDiscipline.objects.create(university=self.university,
                                            discipline=Discipline.ENGG)
        self.admin = User.objects.create_user(
            email="admin@u.edu", password="Str0ngPass!23",
            role=User.Role.UNIVERSITY, university=self.university,
            registration_completed=True)
        self.institute = Institute.objects.create(
            name="Acme", code="ACME", email="o@a.edu", status="APPROVED")
        InstituteAffiliation.objects.create(
            institute=self.institute, discipline=Discipline.ENGG,
            university=self.university)
        InstituteAffiliation.objects.create(
            institute=self.institute, discipline=Discipline.GENERAL,
            university=None)
        self.head = User.objects.create_user(
            email="head@a.edu", password="Str0ngPass!23", role=User.Role.HEAD,
            institute=self.institute, registration_completed=True)

        self.entry = UniversityDepartment.objects.create(
            university=self.university, discipline=Discipline.ENGG,
            name="Computer Science", code="CSE")
        self.adopted = catalogue.adopt(institute=self.institute,
                                       entry=self.entry)
        # A department of the college's own, for the contrast.
        self.own = Department.objects.create(
            institute=self.institute, code="ARTS", name="Arts",
            discipline=Discipline.GENERAL)

    def call(self, view, user, pk=None, **data):
        request = RequestFactory().post("/", data)
        request.user = user
        request.session = self.client.session
        request.META["HTTP_X_REQUESTED_WITH"] = "XMLHttpRequest"
        return view(request, pk=pk) if pk is not None else view(request)

    def body(self, response):
        return json.loads(response.content)


class PublishTests(BatchFixture):
    def test_publishing_reaches_every_college_running_the_department(self):
        response = self.call(catalogue_views.api_batch_save, self.admin,
                             department=str(self.entry.pk), label="2022-26",
                             status="ACTIVE")
        self.assertTrue(self.body(response)["success"], response.content)
        copy = Batch.objects.get(department=self.adopted)
        self.assertEqual(copy.label, "2022-26")
        self.assertIsNotNone(copy.source_id)

    def test_the_label_is_normalised_so_two_spellings_are_one_batch(self):
        """"2022-2026" and "2022-26" are the same cohort to a registrar."""
        self.call(catalogue_views.api_batch_save, self.admin,
                  department=str(self.entry.pk), label="2022-2026",
                  status="ACTIVE")
        self.assertEqual(UniversityBatch.objects.get().label, "2022-26")

    def test_a_malformed_label_is_refused(self):
        response = self.call(catalogue_views.api_batch_save, self.admin,
                             department=str(self.entry.pk), label="next year",
                             status="ACTIVE")
        body = self.body(response)
        self.assertFalse(body["success"])
        self.assertIn("label", body["errors"])

    def test_another_universitys_department_cannot_be_published_into(self):
        other = University.objects.create(name="O", code="O", email="o@o.edu")
        theirs = UniversityDepartment.objects.create(
            university=other, discipline=Discipline.ENGG, name="X", code="XX")
        response = self.call(catalogue_views.api_batch_save, self.admin,
                             department=str(theirs.pk), label="2022-26",
                             status="ACTIVE")
        self.assertFalse(self.body(response)["success"])

    def test_the_label_cannot_change_once_a_college_runs_it(self):
        self.call(catalogue_views.api_batch_save, self.admin,
                  department=str(self.entry.pk), label="2022-26",
                  status="ACTIVE")
        published = UniversityBatch.objects.get()
        response = self.call(catalogue_views.api_batch_save, self.admin,
                             pk=published.pk, department=str(self.entry.pk),
                             label="2023-27", status="ACTIVE")
        body = self.body(response)
        self.assertFalse(body["success"])
        self.assertIn("label", body["errors"])


class ArchiveTests(BatchFixture):
    def setUp(self):
        super().setUp()
        UniversityBatch.objects.create(department=self.entry, label="2022-26",
                                       start_year=2022, end_year=2026)
        catalogue.propagate(self.entry)
        self.published = UniversityBatch.objects.get()
        self.copy = Batch.objects.get(department=self.adopted)

    def test_archiving_centrally_archives_it_at_the_colleges_too(self):
        """
        The deliberate difference from departments. A cohort archived centrally
        but still running at twelve colleges would make one label mean two
        things.
        """
        self.call(catalogue_views.api_batch_toggle, self.admin,
                  pk=self.published.pk)
        self.copy.refresh_from_db()
        self.assertEqual(self.copy.status, RowStatus.ARCHIVED)
        self.assertFalse(self.copy.is_active)

    def test_nothing_is_deleted_and_restoring_brings_it_back(self):
        self.call(catalogue_views.api_batch_toggle, self.admin,
                  pk=self.published.pk)
        self.assertTrue(Batch.objects.filter(pk=self.copy.pk).exists())
        self.call(catalogue_views.api_batch_toggle, self.admin,
                  pk=self.published.pk)
        self.copy.refresh_from_db()
        self.assertEqual(self.copy.status, RowStatus.ACTIVE)

    def test_the_answer_says_how_many_it_reached(self):
        body = self.body(self.call(catalogue_views.api_batch_toggle,
                                   self.admin, pk=self.published.pk))
        self.assertIn("1 institute", body["message"])
        self.assertIn("Nothing was deleted", body["message"])

    def test_it_cannot_be_deleted_while_a_college_runs_it(self):
        response = self.call(catalogue_views.api_batch_delete, self.admin,
                             pk=self.published.pk)
        self.assertFalse(self.body(response)["success"])


class InstituteViewTests(BatchFixture):
    """What the college may do with what it has been given."""

    def setUp(self):
        super().setUp()
        UniversityBatch.objects.create(department=self.entry, label="2022-26",
                                       start_year=2022, end_year=2026)
        catalogue.propagate(self.entry)
        self.given = Batch.objects.get(department=self.adopted)
        self.mine = Batch.objects.create(
            department=self.own, label="2022-25",
            start_year=2022, end_year=2025)

    def rows(self):
        self.client.force_login(self.head)
        return {r["label"]: r for r in self.client.get(
            reverse("academics:api_batches"),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest").json()["data"]["rows"]}

    def test_a_published_batch_reads_as_read_only_and_names_who_owns_it(self):
        row = self.rows()["2022-26"]
        self.assertTrue(row["read_only"])
        self.assertEqual(row["owner"], "ENGGU")

    def test_the_colleges_own_batch_is_editable(self):
        row = self.rows()["2022-25"]
        self.assertFalse(row["read_only"])
        self.assertEqual(row["owner"], "")

    def test_the_college_cannot_edit_a_published_batch(self):
        response = self.call(views.api_batch_save, self.head, pk=self.given.pk,
                             label="2022-26", department=str(self.adopted.pk),
                             is_active="on")
        self.assertEqual(response.status_code, 403)

    def test_the_college_cannot_archive_a_published_batch(self):
        response = self.call(views.api_batch_toggle, self.head,
                             pk=self.given.pk)
        self.assertEqual(response.status_code, 403)
        self.given.refresh_from_db()
        self.assertEqual(self.given.status, RowStatus.ACTIVE)

    def test_the_college_can_edit_its_own(self):
        response = self.call(views.api_batch_save, self.head, pk=self.mine.pk,
                             label="2022-25", department=str(self.own.pk),
                             is_active="on")
        self.assertTrue(self.body(response)["success"], response.content)

    def test_the_college_cannot_add_a_batch_to_an_adopted_department(self):
        """
        A cohort the university has not published would exist at one college
        and nowhere else — the disagreement the catalogue exists to prevent.
        """
        response = self.call(views.api_batch_save, self.head,
                             department=str(self.adopted.pk), label="2024-28",
                             is_active="on")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Batch.objects.filter(label="2024-28").exists())

    def test_the_college_can_add_one_to_its_own_department(self):
        response = self.call(views.api_batch_save, self.head,
                             department=str(self.own.pk), label="2024-28",
                             is_active="on")
        self.assertTrue(self.body(response)["success"], response.content)


class DropdownTests(BatchFixture):
    """
    What the New-batch dropdown offers.

    It should list exactly what the save endpoint accepts. Offering an adopted
    department would be offering a choice that comes back refused, with the
    reason arriving only after the person had filled the form in.
    """

    def page(self, user=None):
        self.client.force_login(user or self.head)
        return self.client.get(reverse("academics:batches"))

    def test_an_adopted_department_is_not_offered(self):
        response = self.page()
        offered = {d.pk for d in response.context["departments"]}
        self.assertNotIn(self.adopted.pk, offered)

    def test_the_colleges_own_department_is_offered(self):
        self.assertIn(self.own.pk,
                      {d.pk for d in self.page().context["departments"]})

    def test_a_grandfathered_department_is_still_offered(self):
        """
        Created in an affiliated discipline back when that was allowed. It has
        no catalogue link, so it stays the institute's — locking it would
        strand any college with attendance running against it.
        """
        legacy = Department.objects.create(
            institute=self.institute, code="ECE", name="Electronics",
            discipline=Discipline.ENGG)
        self.assertIn(legacy.pk,
                      {d.pk for d in self.page().context["departments"]})

    def test_the_filter_above_the_table_still_shows_everything(self):
        """You look at what you cannot edit; you just cannot file under it."""
        self.assertIn(self.adopted.pk,
                      {d.pk for d in self.page().context["filter_departments"]})

    def test_the_button_is_hidden_when_every_department_is_adopted(self):
        self.own.delete()
        response = self.page()
        self.assertFalse(response.context["can_add"])
        self.assertContains(response, "publishes the cohorts for them")

    def test_the_dropdown_matches_what_the_server_accepts(self):
        """
        The guard and the dropdown must agree. If they drift, one of them is
        lying to the person: either a refused option is on offer, or an
        allowed department is missing from it.
        """
        for department in [self.adopted, self.own]:
            offered = department.pk in {
                d.pk for d in self.page().context["departments"]}
            response = self.call(views.api_batch_save, self.head,
                                 department=str(department.pk),
                                 label="2025-29", is_active="on")
            accepted = response.status_code == 200
            self.assertEqual(offered, accepted,
                             f"{department.code}: offered={offered} "
                             f"accepted={accepted}")
            Batch.objects.filter(label="2025-29").delete()
