"""
The university's catalogue, and what adopting from it means.

This replaces the push model. The university used to reach into every
affiliated institute and write rows there; now it publishes, and the institute
adopts. Same end state, opposite direction, and the difference shows in one
place worth naming: an institute that has not adopted a department has nothing
of it at all, rather than having had it appear unasked.

The rule everything below turns on is `source`. A row adopted from the
catalogue carries the link and is the university's to change; a row without one
is the institute's — whether that is an autonomous department, or one
grandfathered from before the catalogue existed. One test, three cases.
"""
from django.test import TestCase

from academics import catalogue
from academics.curriculum import is_read_only, may_define_department
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
    User,
)
from core.enums import RowStatus


class CatalogueFixture(TestCase):
    def setUp(self):
        self.university = University.objects.create(
            name="Engineering University", code="ENGGU", short_name="ENGGU",
            email="e@u.edu", grants_affiliation=True)
        self.admin = User.objects.create_user(
            email="admin@u.edu", password="Str0ngPass!23",
            role=User.Role.UNIVERSITY, university=self.university,
            registration_completed=True)

        self.institute = Institute.objects.create(
            name="Acme", code="ACME", email="o@a.edu", status="APPROVED")
        # Engineering is theirs; general courses are ours.
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
        UniversityBatch.objects.create(
            department=self.entry, label="2022-26",
            start_year=2022, end_year=2026)
        UniversitySubject.objects.create(
            department=self.entry, code="DSA", name="Data Structures",
            semester=3, credits=4)


class ChoiceTests(CatalogueFixture):
    """What the institute is offered, per discipline."""

    def test_an_affiliated_discipline_offers_the_universitys_departments(self):
        offered = catalogue.choices_for(self.institute, Discipline.ENGG)
        self.assertEqual([e.code for e in offered], ["CSE"])

    def test_an_autonomous_discipline_offers_nothing(self):
        """
        A real answer, not a failure — there is no university to publish one,
        which is exactly the case where the institute types the name itself.
        """
        self.assertEqual(
            list(catalogue.choices_for(self.institute, Discipline.GENERAL)), [])

    def test_a_discipline_the_institute_does_not_hold_offers_nothing(self):
        self.assertEqual(
            list(catalogue.choices_for(self.institute, Discipline.PHARMACY)), [])

    def test_an_archived_entry_is_not_offered(self):
        self.entry.status = RowStatus.ARCHIVED
        self.entry.save()
        self.assertEqual(
            list(catalogue.choices_for(self.institute, Discipline.ENGG)), [])

    def test_another_universitys_catalogue_is_not_offered(self):
        other = University.objects.create(
            name="Other", code="OTH", email="o@o.edu")
        UniversityDepartment.objects.create(
            university=other, discipline=Discipline.ENGG, name="X", code="XX")
        offered = catalogue.choices_for(self.institute, Discipline.ENGG)
        self.assertEqual([e.code for e in offered], ["CSE"])


class AdoptionTests(CatalogueFixture):
    def test_adopting_creates_the_department_and_everything_published(self):
        department = catalogue.adopt(institute=self.institute, entry=self.entry)
        self.assertEqual(department.code, "CSE")
        self.assertEqual(department.source_id, self.entry.pk)
        self.assertEqual(Batch.objects.filter(department=department).count(), 1)
        self.assertEqual(Subject.objects.filter(department=department).count(), 1)

    def test_the_copies_point_back_at_what_they_came_from(self):
        department = catalogue.adopt(institute=self.institute, entry=self.entry)
        self.assertIsNotNone(Batch.objects.get(department=department).source_id)
        self.assertIsNotNone(Subject.objects.get(department=department).source_id)

    def test_adopting_twice_is_not_an_error(self):
        """The button that calls this is one a person can double click."""
        first = catalogue.adopt(institute=self.institute, entry=self.entry)
        second = catalogue.adopt(institute=self.institute, entry=self.entry)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Department.objects.filter(institute=self.institute).count(), 1)
        self.assertEqual(Batch.objects.count(), 1)

    def test_an_institute_that_has_not_adopted_has_nothing(self):
        """
        The difference from the push model, stated. Publishing alone reaches
        nobody; the college chooses.
        """
        self.assertEqual(Department.objects.count(), 0)


