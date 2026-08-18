"""
Who may define a department, and who may run it.

Two rights that look like one and are not:

* **defining** it — name, code, discipline — follows the discipline's
  affiliation, exactly as its subjects and batches do;
* **running** it — who leads it — stays with the institute whatever the
  affiliation.

Keeping them apart matters. A university that sets a syllabus has no view on
which of the institute's staff heads the office, and folding the two together
would leave an affiliated institute unable to replace a departing HoD.
"""
import json

from django.test import RequestFactory, TestCase
from django.urls import reverse

from academics import views
from academics.curriculum import may_define_department, selectable_disciplines
from academics.forms import DepartmentForm
from academics.models import Department
from accounts.models import (
    Discipline,
    Institute,
    InstituteAffiliation,
    University,
    User,
)


class DepartmentFixture(TestCase):
    def setUp(self):
        self.university = University.objects.create(
            name="Engineering University", code="ENGGU", short_name="ENGGU",
            email="e@u.edu", grants_affiliation=True)
        self.admin = User.objects.create_user(
            email="admin@u.edu", password="Str0ngPass!23",
            role=User.Role.UNIVERSITY, university=self.university,
            registration_completed=True)

        self.institute = Institute.objects.create(
            name="Acme College", code="ACME", email="office@acme.edu",
            status=Institute.Status.APPROVED)
        # Engineering is the university's; general courses are the institute's.
        InstituteAffiliation.objects.create(
            institute=self.institute, discipline=Discipline.ENGG,
            university=self.university)
        InstituteAffiliation.objects.create(
            institute=self.institute, discipline=Discipline.GENERAL,
            university=None)

        # Adopted from the university's catalogue — which is what makes it
        # the university's to define now. The old rule keyed on the
        # discipline being affiliated; the link is the claim now, so the
        # fixture has to create one.
        from academics.models import UniversityDepartment

        self.entry = UniversityDepartment.objects.create(
            university=self.university, discipline=Discipline.ENGG,
            name="Computer Science", code="CSE")
        self.governed = Department.objects.create(
            institute=self.institute, code="CSE", name="Computer Science",
            discipline=Discipline.ENGG, source=self.entry)
        self.own = Department.objects.create(
            institute=self.institute, code="ARTS", name="Arts",
            discipline=Discipline.GENERAL)

        self.head = User.objects.create_user(
            email="head@acme.edu", password="Str0ngPass!23",
            role=User.Role.HEAD, institute=self.institute,
            registration_completed=True)

    def _post(self, view, user, pk=None, **data):
        request = RequestFactory().post("/", data)
        request.user = user
        request.session = self.client.session
        request.META["HTTP_X_REQUESTED_WITH"] = "XMLHttpRequest"
        return view(request, pk=pk) if pk is not None else view(request)

    def _body(self, response):
        return json.loads(response.content)


class SelectableDisciplineTests(DepartmentFixture):
    """Item 2: the dropdown offers only what the actor governs."""

    def test_an_institute_is_offered_only_its_autonomous_disciplines(self):
        offered = [d["value"] for d in
                   selectable_disciplines(self.head, self.institute)]
        self.assertEqual(offered, ["GENERAL"])

    def test_a_university_is_offered_only_what_it_affiliates(self):
        offered = [d["value"] for d in
                   selectable_disciplines(self.admin, self.institute)]
        self.assertEqual(offered, ["ENGG"])

    def test_an_institute_with_nothing_autonomous_is_offered_nothing(self):
        """
        Not an error — a real answer. It has no department of its own to
        create, and the page says so instead of showing an empty dropdown.
        """
        self.institute.affiliations.filter(university__isnull=True).delete()
        self.assertEqual(selectable_disciplines(self.head, self.institute), [])

    def test_another_universitys_discipline_is_offered_to_neither(self):
        other = University.objects.create(
            name="Health University", code="HU", email="h@u.edu")
        InstituteAffiliation.objects.create(
            institute=self.institute, discipline=Discipline.PHARMACY,
            university=other)
        for actor in (self.head, self.admin):
            with self.subTest(actor=actor.role):
                offered = [d["value"] for d in
                           selectable_disciplines(actor, self.institute)]
                self.assertNotIn("PHARMACY", offered)


