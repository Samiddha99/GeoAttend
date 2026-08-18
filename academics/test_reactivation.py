"""
Coming back from revoked.

Two routes, and they answer different questions:

1. The **department's** discipline is changed from a revoked one to a live one.
   The department and everything in it becomes active again.
2. A **subject or batch** is moved out of a revoked department into a live one.
   Its state on arrival is whatever the Active checkbox says — not whatever it
   happened to be while it was stranded.

The restore in (1) puts each row back to *its previous state*, not to active.
That needs a memory: a cohort that graduated and a cohort hidden by a discipline
removal are indistinguishable afterwards. `archived_with_discipline` is set only
on rows that were live when the removal happened, and cleared when they come
back, so it never describes more than the removal currently in force.

An earlier version restored everything unconditionally and turned a graduated
2018 cohort back on alongside the wing being reopened. Several tests here exist
specifically to keep that from returning.
"""
import json

from django.test import RequestFactory, TestCase

from academics import views
from academics.curriculum import is_revoked, reactivate_department_contents
from academics.models import Batch, Department, StudentProfile, Subject
from accounts.models import (
    Discipline,
    Institute,
    InstituteAffiliation,
    User,
)


class ReactivationFixture(TestCase):
    def setUp(self):
        self.institute = Institute.objects.create(
            name="Acme College", code="ACME", email="office@acme.edu",
            status=Institute.Status.APPROVED)
        # GENERAL is live; ARTS below will be moved into a discipline that is
        # not on file, which is what "revoked" means.
        InstituteAffiliation.objects.create(
            institute=self.institute, discipline=Discipline.GENERAL,
            university=None)

        self.head = User.objects.create_user(
            email="head@acme.edu", password="Str0ngPass!23",
            role=User.Role.HEAD, institute=self.institute,
            registration_completed=True)

        # A department in a discipline the institute does not hold, with
        # everything inside it archived — the state a discipline removal leaves.
        # `archived_with_discipline=True` is what a real removal leaves — it is
        # the snapshot that says "this was live until the discipline went".
        self.dead = Department.objects.create(
            institute=self.institute, code="PHM", name="Pharmacy",
            discipline=Discipline.PHARMACY, is_active=False,
            archived_with_discipline=True)
        self.subject = Subject.objects.create(
            department=self.dead, code="PCG", name="Pharmacology",
            semester=1, credits=3, is_active=False,
            archived_with_discipline=True)
        self.batch = Batch.objects.create(
            department=self.dead, label="2022-26", start_year=2022,
            end_year=2026, is_active=False, archived_with_discipline=True)
        self.teacher = User.objects.create_user(
            email="t@acme.edu", password="Str0ngPass!23", full_name="A Teacher",
            role=User.Role.TEACHER, institute=self.institute,
            department=self.dead, registration_completed=True, is_active=False)
        User.objects.filter(pk=self.teacher.pk).update(
            archived_with_discipline=True)
        self.teacher.refresh_from_db()
        student_user = User.objects.create_user(
            email="s@acme.edu", password="Str0ngPass!23", full_name="A Student",
            role=User.Role.STUDENT, institute=self.institute)
        self.student = StudentProfile.objects.create(
            user=student_user, department=self.dead, batch=self.batch,
            class_roll="1", is_active=False, archived_with_discipline=True)

        # A live department to move things into.
        self.live = Department.objects.create(
            institute=self.institute, code="ARTS", name="Arts",
            discipline=Discipline.GENERAL)

    def _post(self, view, pk=None, **data):
        request = RequestFactory().post("/", data)
        request.user = self.head
        request.session = self.client.session
        request.META["HTTP_X_REQUESTED_WITH"] = "XMLHttpRequest"
        return view(request, pk=pk) if pk is not None else view(request)

    def _body(self, response):
        return json.loads(response.content)

    def _refresh(self):
        for obj in (self.dead, self.subject, self.batch, self.teacher,
                    self.student):
            obj.refresh_from_db()


class StateTests(ReactivationFixture):
    def test_the_stranded_department_reads_as_revoked(self):
        self.assertTrue(is_revoked(self.dead))

    def test_the_live_one_does_not(self):
        self.assertFalse(is_revoked(self.live))