class PropagationTests(CatalogueFixture):
    def setUp(self):
        super().setUp()
        self.department = catalogue.adopt(institute=self.institute,
                                          entry=self.entry)

    def test_a_newly_published_subject_reaches_every_adopter(self):
        UniversitySubject.objects.create(
            department=self.entry, code="OS", name="Operating Systems",
            semester=4, credits=4)
        catalogue.propagate(self.entry)
        self.assertTrue(Subject.objects.filter(
            department=self.department, code="OS").exists())

    def test_a_newly_published_batch_reaches_every_adopter(self):
        UniversityBatch.objects.create(
            department=self.entry, label="2023-27",
            start_year=2023, end_year=2027)
        catalogue.propagate(self.entry)
        self.assertTrue(Batch.objects.filter(
            department=self.department, label="2023-27").exists())

    def test_archiving_in_the_catalogue_archives_the_copies(self):
        published = UniversityBatch.objects.get(department=self.entry)
        published.status = RowStatus.ARCHIVED
        published.save()
        catalogue.propagate(self.entry)
        copy = Batch.objects.get(department=self.department)
        self.assertEqual(copy.status, RowStatus.ARCHIVED)
        self.assertFalse(copy.is_active)

    def test_editing_in_the_catalogue_updates_the_copies(self):
        published = UniversitySubject.objects.get(department=self.entry)
        published.name = "Data Structures & Algorithms"
        published.save()
        catalogue.propagate(self.entry)
        self.assertEqual(Subject.objects.get(department=self.department).name,
                         "Data Structures & Algorithms")

    def test_a_row_the_institute_made_itself_is_never_seized(self):
        """
        Same code, no source. Taking it would take its attendance history with
        it, which is a worse outcome than two rows that look alike.
        """
        mine = Subject.objects.create(
            department=self.department, code="OWN", name="Mine",
            semester=1, credits=3)
        UniversitySubject.objects.create(
            department=self.entry, code="OWN", name="Theirs",
            semester=1, credits=4)
        catalogue.propagate(self.entry)
        mine.refresh_from_db()
        self.assertIsNone(mine.source_id)
        self.assertEqual(mine.name, "Mine")

    def test_propagating_twice_does_not_duplicate(self):
        catalogue.propagate(self.entry)
        catalogue.propagate(self.entry)
        self.assertEqual(Subject.objects.filter(department=self.department).count(), 1)


class OwnershipTests(CatalogueFixture):
    """
    The one rule, over all three kinds of department.

    `source` decides. That is why grandfathering needed no special case: a
    legacy department has no link, so it reads as the institute's without
    anything being written to say so.
    """

    def setUp(self):
        super().setUp()
        self.adopted = catalogue.adopt(institute=self.institute,
                                       entry=self.entry)
        self.autonomous = Department.objects.create(
            institute=self.institute, code="ARTS", name="Arts",
            discipline=Discipline.GENERAL)
        # What the migration flags: created in an affiliated discipline before
        # the catalogue existed.
        self.legacy = Department.objects.create(
            institute=self.institute, code="ECE", name="Electronics",
            discipline=Discipline.ENGG, is_legacy=True)

    def test_an_adopted_department_is_the_universitys(self):
        self.assertFalse(may_define_department(self.head, self.adopted))
        self.assertTrue(may_define_department(self.admin, self.adopted))

    def test_an_autonomous_department_is_the_institutes(self):
        self.assertTrue(may_define_department(self.head, self.autonomous))
        self.assertFalse(may_define_department(self.admin, self.autonomous))

    def test_a_grandfathered_department_stays_the_institutes(self):
        """
        Locking these would strand any college with attendance running against
        them, mid-term. They keep working; the flag only explains.
        """
        self.assertTrue(may_define_department(self.head, self.legacy))

    def test_an_adopted_subject_is_read_only_for_the_institute(self):
        subject = Subject.objects.get(department=self.adopted)
        self.assertTrue(is_read_only(subject, self.head))
        self.assertFalse(is_read_only(subject, self.admin))

    def test_a_subject_the_institute_added_to_an_adopted_department_is_theirs(self):
        """
        Deliberate. The department is the university's; a paper the college
        added alongside is not, and the link says so without anyone deciding.
        """
        mine = Subject.objects.create(
            department=self.adopted, code="EXTRA", name="Extra",
            semester=1, credits=2)
        self.assertFalse(is_read_only(mine, self.head))

    def test_a_subject_in_a_grandfathered_department_is_editable(self):
        legacy_subject = Subject.objects.create(
            department=self.legacy, code="SIG", name="Signals",
            semester=1, credits=4)
        self.assertFalse(is_read_only(legacy_subject, self.head))

    def test_an_autonomous_departments_batches_are_editable(self):
        batch = Batch.objects.create(
            department=self.autonomous, label="2022-25",
            start_year=2022, end_year=2025)
        self.assertFalse(is_read_only(batch, self.head))
