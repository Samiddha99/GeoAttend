"""
Subjects published by a university, and what an institute may do with them.

Same shape as the batch rules, and archiving behaves the same way: it reaches
the colleges. A paper withdrawn from the syllabus centrally but still taught at
nine colleges is not a withdrawn paper, and the marks would have nowhere to go
at the end of term.
"""
import json

from django.test import RequestFactory, TestCase
from django.urls import reverse

from academics import catalogue, catalogue_views, views
from academics.models import (
    Department,
    Subject,
    UniversityDepartment,
    UniversitySubject,
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


class SubjectFixture(TestCase):
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

    def publish(self, **overrides):
        fields = {"department": str(self.entry.pk), "semester": "3",
                  "code": "DSA", "name": "Data Structures",
                  "degree": "BACHELOR", "subject_type": "THEORY",
                  "credits": "4", "status": "ACTIVE"}
        fields.update(overrides)
        return self.call(catalogue_views.api_subject_save, self.admin, **fields)


class PublishTests(SubjectFixture):
    def test_publishing_reaches_every_college_running_the_department(self):
        self.assertTrue(self.body(self.publish())["success"])
        copy = Subject.objects.get(department=self.adopted)
        self.assertEqual((copy.code, copy.semester, copy.credits),
                         ("DSA", 3, 4))
        self.assertIsNotNone(copy.source_id)

    def test_the_code_is_upper_cased_so_two_spellings_are_one_subject(self):
        self.publish(code="dsa")
        self.assertEqual(UniversitySubject.objects.get().code, "DSA")

    def test_a_syllabus_correction_reaches_the_colleges(self):
        self.publish()
        entry = UniversitySubject.objects.get()
        self.call(catalogue_views.api_subject_save, self.admin, pk=entry.pk,
                  department=str(self.entry.pk), semester="3", code="DSA",
                  name="Data Structures & Algorithms", degree="BACHELOR",
                  subject_type="THEORY", credits="5", status="ACTIVE")
        self.assertEqual(UniversitySubject.objects.count(), 1)
        copy = Subject.objects.get(department=self.adopted)
        self.assertEqual(copy.name, "Data Structures & Algorithms")
        self.assertEqual(copy.credits, 5)
        self.assertEqual(copy.source_id, entry.pk)

    def test_the_semester_can_be_moved_and_that_reaches_them_too(self):
        self.publish()
        entry = UniversitySubject.objects.get()
        self.call(catalogue_views.api_subject_save, self.admin, pk=entry.pk,
                  department=str(self.entry.pk), semester="4", code="DSA",
                  name="Data Structures", degree="BACHELOR",
                  subject_type="THEORY", credits="4", status="ACTIVE")
        self.assertEqual(Subject.objects.get(department=self.adopted).semester, 4)

    def test_a_second_paper_with_the_same_code_is_refused(self):
        self.publish()
        entry = UniversitySubject.objects.get()
        response = self.call(
            catalogue_views.api_subject_save, self.admin,
            department=str(self.entry.pk), semester="5", code="DSA",
            name="Something else", degree="BACHELOR", subject_type="THEORY",
            credits="3", status="ACTIVE")
        body = self.body(response)
        self.assertFalse(body["success"])
        # Against the code box, not in the footer as a non-field error: the
        # code is the thing the person has to change.
        self.assertIn("code", body["errors"])
        self.assertEqual(UniversitySubject.objects.count(), 1)
        self.assertEqual(UniversitySubject.objects.get().pk, entry.pk)

    def test_another_universitys_department_cannot_be_published_into(self):
        other = University.objects.create(name="O", code="O", email="o@o.edu")
        theirs = UniversityDepartment.objects.create(
            university=other, discipline=Discipline.ENGG, name="X", code="XX")
        self.assertFalse(self.body(self.publish(department=str(theirs.pk)))["success"])

    def test_the_code_cannot_change_once_a_college_teaches_it(self):
        self.publish()
        entry = UniversitySubject.objects.get()
        body = self.body(self.call(
            catalogue_views.api_subject_save, self.admin, pk=entry.pk,
            department=str(self.entry.pk), semester="3", code="DSA2",
            name="Data Structures", degree="BACHELOR", subject_type="THEORY",
            credits="4", status="ACTIVE"))
        self.assertFalse(body["success"])
        self.assertIn("code", body["errors"])

    def test_the_degree_and_type_are_real_choices_not_free_text(self):
        """
        They were plain CharFields, which produced a text box where every
        other screen has a dropdown — and a display method that returned the
        raw code.
        """
        self.publish(degree="Bachelors of nothing")
        self.assertEqual(UniversitySubject.objects.count(), 0)
        self.publish()
        self.assertEqual(UniversitySubject.objects.get().get_degree_display(),
                         "Bachelor")


class ArchiveTests(SubjectFixture):
    def setUp(self):
        super().setUp()
        self.publish()
        self.entry_subject = UniversitySubject.objects.get()
        self.copy = Subject.objects.get(department=self.adopted)

    def test_archiving_centrally_archives_it_at_the_colleges_too(self):
        self.call(catalogue_views.api_subject_toggle, self.admin,
                  pk=self.entry_subject.pk)
        self.copy.refresh_from_db()
        self.assertEqual(self.copy.status, RowStatus.ARCHIVED)
        self.assertFalse(self.copy.is_active)

    def test_nothing_is_deleted_and_restoring_brings_it_back(self):
        self.call(catalogue_views.api_subject_toggle, self.admin,
                  pk=self.entry_subject.pk)
        self.assertTrue(Subject.objects.filter(pk=self.copy.pk).exists())
        self.call(catalogue_views.api_subject_toggle, self.admin,
                  pk=self.entry_subject.pk)
        self.copy.refresh_from_db()
        self.assertEqual(self.copy.status, RowStatus.ACTIVE)
        self.assertTrue(self.copy.is_active)

    def test_the_answer_says_how_many_it_reached(self):
        body = self.body(self.call(catalogue_views.api_subject_toggle,
                                   self.admin, pk=self.entry_subject.pk))
        self.assertIn("1 institute", body["message"])
        self.assertIn("Nothing was deleted", body["message"])

    def test_it_cannot_be_deleted_while_a_college_teaches_it(self):
        response = self.call(catalogue_views.api_subject_delete, self.admin,
                             pk=self.entry_subject.pk)
        self.assertFalse(self.body(response)["success"])
        self.assertTrue(Subject.objects.filter(pk=self.copy.pk).exists())

    def test_it_can_be_deleted_when_nobody_teaches_it(self):
        Subject.objects.filter(pk=self.copy.pk).delete()
        response = self.call(catalogue_views.api_subject_delete, self.admin,
                             pk=self.entry_subject.pk)
        self.assertTrue(self.body(response)["success"])
        self.assertFalse(UniversitySubject.objects.exists())


class InstituteViewTests(SubjectFixture):
    """What the college may do with the syllabus it has been given."""

    def setUp(self):
        super().setUp()
        self.publish()
        self.given = Subject.objects.get(department=self.adopted)
        self.mine = Subject.objects.create(
            department=self.own, code="ENG1", name="English", semester=1,
            credits=3)

    def rows(self):
        self.client.force_login(self.head)
        return {r["code"]: r for r in self.client.get(
            reverse("academics:api_subjects"),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest").json()["data"]["rows"]}

    def test_a_published_subject_reads_as_read_only_and_names_its_owner(self):
        row = self.rows()["DSA"]
        self.assertTrue(row["read_only"])
        self.assertEqual(row["owner"], "ENGGU")

    def test_the_colleges_own_subject_is_editable(self):
        row = self.rows()["ENG1"]
        self.assertFalse(row["read_only"])
        self.assertEqual(row["owner"], "")

    def test_the_college_cannot_edit_a_published_subject(self):
        response = self.call(views.api_subject_save, self.head,
                             pk=self.given.pk, code="DSA", name="Mine now",
                             department=str(self.adopted.pk), semester="3",
                             degree="BACHELOR", subject_type="THEORY",
                             credits="4", is_active="on")
        self.assertEqual(response.status_code, 403)
        self.given.refresh_from_db()
        self.assertEqual(self.given.name, "Data Structures")

    def test_the_college_cannot_archive_a_published_subject(self):
        """
        There is no separate toggle endpoint for subjects — archiving is the
        Active checkbox on the save form — so this is the save path with the
        box cleared. It has to be refused by the same rule.
        """
        response = self.call(views.api_subject_save, self.head,
                             pk=self.given.pk, code="DSA",
                             name="Data Structures",
                             department=str(self.adopted.pk), semester="3",
                             degree="BACHELOR", subject_type="THEORY",
                             credits="4")
        self.assertEqual(response.status_code, 403)
        self.given.refresh_from_db()
        self.assertTrue(self.given.is_active)

    def test_the_college_cannot_delete_a_published_subject(self):
        response = self.call(views.api_subject_delete, self.head,
                             pk=self.given.pk)
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Subject.objects.filter(pk=self.given.pk).exists())

    def test_the_college_cannot_add_a_subject_to_an_adopted_department(self):
        response = self.call(views.api_subject_save, self.head,
                             department=str(self.adopted.pk), code="EXTRA",
                             name="Local paper", semester="3",
                             degree="BACHELOR", subject_type="THEORY",
                             credits="2", is_active="on")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Subject.objects.filter(code="EXTRA").exists())

    def test_the_college_can_add_and_edit_in_its_own_department(self):
        created = self.call(views.api_subject_save, self.head,
                            department=str(self.own.pk), code="ENG2",
                            name="Literature", semester="2",
                            degree="BACHELOR", subject_type="THEORY",
                            credits="3", is_active="on")
        self.assertTrue(self.body(created)["success"], created.content)
        edited = self.call(views.api_subject_save, self.head, pk=self.mine.pk,
                           department=str(self.own.pk), code="ENG1",
                           name="English I", semester="1", degree="BACHELOR",
                           subject_type="THEORY", credits="3", is_active="on")
        self.assertTrue(self.body(edited)["success"], edited.content)


class DropdownTests(SubjectFixture):
    """The New-subject dropdown offers exactly what the endpoint accepts."""

    def page(self):
        self.client.force_login(self.head)
        return self.client.get(reverse("academics:subjects"))

    def test_an_adopted_department_is_not_offered(self):
        self.assertNotIn(self.adopted.pk,
                         {d.pk for d in self.page().context["departments"]})

    def test_the_colleges_own_department_is_offered(self):
        self.assertIn(self.own.pk,
                      {d.pk for d in self.page().context["departments"]})

    def test_the_filter_above_the_table_still_shows_everything(self):
        self.assertIn(self.adopted.pk,
                      {d.pk for d in self.page().context["filter_departments"]})

    def test_the_button_is_hidden_when_every_department_is_adopted(self):
        self.own.delete()
        response = self.page()
        self.assertFalse(response.context["can_add"])
        self.assertContains(response, "publishes the syllabus for them")

    def test_the_dropdown_matches_what_the_server_accepts(self):
        for department in [self.adopted, self.own]:
            offered = department.pk in {
                d.pk for d in self.page().context["departments"]}
            response = self.call(views.api_subject_save, self.head,
                                 department=str(department.pk), code="TMP1",
                                 name="Probe", semester="1",
                                 degree="BACHELOR", subject_type="THEORY",
                                 credits="1", is_active="on")
            accepted = response.status_code == 200
            self.assertEqual(offered, accepted,
                             f"{department.code}: offered={offered} "
                             f"accepted={accepted}")
            Subject.objects.filter(code="TMP1").delete()