class HelperTests(ReactivationFixture):
    def test_reactivating_brings_back_everything_in_the_department(self):
        reactivate_department_contents(self.dead, actor=self.head)
        self._refresh()
        self.assertTrue(self.dead.is_active)
        self.assertTrue(self.subject.is_active)
        self.assertTrue(self.batch.is_active)
        self.assertTrue(self.teacher.is_active)
        self.assertTrue(self.student.is_active)

    def test_it_reports_what_it_touched(self):
        counts = reactivate_department_contents(self.dead, actor=self.head)
        self.assertEqual(counts["students"], 1)
        self.assertEqual(counts["batches"], 1)

    def test_a_cohort_archived_for_its_own_reasons_stays_archived(self):
        """
        The whole point of the snapshot, and the behaviour this used to get
        wrong. A 2018 cohort archived when it graduated carries no marker, so
        reopening the wing does not bring it back with everything else.
        """
        graduated = Batch.objects.create(
            department=self.dead, label="2018-22", start_year=2018,
            end_year=2022, is_active=False)          # no marker
        reactivate_department_contents(self.dead, actor=self.head)
        graduated.refresh_from_db()
        self.assertFalse(graduated.is_active)

    def test_the_marker_is_cleared_so_it_only_ever_describes_one_removal(self):
        reactivate_department_contents(self.dead, actor=self.head)
        self._refresh()
        self.assertFalse(self.batch.archived_with_discipline)
        self.assertFalse(self.dead.archived_with_discipline)

    def test_a_department_archived_by_hand_is_not_reopened(self):
        """
        Its state is its own. The removal did not switch it off, so a restore
        has no business switching it on.
        """
        self.dead.archived_with_discipline = False
        self.dead.save()
        counts = reactivate_department_contents(self.dead, actor=self.head)
        self.dead.refresh_from_db()
        self.assertFalse(self.dead.is_active)
        self.assertFalse(counts["department_restored"])


class DepartmentDisciplineChangeTests(ReactivationFixture):
    """Rule 1, through the endpoint."""

    def _save(self, discipline):
        return self._post(views.api_department_save, pk=self.dead.pk,
                          name="Pharmacy", code="PHM", discipline=discipline,
                          hod_email="")

    def test_moving_to_a_live_discipline_reactivates_the_department(self):
        response = self._save(Discipline.GENERAL)
        self.assertTrue(self._body(response)["success"], response.content)
        self.dead.refresh_from_db()
        self.assertTrue(self.dead.is_active)
        self.assertFalse(is_revoked(self.dead))

    def test_everything_the_removal_switched_off_comes_back(self):
        self._save(Discipline.GENERAL)
        self._refresh()
        self.assertTrue(self.subject.is_active)
        self.assertTrue(self.batch.is_active)
        self.assertTrue(self.teacher.is_active)
        self.assertTrue(self.student.is_active)

    def test_rows_archived_before_the_removal_stay_archived(self):
        """Item 2, at the endpoint: previous status, not blanket active."""
        graduated = Batch.objects.create(
            department=self.dead, label="2018-22", start_year=2018,
            end_year=2022, is_active=False)
        self._save(Discipline.GENERAL)
        graduated.refresh_from_db()
        self.assertFalse(graduated.is_active)

    def test_a_department_archived_by_hand_before_the_removal_stays_archived(self):
        """
        It was already off when the discipline went, so the removal never
        marked it, so restoring the discipline leaves it off. Its previous
        status *was* archived.
        """
        self.dead.archived_with_discipline = False
        self.dead.save()
        self._save(Discipline.GENERAL)
        self.dead.refresh_from_db()
        self.assertFalse(self.dead.is_active)

    def test_the_answer_says_what_it_reactivated(self):
        """
        A restore that silently un-archives three years of students is a
        surprise; one that reports the numbers is a decision.
        """
        body = self._body(self._save(Discipline.GENERAL))
        self.assertIn("reactivated", body["message"].lower())
        self.assertEqual(body["data"]["reactivated"]["students"], 1)

    def test_a_department_that_was_never_revoked_is_left_alone(self):
        """
        Editing a live department must not quietly un-archive a cohort that was
        deliberately archived.
        """
        archived = Batch.objects.create(
            department=self.live, label="2019-23", start_year=2019,
            end_year=2023, is_active=False)
        self._post(views.api_department_save, pk=self.live.pk,
                   name="Arts", code="ARTS", discipline=Discipline.GENERAL,
                   hod_email="")
        archived.refresh_from_db()
        self.assertFalse(archived.is_active)


