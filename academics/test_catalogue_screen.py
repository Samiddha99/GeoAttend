"""
The university's Departments screen.

The university publishes; it does not reach into anyone's college. Most of what
is asserted here is that separation holding under the kinds of edit that would
break it — renaming after adoption, archiving something in use, deleting
something a college is running.
"""
import json

from django.test import RequestFactory, TestCase
from django.urls import reverse

from academics import catalogue, catalogue_views
from academics.models import Department, UniversityDepartment
from accounts.models import (
    Discipline,
    Institute,
    InstituteAffiliation,
    University,
    UniversityDiscipline,
    User,
)
from core.enums import RowStatus


class ScreenFixture(TestCase):
    def setUp(self):
        self.university = University.objects.create(
            name="Engineering University", code="ENGGU", short_name="ENGGU",
            email="e@u.edu", grants_affiliation=True)
        # Only engineering: the discipline list is narrowed to what this
        # university actually grants, so pharmacy must not be offered.
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
        self.head = User.objects.create_user(
            email="head@a.edu", password="Str0ngPass!23", role=User.Role.HEAD,
            institute=self.institute, registration_completed=True)
        self.client.force_login(self.admin)

    def post(self, url_name, *args, **data):
        """
        `data` as keywords, but the URL name positionally — the form has a
        `name` field of its own and a parameter called `name` collided with it.
        """
        return self.client.post(reverse(url_name, args=args), data,
                                HTTP_X_REQUESTED_WITH="XMLHttpRequest")

    def call(self, view, pk=None, **data):
        """
        Per-row endpoints, called directly rather than through `reverse`.

        Their URLs use the `<oid:pk>` converter — 24 hex characters, right for
        a MongoDB `_id` and impossible for the integer keys sqlite gives this
        harness. Widening the converter for the test run breaks routing for
        unrelated tests, so the view is what gets called; the authorisation
        being tested lives there anyway.
        """
        request = RequestFactory().post("/", data)
        request.user = self.admin
        request.session = self.client.session
        request.META["HTTP_X_REQUESTED_WITH"] = "XMLHttpRequest"
        return view(request, pk=pk) if pk is not None else view(request)

    def body(self, response):
        return json.loads(response.content)

    def rows(self):
        return self.client.get(
            reverse("academics:api_catalogue_departments"),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest").json()["data"]["rows"]


class PageTests(ScreenFixture):
    def test_the_page_loads_and_offers_only_granted_disciplines(self):
        response = self.client.get(reverse("academics:catalogue_departments"))
        self.assertEqual(response.status_code, 200)
        offered = {d["value"] for d in response.context["disciplines"]}
        self.assertEqual(offered, {"ENGG"})

    def test_the_nav_links_to_it_for_a_university(self):
        html = self.client.get(reverse("academics:catalogue_departments")).content.decode()
        self.assertIn(reverse("academics:catalogue_departments"), html)

    def test_an_institute_head_cannot_reach_it(self):
        self.client.force_login(self.head)
        self.assertEqual(
            self.client.get(reverse("academics:catalogue_departments")).status_code,
            403)

    def test_a_university_granting_nothing_is_told_rather_than_shown_an_empty_box(self):
        UniversityDiscipline.objects.all().delete()
        response = self.client.get(reverse("academics:catalogue_departments"))
        self.assertFalse(response.context["can_publish"])


class PublishTests(ScreenFixture):
    def test_publishing_creates_an_entry_and_nothing_at_any_institute(self):
        response = self.post("academics:api_catalogue_department_create",
                             discipline="ENGG", name="Computer Science",
                             code="cse", status="ACTIVE")
        self.assertTrue(self.body(response)["success"], response.content)
        entry = UniversityDepartment.objects.get()
        self.assertEqual(entry.code, "CSE")          # slugified upper
        self.assertEqual(entry.university, self.university)
        # The direction that matters: publishing reaches nobody until adopted.
        self.assertEqual(Department.objects.count(), 0)

    def test_a_discipline_the_university_does_not_grant_is_refused(self):
        """
        The dropdown never offers it, so this can only arrive by hand — and a
        department nobody can adopt would be a row that exists for nobody.
        """
        response = self.post("academics:api_catalogue_department_create",
                             discipline="PHARMACY", name="Pharmacy",
                             code="PHM", status="ACTIVE")
        self.assertFalse(self.body(response)["success"])
        self.assertEqual(UniversityDepartment.objects.count(), 0)

    def test_two_departments_cannot_share_a_code_in_one_discipline(self):
        for _ in range(2):
            response = self.post("academics:api_catalogue_department_create",
                                 discipline="ENGG", name="CSE", code="CSE",
                                 status="ACTIVE")
        self.assertFalse(self.body(response)["success"])
        self.assertEqual(UniversityDepartment.objects.count(), 1)

    def test_the_rows_report_uptake(self):
        self.post("academics:api_catalogue_department_create",
                  discipline="ENGG", name="CSE", code="CSE", status="ACTIVE")
        self.assertEqual(self.rows()[0]["adoptions"], 0)
        catalogue.adopt(institute=self.institute,
                        entry=UniversityDepartment.objects.get())
        self.assertEqual(self.rows()[0]["adoptions"], 1)


class EditTests(ScreenFixture):
    def setUp(self):
        super().setUp()
        self.entry = UniversityDepartment.objects.create(
            university=self.university, discipline=Discipline.ENGG,
            name="Computer Science", code="CSE")

    def test_renaming_is_allowed_and_reaches_nobody_destructively(self):
        response = self.call(catalogue_views.api_department_save,
                             pk=self.entry.pk, discipline="ENGG",
                             name="Computer Science & Engineering",
                             code="CSE", status="ACTIVE")
        self.assertTrue(self.body(response)["success"], response.content)
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.name, "Computer Science & Engineering")

    def test_the_code_cannot_change_once_a_college_runs_it(self):
        """
        The copies were matched on the code. Changing it would orphan every one
        of them and the next propagation would create a second department
        beside each original.
        """
        catalogue.adopt(institute=self.institute, entry=self.entry)
        response = self.call(catalogue_views.api_department_save,
                             pk=self.entry.pk, discipline="ENGG", name="CSE",
                             code="COMP", status="ACTIVE")
        body = self.body(response)
        self.assertFalse(body["success"])
        self.assertIn("code", body["errors"])
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.code, "CSE")

    def test_the_code_can_change_before_anybody_adopts(self):
        response = self.call(catalogue_views.api_department_save,
                             pk=self.entry.pk, discipline="ENGG", name="CSE",
                             code="COMP", status="ACTIVE")
        self.assertTrue(self.body(response)["success"], response.content)
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.code, "COMP")

    def test_another_universitys_entry_is_not_reachable(self):
        other = University.objects.create(name="O", code="O", email="o@o.edu")
        theirs = UniversityDepartment.objects.create(
            university=other, discipline=Discipline.ENGG, name="X", code="XX")
        from django.http import Http404

        with self.assertRaises(Http404):
            self.call(catalogue_views.api_department_save, pk=theirs.pk,
                      discipline="ENGG", name="Hijacked", code="XX",
                      status="ACTIVE")