class FormTests(DepartmentFixture):
    """Items 2 and 3, where the browser cannot be trusted."""

    def test_the_blank_option_is_gone_and_the_field_is_required(self):
        form = DepartmentForm(user=self.head, institute=self.institute)
        self.assertTrue(form.fields["discipline"].required)
        labels = [label for value, label in form.fields["discipline"].choices]
        self.assertNotIn("Not tied to one", labels)

    def test_a_discipline_the_institute_does_not_hold_is_refused(self):
        """
        The dropdown never offers it, so this can only arrive by hand — and a
        department the institute could not then edit is worse than a refusal.
        """
        form = DepartmentForm(
            {"name": "Mechanical", "code": "MECH", "discipline": "ENGG"},
            user=self.head, institute=self.institute)
        self.assertFalse(form.is_valid())
        self.assertIn("discipline", form.errors)

    def test_an_autonomous_discipline_is_accepted(self):
        form = DepartmentForm(
            {"name": "Commerce", "code": "COM", "discipline": "GENERAL"},
            user=self.head, institute=self.institute)
        self.assertTrue(form.is_valid(), form.errors)

    def test_leaving_it_blank_is_refused(self):
        form = DepartmentForm(
            {"name": "Commerce", "code": "COM", "discipline": ""},
            user=self.head, institute=self.institute)
        self.assertFalse(form.is_valid())


class DefinitionRightsTests(DepartmentFixture):
    def test_the_institute_may_define_its_own_department(self):
        self.assertTrue(may_define_department(self.head, self.own))

    def test_the_institute_may_not_define_a_governed_one(self):
        self.assertFalse(may_define_department(self.head, self.governed))

    def test_the_university_may_define_the_one_it_governs(self):
        self.assertTrue(may_define_department(self.admin, self.governed))

    def test_a_department_with_no_discipline_is_anybodys_in_scope(self):
        """Rows that predate the column keep working for their institute."""
        legacy = Department.objects.create(
            institute=self.institute, code="OLD", name="Legacy", discipline="")
        self.assertTrue(may_define_department(self.head, legacy))


class EndpointTests(DepartmentFixture):
    """Item 4 is the interesting one: the HoD stays the institute's."""

    def test_the_institute_can_change_the_hod_of_a_governed_department(self):
        response = self._post(
            views.api_department_save, self.head, pk=self.governed.pk,
            name="Computer Science", code="CSE", discipline="ENGG",
            hod_email="newhod@acme.edu")
        self.assertTrue(self._body(response)["success"], response.content)
        self.governed.refresh_from_db()
        self.assertIsNotNone(self.governed.hod)
        self.assertEqual(self.governed.hod.email, "newhod@acme.edu")

    def test_the_name_and_code_are_ignored_on_a_governed_department(self):
        """
        Ignored, not refused. Refusing the whole request would block the one
        change the institute *is* entitled to make.
        """
        response = self._post(
            views.api_department_save, self.head, pk=self.governed.pk,
            name="Renamed By Me", code="HIJACK", discipline="GENERAL",
            hod_email="newhod@acme.edu")
        self.assertTrue(self._body(response)["success"], response.content)
        self.governed.refresh_from_db()
        self.assertEqual(self.governed.name, "Computer Science")
        self.assertEqual(self.governed.code, "CSE")
        self.assertEqual(self.governed.discipline, "ENGG")

    def test_the_institute_can_still_rename_its_own_department(self):
        response = self._post(
            views.api_department_save, self.head, pk=self.own.pk,
            name="Arts and Humanities", code="ARTS", discipline="GENERAL",
            hod_email="")
        self.assertTrue(self._body(response)["success"], response.content)
        self.own.refresh_from_db()
        self.assertEqual(self.own.name, "Arts and Humanities")

    def test_an_adopted_department_can_still_be_closed_by_the_institute(self):
        """
        Changed deliberately: "the institute can delete or deactivate any
        department".

        Defining a department follows the affiliation; *closing* one does not.
        A college that has stopped running a course has stopped running it, and
        needing the university's permission to say so would leave a dead
        department on its screens indefinitely.
        """
        from core.enums import RowStatus

        response = self._post(views.api_department_delete, self.head,
                              pk=self.governed.pk)
        self.assertTrue(self._body(response)["success"], response.content)
        # Held records, so archived rather than deleted — the ordinary rule.
        if Department.objects.filter(pk=self.governed.pk).exists():
            self.governed.refresh_from_db()
            self.assertEqual(self.governed.status, RowStatus.ARCHIVED)

    def test_the_institutes_own_department_can_be_removed(self):
        response = self._post(views.api_department_delete, self.head,
                              pk=self.own.pk)
        self.assertTrue(self._body(response)["success"], response.content)