class DepartmentActiveCheckboxTests(ReactivationFixture):
    """
    Item 1: a department nobody else governs is archived and restored by hand.

    Separate from the discipline machinery entirely — this is the ordinary
    "switch it off" control, and it must not disturb the snapshot that the
    discipline restore depends on.
    """

    def _save(self, department, **overrides):
        data = {"name": department.name, "code": department.code,
                "discipline": department.discipline, "hod_email": ""}
        data.update(overrides)
        return self._post(views.api_department_save, pk=department.pk, **data)

    def test_unchecking_active_archives_a_live_department(self):
        response = self._save(self.live)          # checkbox absent = unchecked
        self.assertTrue(self._body(response)["success"], response.content)
        self.live.refresh_from_db()
        self.assertFalse(self.live.is_active)

    def test_checking_active_restores_it(self):
        self.live.is_active = False
        self.live.save()
        self._save(self.live, is_active="on")
        self.live.refresh_from_db()
        self.assertTrue(self.live.is_active)

    def test_archiving_by_hand_leaves_no_discipline_marker(self):
        """
        The two mechanisms must stay separate. If this set the marker, a later
        discipline restore would reopen a department somebody deliberately
        closed.
        """
        self._save(self.live)
        self.live.refresh_from_db()
        self.assertFalse(self.live.is_active)
        self.assertFalse(self.live.archived_with_discipline)

    def test_it_does_not_cascade_to_the_contents(self):
        """
        Archiving a department hides it and everything under it through the
        selectors; it does not rewrite each row. Only a discipline removal does
        that, because only that needs a snapshot to undo.
        """
        batch = Batch.objects.create(
            department=self.live, label="2023-27", start_year=2023,
            end_year=2027)
        self._save(self.live)
        batch.refresh_from_db()
        self.assertTrue(batch.is_active)
        self.assertFalse(batch.archived_with_discipline)


class MoveSubjectTests(ReactivationFixture):
    """Rule 2 — the Active checkbox decides the state on arrival."""

    def _save(self, **overrides):
        data = {"code": "PCG", "name": "Pharmacology", "semester": 1,
                "credits": 3, "subject_type": "THEORY", "degree": "BACHELOR",
                "department": str(self.live.pk), "is_active": "on"}
        data.update(overrides)
        return self._post(views.api_subject_save, pk=self.subject.pk, **data)

    def test_moving_it_with_active_ticked_makes_it_active(self):
        response = self._save()
        self.assertTrue(self._body(response)["success"], response.content)
        self.subject.refresh_from_db()
        self.assertEqual(self.subject.department_id, self.live.pk)
        self.assertTrue(self.subject.is_active)

    def test_moving_it_with_active_unticked_leaves_it_archived(self):
        response = self._save(is_active="")
        self.assertTrue(self._body(response)["success"], response.content)
        self.subject.refresh_from_db()
        self.assertEqual(self.subject.department_id, self.live.pk)
        self.assertFalse(self.subject.is_active)

    def test_it_is_no_longer_revoked_once_moved(self):
        self._save()
        self.subject.refresh_from_db()
        self.assertFalse(is_revoked(self.subject.department))

    def test_omitting_the_department_leaves_it_where_it_was(self):
        """
        The bug this nearly shipped with. `_scoped_department` falls back to
        the first department in scope, so a form without the field — a HoD's —
        would have silently moved the subject.
        """
        # `is_active` left off: staying put in a revoked department and asking
        # to be activated is refused now, and that is a different test.
        response = self._post(
            views.api_subject_save, pk=self.subject.pk,
            code="PCG", name="Pharmacology", semester=1, credits=3,
            subject_type="THEORY", degree="BACHELOR")
        self.assertTrue(self._body(response)["success"], response.content)
        self.subject.refresh_from_db()
        self.assertEqual(self.subject.department_id, self.dead.pk)


class MoveBatchTests(ReactivationFixture):
    def _save(self, **overrides):
        data = {"label": "2022-26", "department": str(self.live.pk),
                "is_active": "on"}
        data.update(overrides)
        return self._post(views.api_batch_save, pk=self.batch.pk, **data)

    def test_moving_it_with_active_ticked_makes_it_active(self):
        response = self._save()
        self.assertTrue(self._body(response)["success"], response.content)
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.department_id, self.live.pk)
        self.assertTrue(self.batch.is_active)

    def test_moving_it_with_active_unticked_leaves_it_archived(self):
        self._save(is_active="")
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.department_id, self.live.pk)
        self.assertFalse(self.batch.is_active)

    def test_omitting_the_department_leaves_it_where_it_was(self):
        self._post(views.api_batch_save, pk=self.batch.pk,
                   label="2022-26", is_active="on")
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.department_id, self.dead.pk)