class WithdrawTests(ScreenFixture):
    def setUp(self):
        super().setUp()
        self.entry = UniversityDepartment.objects.create(
            university=self.university, discipline=Discipline.ENGG,
            name="Computer Science", code="CSE")
        self.department = catalogue.adopt(institute=self.institute,
                                          entry=self.entry)

    def test_archiving_withdraws_it_from_the_adoption_list(self):
        self.call(catalogue_views.api_department_toggle, pk=self.entry.pk)
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.status, RowStatus.ARCHIVED)
        self.assertEqual(
            list(catalogue.choices_for(self.institute, Discipline.ENGG)), [])

    def test_archiving_leaves_the_colleges_already_running_it_alone(self):
        """
        A university retiring a syllabus is not asking to close a working
        department mid-term, and there would be no way to undo that.
        """
        self.call(catalogue_views.api_department_toggle, pk=self.entry.pk)
        self.department.refresh_from_db()
        self.assertEqual(self.department.status, RowStatus.ACTIVE)

    def test_the_answer_says_how_many_are_unaffected(self):
        body = self.body(
            self.call(catalogue_views.api_department_toggle, pk=self.entry.pk))
        self.assertIn("untouched", body["message"])

    def test_it_cannot_be_deleted_while_a_college_runs_it(self):
        response = self.call(catalogue_views.api_department_delete,
                             pk=self.entry.pk)
        self.assertFalse(self.body(response)["success"])
        self.assertTrue(UniversityDepartment.objects.filter(pk=self.entry.pk).exists())

    def test_it_can_be_deleted_when_nobody_does(self):
        self.department.delete()
        response = self.call(catalogue_views.api_department_delete,
                             pk=self.entry.pk)
        self.assertTrue(self.body(response)["success"], response.content)
        self.assertFalse(UniversityDepartment.objects.exists())