class RowPayloadTests(DepartmentFixture):
    """Items 1 and 5 need these on every row."""

    def _rows(self, user):
        self.client.force_login(user)
        return self.client.get(
            reverse("academics:api_departments"),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest").json()["data"]["rows"]

    def test_every_row_carries_the_institute_code(self):
        for row in self._rows(self.head):
            self.assertEqual(row["institute"], "ACME")
            self.assertEqual(row["institute_name"], "Acme College")

    def test_the_rows_say_which_are_governed_and_by_whom(self):
        by_code = {r["code"]: r for r in self._rows(self.head)}
        self.assertFalse(by_code["CSE"]["can_define"])
        self.assertEqual(by_code["CSE"]["governed_by"], "ENGGU")
        self.assertTrue(by_code["ARTS"]["can_define"])
        self.assertEqual(by_code["ARTS"]["governed_by"], "")

    def test_the_rows_carry_what_the_filters_need(self):
        """Item 5 — each filter reads a field, so each field has to be there."""
        for row in self._rows(self.head):
            for key in ("discipline", "can_define", "hod_status", "is_active",
                        "hod_email"):
                self.assertIn(key, row)

    def test_the_university_sees_its_own_department_as_definable(self):
        by_code = {r["code"]: r for r in self._rows(self.admin)}
        self.assertTrue(by_code["CSE"]["can_define"])
        # And not the one it does not affiliate.
        self.assertFalse(by_code["ARTS"]["can_define"])


class DepartmentCountTests(DepartmentFixture):
    """
    Item 1. The counts were four filtered `Count` annotations on one queryset —
    four unrelated reverse relations walked in a single statement, which on
    MongoDB fans out into a cross product. They are four grouped queries now,
    which cannot inflate each other.
    """

    def setUp(self):
        super().setUp()
        from academics.models import Batch, StudentProfile, Subject

        # Two live subjects and one archived, in the same department.
        for code, live in (("A1", True), ("A2", True), ("A3", False)):
            Subject.objects.create(department=self.own, code=code, name=code,
                                   semester=1, credits=3, is_active=live)
        # Two live batches, one archived.
        self.batches = [
            Batch.objects.create(department=self.own, label=label,
                                 start_year=2022, end_year=2026, is_active=live)
            for label, live in (("2022-26", True), ("2023-27", True),
                                ("2018-22", False))]
        # Three students: two live, one deactivated, plus one in a dead batch.
        for i, (live, batch) in enumerate(
                [(True, 0), (True, 0), (False, 0), (True, 2)]):
            user = User.objects.create_user(
                email=f"s{i}@acme.edu", password="Str0ngPass!23",
                role=User.Role.STUDENT, institute=self.institute)
            StudentProfile.objects.create(
                user=user, department=self.own, batch=self.batches[batch],
                class_roll=str(i), is_active=live)
        # Two teachers, one deactivated.
        for i, live in enumerate((True, True, False)):
            User.objects.create_user(
                email=f"t{i}@acme.edu", password="Str0ngPass!23",
                role=User.Role.TEACHER, institute=self.institute,
                department=self.own, is_active=live)

    def _row(self):
        from django.urls import reverse

        self.client.force_login(self.head)
        rows = self.client.get(
            reverse("academics:api_departments"),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest").json()["data"]["rows"]
        return {r["code"]: r for r in rows}["ARTS"]

    def test_subjects_count_only_the_active_ones(self):
        self.assertEqual(self._row()["subject_count"], 2)

    def test_batches_count_only_the_active_ones(self):
        self.assertEqual(self._row()["batch_count"], 2)

    def test_students_need_to_be_active_and_in_a_live_batch(self):
        """Two live in a live batch. The deactivated one and the one in the
        archived cohort are both out — the second used to be counted."""
        self.assertEqual(self._row()["student_count"], 2)

    def test_teachers_count_only_the_active_ones(self):
        self.assertEqual(self._row()["teacher_count"], 2)

    def test_the_counts_do_not_inflate_each_other(self):
        """
        The regression itself. With four annotations on one queryset each count
        was multiplied by the rows of the others; with two subjects, two batches
        and two teachers the student count came back as a multiple rather than
        2. Asserting all four together is what catches a fan-out — any one of
        them alone can look right by coincidence.
        """
        row = self._row()
        self.assertEqual(
            (row["subject_count"], row["batch_count"],
             row["student_count"], row["teacher_count"]),
            (2, 2, 2, 2))

    def test_an_archived_department_counts_its_live_rows(self):
        """
        The bug that motivated splitting status from revocation.

        An archived department used to need its own counting rule — "count
        everything" — because its contents had been *relabelled* archived and
        counting live rows found none. Nothing is relabelled now, so there is
        one rule: a department reports the rows that are running inside it,
        whatever the department's own state.
        """
        self.own.is_active = False
        self.own.save()
        row = self._row()
        self.assertEqual(
            (row["subject_count"], row["batch_count"],
             row["student_count"], row["teacher_count"]),
            (2, 2, 2, 2))

    def test_a_revoked_department_still_reports_its_students(self):
        """
        The exact symptom that was reported: a revoked department showing 0
        students while holding four. Revocation is a separate fact now and the
        count never looks at it.
        """
        self.institute.affiliations.filter(
            discipline=Discipline.GENERAL).delete()
        self.own.refresh_from_db()
        self.assertTrue(self.own.is_revoked)
        row = self._row()
        self.assertEqual(row["student_count"], 2)
        self.assertEqual(row["subject_count"], 2)

    def test_a_revoked_department_keeps_its_rows_status(self):
        """The other half: the students are revoked *and* still active."""
        from academics.models import StudentProfile
        from core.enums import RowStatus

        self.institute.affiliations.filter(
            discipline=Discipline.GENERAL).delete()
        live = StudentProfile.objects.filter(department=self.own,
                                             status=RowStatus.ACTIVE)
        # Three, not two: the third is in an archived *cohort*, which is a fact
        # about their batch and not about them. The department count excludes
        # them for that reason; their own status is untouched, which is exactly
        # the separation this change is about.
        self.assertEqual(live.count(), 3)
        self.assertTrue(all(s.is_revoked for s in live))

    def test_live_and_revoked_departments_are_counted_the_same_way(self):
        """
        One rule now, so there is no second pass to get out of step with the
        first — which is what the two-rule version risked.
        """
        from django.urls import reverse

        self.institute.affiliations.filter(
            discipline=Discipline.ENGG).delete()
        self.client.force_login(self.head)
        rows = {r["code"]: r for r in self.client.get(
            reverse("academics:api_departments"),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest").json()["data"]["rows"]}
        self.assertEqual(rows["ARTS"]["student_count"], 2)
        self.assertEqual(rows["CSE"]["student_count"], 0)   # genuinely empty

    def test_an_empty_department_counts_zero_rather_than_missing(self):
        row = {r["code"]: r for r in [self._row()]}
        # `governed` has nothing in it at all.
        from django.urls import reverse

        self.client.force_login(self.head)
        rows = {r["code"]: r for r in self.client.get(
            reverse("academics:api_departments"),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest").json()["data"]["rows"]}
        self.assertEqual(rows["CSE"]["student_count"], 0)
        self.assertEqual(rows["CSE"]["subject_count"], 0)


class FilterListTests(DepartmentFixture):
    """
    Filters offer every department; forms offer only the live ones.

    Two lists because they answer two questions. Filing a new subject under an
    archived department creates a row that is archived the moment it exists;
    but a filter that omits archived departments makes every archived row in
    the table it filters unfindable.
    """

    def _context(self, url_name):
        from django.urls import reverse

        self.client.force_login(self.head)
        return self.client.get(reverse(url_name)).context

    def setUp(self):
        super().setUp()
        self.own.is_active = False       # archived, but still real
        self.own.save()

    def test_the_subject_filter_offers_the_archived_department(self):
        context = self._context("academics:subjects")
        self.assertIn("ARTS", {d.code for d in context["filter_departments"]})

    def test_the_subject_form_does_not(self):
        context = self._context("academics:subjects")
        self.assertNotIn("ARTS", {d.code for d in context["departments"]})

    def test_the_batch_filter_offers_it_and_the_form_does_not(self):
        context = self._context("academics:batches")
        self.assertIn("ARTS", {d.code for d in context["filter_departments"]})
        self.assertNotIn("ARTS", {d.code for d in context["departments"]})

    def test_the_student_filter_offers_it_and_the_import_target_does_not(self):
        context = self._context("academics:students")
        self.assertIn("ARTS", {d.code for d in context["filter_departments"]})
        self.assertNotIn("ARTS", {d.code for d in context["departments"]})

    def test_the_teacher_filter_offers_it_and_the_invite_list_does_not(self):
        context = self._context("academics:teachers")
        self.assertIn("ARTS", {d.code for d in context["filter_departments"]})
        self.assertNotIn("ARTS", {d.code for d in context["departments"]})

    def test_a_revoked_department_is_offered_by_the_filter_too(self):
        """The stronger case: its rows are the hardest to find otherwise."""
        self.institute.affiliations.filter(
            discipline=Discipline.ENGG).delete()
        context = self._context("academics:subjects")
        self.assertIn("CSE", {d.code for d in context["filter_departments"]})