class DerivedStateTests(ReactivationFixture):
    """
    Item 1: state flows down from the department and cannot be overridden.

    Nothing is copied onto the rows when a department is archived — the answer
    is computed, so it is right the moment the department changes and there is
    no cascade to fall out of step.
    """

    def _rows(self, url_name, **params):
        from django.urls import reverse

        self.client.force_login(self.head)
        return self.client.get(
            reverse(url_name), params,
            HTTP_X_REQUESTED_WITH="XMLHttpRequest").json()["data"]["rows"]

    def setUp(self):
        super().setUp()
        # A *live* row inside a department that is merely archived — the case
        # that used to read as Active on screen.
        self.live.is_active = False
        self.live.save()
        self.stranded = Subject.objects.create(
            department=self.live, code="ENG", name="English",
            semester=1, credits=3, is_active=True)

    def test_a_live_subject_in_an_archived_department_keeps_its_own_status(self):
        """
        Changed deliberately, and this is the change the whole split is for.

        A subject inside an archived department used to be *reported* archived,
        which is how a revoked department came to say "0 students": the word
        had been overwritten before anything counted it. Status is the row's
        own now. The department's state is still visible — on the department.
        """
        rows = {r["code"]: r for r in self._rows("academics:api_subjects")}
        self.assertEqual(rows["ENG"]["state"], "active")
        self.assertTrue(rows["ENG"]["is_active"])
        self.assertFalse(rows["ENG"]["revoked"])

    def test_a_subject_in_a_revoked_department_reads_as_revoked(self):
        rows = {r["code"]: r for r in self._rows("academics:api_subjects")}
        self.assertEqual(rows["PCG"]["state"], "revoked")

    def test_the_active_checkbox_cannot_revive_it_inside_a_dead_department(self):
        response = self._post(
            views.api_subject_save, pk=self.stranded.pk,
            code="ENG", name="English", semester=1, credits=3,
            subject_type="THEORY", degree="BACHELOR", is_active="on",
            department=str(self.live.pk))
        body = self._body(response)
        self.assertFalse(body["success"])
        self.assertIn("archived", body["message"])

    def test_the_refusal_names_the_revoked_case_differently(self):
        response = self._post(
            views.api_subject_save, pk=self.subject.pk,
            code="PCG", name="Pharmacology", semester=1, credits=3,
            subject_type="THEORY", degree="BACHELOR", is_active="on",
            department=str(self.dead.pk))
        self.assertIn("discipline", self._body(response)["message"])

    def test_switching_a_row_off_is_always_allowed(self):
        """Nothing about a dead department makes archiving a row incoherent."""
        response = self._post(
            views.api_subject_save, pk=self.stranded.pk,
            code="ENG", name="English", semester=1, credits=3,
            subject_type="THEORY", degree="BACHELOR",
            department=str(self.live.pk))
        self.assertTrue(self._body(response)["success"], response.content)

    def test_moving_it_to_a_live_department_activates_it(self):
        """The only route back — item 1's last sentence."""
        good = Department.objects.create(
            institute=self.institute, code="COM", name="Commerce",
            discipline=Discipline.GENERAL)
        response = self._post(
            views.api_subject_save, pk=self.stranded.pk,
            code="ENG", name="English", semester=1, credits=3,
            subject_type="THEORY", degree="BACHELOR", is_active="on",
            department=str(good.pk))
        self.assertTrue(self._body(response)["success"], response.content)
        self.stranded.refresh_from_db()
        self.assertEqual(self.stranded.department_id, good.pk)
        self.assertTrue(self.stranded.is_active)


class DeadDepartmentsAreHiddenTests(ReactivationFixture):
    """Items 2 and 3: out of the dropdowns, out of the statistics."""

    def test_the_lookups_offer_only_live_departments(self):
        from django.urls import reverse

        self.client.force_login(self.head)
        data = self.client.get(
            reverse("academics:api_lookups"),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest").json()["data"]
        codes = {d["code"] for d in data["departments"]}
        self.assertIn("ARTS", codes)
        self.assertNotIn("PHM", codes)       # revoked

    def test_an_archived_department_is_not_offered_either(self):
        from django.urls import reverse

        self.live.is_active = False
        self.live.save()
        self.client.force_login(self.head)
        data = self.client.get(
            reverse("academics:api_lookups"),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest").json()["data"]
        self.assertEqual(data["departments"], [])

    def test_reports_do_not_count_students_in_a_dead_department(self):
        from academics.selectors import students_qs_for

        # The student sits in the revoked department, and is live in itself.
        StudentProfile.objects.filter(pk=self.student.pk).update(is_active=True)
        Batch.objects.filter(pk=self.batch.pk).update(is_active=True)
        self.assertEqual(students_qs_for(self.head).count(), 0)

    def test_but_the_management_screen_still_lists_them(self):
        """
        The distinction that made this worth two flags: the Students screen has
        to show a revoked student — with the right status — or the status
        column has nothing to describe.
        """
        from academics.selectors import students_qs_for

        StudentProfile.objects.filter(pk=self.student.pk).update(is_active=True)
        Batch.objects.filter(pk=self.batch.pk).update(is_active=True)
        self.assertEqual(
            students_qs_for(self.head, include_dead_departments=True).count(), 1)
