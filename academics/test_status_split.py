"""
Status and revocation are two facts, not one.

The bug that forced the split: a revoked department reported **0 students**
while holding four. Every student was perfectly active, but "revoked" had
overwritten "active" on the way to the screen, so counting active students
found none.

So: `status` is the row's own lifecycle and the only thing a count reads;
`is_revoked` is a separate boolean that display and filtering read. A revoked
row keeps its status — that is the entire point, and most of what is asserted
here.
"""
from django.test import TestCase
from django.urls import reverse

from academics.models import Batch, Department, StudentProfile, Subject
from accounts.models import Discipline, Institute, InstituteAffiliation, User
from core.enums import RowStatus


class SplitFixture(TestCase):
    def setUp(self):
        self.institute = Institute.objects.create(
            name="Acme", code="ACME", email="o@a.edu", status="APPROVED")
        InstituteAffiliation.objects.create(
            institute=self.institute, discipline=Discipline.ENGG, university=None)
        self.dept = Department.objects.create(
            institute=self.institute, code="CSE", name="CSE",
            discipline=Discipline.ENGG)
        self.batch = Batch.objects.create(
            department=self.dept, label="2022-26", start_year=2022, end_year=2026)
        self.head = User.objects.create_user(
            email="h@a.edu", password="Str0ngPass!23", role=User.Role.HEAD,
            institute=self.institute, registration_completed=True)
        for n in range(4):
            user = User.objects.create_user(
                email=f"s{n}@a.edu", password="Str0ngPass!23",
                role=User.Role.STUDENT, institute=self.institute,
                registration_completed=True)
            StudentProfile.objects.create(
                user=user, department=self.dept, batch=self.batch,
                class_roll=str(n))
        Subject.objects.create(department=self.dept, code="DSA", name="DSA",
                               semester=1, credits=4)

    def revoke(self):
        self.institute.affiliations.filter(discipline=Discipline.ENGG).delete()
        self.dept.refresh_from_db()

    def rows(self, url_name):
        self.client.force_login(self.head)
        return self.client.get(
            reverse(url_name),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest").json()["data"]["rows"]


class TheReportedBugTests(SplitFixture):
    def test_a_revoked_department_still_reports_its_students(self):
        """The symptom, exactly as reported: four students, not zero."""
        self.revoke()
        row = self.rows("academics:api_departments")[0]
        self.assertTrue(row["revoked"])
        self.assertEqual(row["student_count"], 4)

    def test_the_students_are_revoked_and_still_active(self):
        self.revoke()
        for student in StudentProfile.objects.all():
            self.assertTrue(student.is_revoked)
            self.assertEqual(student.status, RowStatus.ACTIVE)

    def test_archiving_a_department_does_not_relabel_its_contents(self):
        self.dept.is_active = False
        self.dept.save()
        for student in StudentProfile.objects.all():
            self.assertEqual(student.status, RowStatus.ACTIVE)
        self.assertEqual(
            self.rows("academics:api_departments")[0]["student_count"], 4)


class FlagMaintenanceTests(SplitFixture):
    """A stored flag is only worth having if nothing can leave it stale."""

    def test_removing_the_affiliation_sets_it_everywhere(self):
        self.revoke()
        self.assertTrue(Subject.objects.get().is_revoked)
        self.assertTrue(Batch.objects.get().is_revoked)
        self.assertTrue(all(s.is_revoked for s in StudentProfile.objects.all()))

    def test_adding_it_back_clears_it_everywhere(self):
        self.revoke()
        InstituteAffiliation.objects.create(
            institute=self.institute, discipline=Discipline.ENGG, university=None)
        self.dept.refresh_from_db()
        self.assertFalse(self.dept.is_revoked)
        self.assertFalse(Subject.objects.get().is_revoked)

    def test_a_row_created_inside_a_revoked_department_is_revoked(self):
        """Inherited on save, so it is right from the moment it exists."""
        self.revoke()
        subject = Subject.objects.create(department=self.dept, code="NEW",
                                         name="New", semester=1, credits=4)
        self.assertTrue(subject.is_revoked)

    def test_moving_a_department_to_a_live_discipline_clears_it(self):
        self.revoke()
        InstituteAffiliation.objects.create(
            institute=self.institute, discipline=Discipline.GENERAL,
            university=None)
        self.dept.discipline = Discipline.GENERAL
        self.dept.save()
        self.dept.refresh_from_db()
        self.assertFalse(self.dept.is_revoked)
        self.assertFalse(Subject.objects.get().is_revoked)

    def test_a_department_with_no_discipline_is_never_revoked(self):
        blank = Department.objects.create(
            institute=self.institute, code="OLD", name="Legacy", discipline="")
        self.assertFalse(blank.is_revoked)


class StatusSyncTests(SplitFixture):
    def test_archiving_by_is_active_sets_the_status(self):
        subject = Subject.objects.get()
        subject.is_active = False
        subject.save()
        subject.refresh_from_db()
        self.assertEqual(subject.status, RowStatus.ARCHIVED)

    def test_restoring_by_is_active_clears_it(self):
        """
        The precedence bug in the first attempt: `status` won, so ticking
        Active on an archived row was silently overruled by its own status.
        """
        subject = Subject.objects.get()
        subject.is_active = False
        subject.save()
        subject.is_active = True
        subject.save()
        subject.refresh_from_db()
        self.assertEqual(subject.status, RowStatus.ACTIVE)
        self.assertTrue(subject.is_active)

    def test_an_unfinished_account_is_invited_not_active(self):
        user = User.objects.create_user(
            email="new@a.edu", password="Str0ngPass!23", role=User.Role.TEACHER,
            institute=self.institute, department=self.dept,
            registration_completed=False)
        self.assertEqual(user.status, RowStatus.INVITED)

    def test_a_deactivated_account_is_archived(self):
        user = User.objects.create_user(
            email="off@a.edu", password="Str0ngPass!23", role=User.Role.TEACHER,
            institute=self.institute, is_active=False)
        self.assertEqual(user.status, RowStatus.ARCHIVED)


class PayloadTests(SplitFixture):
    """Item 1: every table sends both, so the cell can lead with Revoked."""

    def test_every_table_sends_status_and_the_flag(self):
        self.revoke()
        for url in ("academics:api_subjects", "academics:api_batches",
                    "academics:api_students", "academics:api_departments"):
            with self.subTest(url=url):
                for row in self.rows(url):
                    self.assertIn(row["status"],
                                  ["REVOKED"] + list(RowStatus.values), url)
                    self.assertTrue(row["revoked"], url)

    def test_the_status_string_leads_with_revoked(self):
        self.revoke()
        self.assertEqual(self.rows("academics:api_students")[0]["status"],
                         "REVOKED")

    def test_without_the_flag_it_is_the_rows_own_status(self):
        self.assertEqual(self.rows("academics:api_students")[0]["status"],
                         RowStatus.ACTIVE)


class BackfillTests(TestCase):
    """
    The data migration's own logic, run against the real app registry.

    Worth a test of its own because nothing else reaches it: the harness builds
    the schema from the models and skips migrations entirely, so `0009`'s
    `RunPython` had never executed anywhere before it ran on the live database
    and failed.

    It failed on a shape, not on logic — `filter(user__registration_completed=…)`
    walks from one collection into another, and django_mongodb_backend cannot
    express that inside an `update()`. sqlite runs it happily, which is why
    only the real database noticed. This test at least proves the reconstruction
    is right; `/tmp/subq.py`-style shape scanning is what catches the other half.
    """

    def _backfill(self):
        import importlib

        import django.apps

        module = importlib.import_module(
            "academics.migrations.0009_status_and_revoked")
        module.backfill(django.apps.apps, None)

    def test_it_reconstructs_status_and_revocation(self):
        institute = Institute.objects.create(
            name="B", code="B", email="b@b.edu", status="APPROVED")
        InstituteAffiliation.objects.create(
            institute=institute, discipline=Discipline.ENGG, university=None)
        live = Department.objects.create(
            institute=institute, code="CSE", name="CSE",
            discipline=Discipline.ENGG)
        # A department whose discipline is not on file — what "revoked" means.
        gone = Department.objects.create(
            institute=institute, code="PHM", name="PHM",
            discipline=Discipline.PHARMACY)
        batch = Batch.objects.create(department=live, label="2022-26",
                                     start_year=2022, end_year=2026)
        subject = Subject.objects.create(department=gone, code="X", name="X",
                                         semester=1, credits=4)
        finished = User.objects.create_user(
            email="a@b.edu", password="Str0ngPass!23", role=User.Role.STUDENT,
            institute=institute, registration_completed=True)
        unfinished = User.objects.create_user(
            email="c@b.edu", password="Str0ngPass!23", role=User.Role.STUDENT,
            institute=institute, registration_completed=False)
        done = StudentProfile.objects.create(
            user=finished, department=live, batch=batch, class_roll="1")
        pending = StudentProfile.objects.create(
            user=unfinished, department=live, batch=batch, class_roll="2")

        # Scramble the new columns so the backfill has something to correct.
        for model, pk in ((Department, live.pk), (Department, gone.pk),
                          (Subject, subject.pk), (StudentProfile, done.pk),
                          (StudentProfile, pending.pk)):
            model.objects.filter(pk=pk).update(status="ACTIVE", is_revoked=False)

        self._backfill()

        live.refresh_from_db(); gone.refresh_from_db()
        subject.refresh_from_db(); done.refresh_from_db(); pending.refresh_from_db()
        self.assertFalse(live.is_revoked)
        self.assertTrue(gone.is_revoked)
        self.assertTrue(subject.is_revoked)
        self.assertEqual(done.status, RowStatus.ACTIVE)
        # The line that broke: an unfinished account's profile is INVITED.
        self.assertEqual(pending.status, RowStatus.INVITED)

    def test_it_reconstructs_user_status_from_the_two_flags(self):
        institute = Institute.objects.create(
            name="C", code="C", email="c@c.edu", status="APPROVED")
        active = User.objects.create_user(
            email="x@c.edu", password="Str0ngPass!23", role=User.Role.TEACHER,
            institute=institute, registration_completed=True)
        invited = User.objects.create_user(
            email="y@c.edu", password="Str0ngPass!23", role=User.Role.TEACHER,
            institute=institute, registration_completed=False)
        off = User.objects.create_user(
            email="z@c.edu", password="Str0ngPass!23", role=User.Role.TEACHER,
            institute=institute, is_active=False)
        User.objects.all().update(status="ACTIVE")

        self._backfill()

        for user, expected in ((active, RowStatus.ACTIVE),
                               (invited, RowStatus.INVITED),
                               (off, RowStatus.ARCHIVED)):
            user.refresh_from_db()
            self.assertEqual(user.status, expected, user.email)

    def test_a_department_with_no_discipline_is_not_revoked(self):
        institute = Institute.objects.create(
            name="D", code="D", email="d@d.edu", status="APPROVED")
        legacy = Department.objects.create(
            institute=institute, code="OLD", name="Legacy", discipline="")
        Department.objects.filter(pk=legacy.pk).update(is_revoked=True)
        self._backfill()
        legacy.refresh_from_db()
        self.assertFalse(legacy.is_revoked)
