"""
The institute's Departments modal: discipline first, then adopt or write.

The rule being tested is that where a department *comes from* is decided by its
discipline, not by the person. An affiliated discipline offers the university's
published departments and nothing else; an autonomous one offers a blank name
and code. A college cannot type its own name into an affiliated discipline —
its copy would then disagree with every other college running the same
syllabus.

Deleting and deactivating are the institute's either way, which is the one
thing that does *not* follow the affiliation.
"""
import json

from django.test import RequestFactory, TestCase
from django.urls import reverse

from academics import catalogue, views
from academics.models import (
    Batch,
    Department,
    Subject,
    UniversityBatch,
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


class ModalFixture(TestCase):
    def setUp(self):
        self.university = University.objects.create(
            name="Engineering University", code="ENGGU", short_name="ENGGU",
            email="e@u.edu", grants_affiliation=True)
        UniversityDiscipline.objects.create(university=self.university,
                                            discipline=Discipline.ENGG)
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
            name="Computer Science & Engineering", code="CSE")
        UniversityBatch.objects.create(department=self.entry, label="2022-26",
                                       start_year=2022, end_year=2026)
        UniversitySubject.objects.create(department=self.entry, code="DSA",
                                         name="Data Structures", semester=3)
        self.client.force_login(self.head)

    def call(self, view, pk=None, **data):
        request = RequestFactory().post("/", data)
        request.user = self.head
        request.session = self.client.session
        request.META["HTTP_X_REQUESTED_WITH"] = "XMLHttpRequest"
        return view(request, pk=pk) if pk is not None else view(request)

    def body(self, response):
        return json.loads(response.content)

    def options(self):
        return self.client.get(
            reverse("academics:api_department_options"),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest").json()["data"]["disciplines"]


class OptionsTests(ModalFixture):
    """What the modal is given to drive itself."""

    def test_it_lists_every_discipline_the_institute_holds(self):
        self.assertEqual({d["value"] for d in self.options()},
                         {"ENGG", "GENERAL"})

    def test_an_affiliated_discipline_carries_the_universitys_departments(self):
        engg = next(d for d in self.options() if d["value"] == "ENGG")
        self.assertFalse(engg["autonomous"])
        self.assertEqual(engg["university"], "ENGGU")
        self.assertEqual([e["code"] for e in engg["catalogue"]], ["CSE"])

    def test_an_autonomous_discipline_carries_an_empty_catalogue(self):
        """
        The emptiness *is* the instruction — the modal switches to name-and-code
        when it sees it, rather than being told separately which mode to be in.
        """
        general = next(d for d in self.options() if d["value"] == "GENERAL")
        self.assertTrue(general["autonomous"])
        self.assertEqual(general["catalogue"], [])

    def test_an_entry_already_running_is_marked_so(self):
        """Or somebody adopts twice and wonders why nothing changed."""
        catalogue.adopt(institute=self.institute, entry=self.entry)
        engg = next(d for d in self.options() if d["value"] == "ENGG")
        self.assertTrue(engg["catalogue"][0]["adopted"])

    def test_an_archived_catalogue_entry_is_not_offered(self):
        self.entry.status = RowStatus.ARCHIVED
        self.entry.save()
        engg = next(d for d in self.options() if d["value"] == "ENGG")
        self.assertEqual(engg["catalogue"], [])


class AffiliatedCreateTests(ModalFixture):
    def test_adopting_brings_the_department_and_everything_published(self):
        response = self.call(views.api_department_save, discipline="ENGG",
                             catalogue_entry=str(self.entry.pk),
                             hod_email="hod@a.edu")
        self.assertTrue(self.body(response)["success"], response.content)
        department = Department.objects.get()
        self.assertEqual(department.source_id, self.entry.pk)
        self.assertEqual(department.name, "Computer Science & Engineering")
        self.assertEqual(Subject.objects.filter(department=department).count(), 1)
        self.assertEqual(Batch.objects.filter(department=department).count(), 1)

    def test_the_hod_is_invited(self):
        self.call(views.api_department_save, discipline="ENGG",
                  catalogue_entry=str(self.entry.pk), hod_email="hod@a.edu")
        department = Department.objects.get()
        self.assertIsNotNone(department.hod)
        self.assertEqual(department.hod.email, "hod@a.edu")

    def test_a_typed_name_is_ignored_not_honoured(self):
        """
        The name belongs to the university. A college inventing its own would
        make its copy disagree with every other running the same syllabus.
        """
        self.call(views.api_department_save, discipline="ENGG",
                  catalogue_entry=str(self.entry.pk),
                  name="My Own Name", code="MINE", hod_email="")
        department = Department.objects.get()
        self.assertEqual(department.name, "Computer Science & Engineering")
        self.assertEqual(department.code, "CSE")

    def test_choosing_no_catalogue_entry_is_refused(self):
        response = self.call(views.api_department_save, discipline="ENGG",
                             name="Invented", code="INV")
        body = self.body(response)
        self.assertFalse(body["success"])
        self.assertIn("catalogue_entry", body["errors"])
        self.assertEqual(Department.objects.count(), 0)

    def test_another_universitys_entry_cannot_be_adopted(self):
        other = University.objects.create(name="O", code="O", email="o@o.edu")
        theirs = UniversityDepartment.objects.create(
            university=other, discipline=Discipline.ENGG, name="X", code="XX")
        response = self.call(views.api_department_save, discipline="ENGG",
                             catalogue_entry=str(theirs.pk))
        self.assertFalse(self.body(response)["success"])
        self.assertEqual(Department.objects.count(), 0)


class AutonomousCreateTests(ModalFixture):
    def test_the_institute_writes_the_name_and_code_itself(self):
        response = self.call(views.api_department_save, discipline="GENERAL",
                             name="Arts & Humanities", code="ARTS",
                             hod_email="arts@a.edu")
        self.assertTrue(self.body(response)["success"], response.content)
        department = Department.objects.get()
        self.assertEqual(department.name, "Arts & Humanities")
        self.assertIsNone(department.source_id)
        self.assertEqual(department.discipline, "GENERAL")

    def test_a_missing_name_is_refused(self):
        response = self.call(views.api_department_save, discipline="GENERAL",
                             code="ARTS")
        self.assertFalse(self.body(response)["success"])
        self.assertEqual(Department.objects.count(), 0)


class DisciplineGateTests(ModalFixture):
    def test_a_discipline_the_institute_does_not_teach_is_refused(self):
        response = self.call(views.api_department_save, discipline="PHARMACY",
                             name="Pharmacy", code="PHM")
        body = self.body(response)
        self.assertFalse(body["success"])
        self.assertIn("discipline", body["errors"])

    def test_no_discipline_at_all_is_refused(self):
        response = self.call(views.api_department_save, name="X", code="X")
        self.assertFalse(self.body(response)["success"])


class InstituteControlTests(ModalFixture):
    """
    "But Institute can delete or deactive any department."

    The one thing that does not follow the affiliation. A college closing a
    department it no longer runs is its own decision, adopted or not.
    """

    def setUp(self):
        super().setUp()
        self.adopted = catalogue.adopt(institute=self.institute,
                                       entry=self.entry)

    def test_the_institute_can_archive_an_adopted_department(self):
        response = self.call(views.api_department_delete, pk=self.adopted.pk)
        self.assertTrue(self.body(response)["success"], response.content)
        self.adopted.refresh_from_db()
        self.assertEqual(self.adopted.status, RowStatus.ARCHIVED)

    def test_the_institute_can_still_change_the_hod_of_an_adopted_one(self):
        response = self.call(views.api_department_save, pk=self.adopted.pk,
                             hod_email="newhod@a.edu")
        self.assertTrue(self.body(response)["success"], response.content)
        self.adopted.refresh_from_db()
        self.assertEqual(self.adopted.hod.email, "newhod@a.edu")

    def test_but_not_its_name(self):
        self.call(views.api_department_save, pk=self.adopted.pk,
                  name="Renamed", code="REN", discipline="ENGG",
                  hod_email="")
        self.adopted.refresh_from_db()
        self.assertEqual(self.adopted.name, "Computer Science & Engineering")

    def test_one_person_cannot_head_two_departments(self):
        """
        `Department.hod` is a OneToOne, so moving somebody silently would leave
        their old department headless without saying so.
        """
        other = Department.objects.create(
            institute=self.institute, code="ARTS", name="Arts",
            discipline=Discipline.GENERAL)
        self.call(views.api_department_save, pk=other.pk,
                  name="Arts", code="ARTS", discipline="GENERAL",
                  hod_email="shared@a.edu")
        response = self.call(views.api_department_save, pk=self.adopted.pk,
                             hod_email="shared@a.edu")
        body = self.body(response)
        self.assertFalse(body["success"])
        self.assertIn("already leads", body["message"])
