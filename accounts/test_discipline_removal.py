"""
Removing a discipline an institute holds autonomously.

Two decisions are being tested, and the second is the dangerous one:

* **which** disciplines an institute may remove — its own, never a
  university's;
* **what happens to the data inside it** — archived or left alone, chosen
  explicitly, and never deleted.

Archiving rather than deleting is the point. Deleting the students would take
their attendance, feedback and absence history with them, and an institute
tidying up a discipline it no longer offers is not asking to destroy three
years of records. Every test below that says "archived" also checks the rows
are still there.
"""
import json

from django.test import RequestFactory, TestCase

from accounts import views
from accounts.affiliations import AffiliationError, contents_of, remove_own
from accounts.models import (
    Discipline,
    Institute,
    InstituteAffiliation,
    University,
    User,
)
from academics.models import Batch, Department, StudentProfile, Subject


class RemovalFixture(TestCase):
    def setUp(self):
        self.university = University.objects.create(
            name="Engineering University", code="ENGGU", short_name="ENGGU",
            email="e@u.edu", grants_affiliation=True)
        self.institute = Institute.objects.create(
            name="Acme College", code="ACME", email="office@acme.edu",
            status=Institute.Status.APPROVED)
        # One affiliated, two autonomous — so removing one still leaves the
        # institute teaching something.
        InstituteAffiliation.objects.create(
            institute=self.institute, discipline=Discipline.ENGG,
            university=self.university)
        InstituteAffiliation.objects.create(
            institute=self.institute, discipline=Discipline.GENERAL,
            university=None)
        InstituteAffiliation.objects.create(
            institute=self.institute, discipline=Discipline.PHARMACY,
            university=None)

        self.head = User.objects.create_user(
            email="head@acme.edu", password="Str0ngPass!23",
            role=User.Role.HEAD, institute=self.institute,
            registration_completed=True)

        # A populated autonomous discipline.
        self.arts = Department.objects.create(
            institute=self.institute, code="ARTS", name="Arts",
            discipline=Discipline.GENERAL)
        self.subject = Subject.objects.create(
            department=self.arts, code="ENG", name="English",
            semester=1, credits=3)
        self.batch = Batch.objects.create(
            department=self.arts, label="2022-25",
            start_year=2022, end_year=2025)
        self.teacher = User.objects.create_user(
            email="t@acme.edu", password="Str0ngPass!23", full_name="A Teacher",
            role=User.Role.TEACHER, institute=self.institute,
            department=self.arts, registration_completed=True)
        student_user = User.objects.create_user(
            email="s@acme.edu", password="Str0ngPass!23", full_name="A Student",
            role=User.Role.STUDENT, institute=self.institute)
        self.student = StudentProfile.objects.create(
            user=student_user, department=self.arts, batch=self.batch,
            class_roll="1")

    def _post(self, view, user, **data):
        request = RequestFactory().post("/", data)
        request.user = user
        request.session = self.client.session
        request.META["HTTP_X_REQUESTED_WITH"] = "XMLHttpRequest"
        return view(request)

    def _body(self, response):
        return json.loads(response.content)

    def _codes(self):
        return set(self.institute.affiliations.values_list(
            "discipline", flat=True))


class ContentsTests(RemovalFixture):
    def test_the_counts_describe_what_is_in_the_discipline(self):
        counts = contents_of(self.institute, Discipline.GENERAL)
        self.assertEqual(counts, {"departments": 1, "subjects": 1, "batches": 1,
                                  "students": 1, "teachers": 1})

    def test_an_empty_discipline_counts_zero_rather_than_failing(self):
        counts = contents_of(self.institute, Discipline.PHARMACY)
        self.assertEqual(sum(counts.values()), 0)

    def test_already_archived_rows_are_not_counted(self):
        """They are not something the person is about to lose."""
        self.batch.is_active = False
        self.batch.save()
        self.assertEqual(
            contents_of(self.institute, Discipline.GENERAL)["batches"], 0)


class ScopeTests(RemovalFixture):
    def test_an_autonomous_discipline_can_be_removed(self):
        remove_own(institute=self.institute, discipline=Discipline.PHARMACY,
                   archive=False, actor=self.head)
        self.assertNotIn("PHARMACY", self._codes())

    def test_an_affiliated_discipline_cannot_be_and_the_message_names_them(self):
        with self.assertRaises(AffiliationError) as caught:
            remove_own(institute=self.institute, discipline=Discipline.ENGG,
                       archive=False, actor=self.head)
        self.assertIn("ENGGU", str(caught.exception))
        self.assertIn("ENGG", self._codes())

    def test_the_last_discipline_cannot_be_removed(self):
        self.institute.affiliations.exclude(
            discipline=Discipline.PHARMACY).delete()
        with self.assertRaises(AffiliationError) as caught:
            remove_own(institute=self.institute, discipline=Discipline.PHARMACY,
                       archive=False, actor=self.head)
        self.assertIn("only discipline", str(caught.exception))

    def test_a_discipline_not_on_file_is_refused(self):
        with self.assertRaises(AffiliationError):
            remove_own(institute=self.institute, discipline=Discipline.AGRI,
                       archive=False, actor=self.head)


class KeepTests(RemovalFixture):
    """`keep`: unlist the discipline, leave everything running."""

    def setUp(self):
        super().setUp()
        remove_own(institute=self.institute, discipline=Discipline.GENERAL,
                   archive=False, actor=self.head)

    def test_the_discipline_is_gone_from_the_list(self):
        self.assertNotIn("GENERAL", self._codes())

    def test_the_department_and_everything_in_it_is_untouched(self):
        self.arts.refresh_from_db()
        self.subject.refresh_from_db()
        self.batch.refresh_from_db()
        self.student.refresh_from_db()
        self.teacher.refresh_from_db()
        self.assertTrue(self.arts.is_active)
        self.assertTrue(self.subject.is_active)
        self.assertTrue(self.batch.is_active)
        self.assertTrue(self.student.is_active)
        self.assertTrue(self.teacher.is_active)

    def test_the_orphaned_department_stays_the_institutes_to_manage(self):
        """
        Its discipline is no longer held by anyone, which is the same state as
        a department created before the column existed: governed by nobody.
        """
        from academics.curriculum import may_define_department

        self.arts.refresh_from_db()
        self.assertTrue(may_define_department(self.head, self.arts))


class ArchiveTests(RemovalFixture):
    """`archive`: hide it all, delete none of it."""

    def setUp(self):
        super().setUp()
        self.result = remove_own(
            institute=self.institute, discipline=Discipline.GENERAL,
            archive=True, actor=self.head)

    def test_everything_is_deactivated(self):
        self.arts.refresh_from_db()
        self.subject.refresh_from_db()
        self.batch.refresh_from_db()
        self.student.refresh_from_db()
        self.teacher.refresh_from_db()
        self.assertFalse(self.arts.is_active)
        self.assertFalse(self.subject.is_active)
        self.assertFalse(self.batch.is_active)
        self.assertFalse(self.student.is_active)
        self.assertFalse(self.teacher.is_active)

    def test_nothing_is_deleted(self):
        """The whole reason archiving was chosen over deleting."""
        self.assertTrue(Department.objects.filter(pk=self.arts.pk).exists())
        self.assertTrue(Subject.objects.filter(pk=self.subject.pk).exists())
        self.assertTrue(Batch.objects.filter(pk=self.batch.pk).exists())
        self.assertTrue(StudentProfile.objects.filter(pk=self.student.pk).exists())
        self.assertTrue(User.objects.filter(pk=self.teacher.pk).exists())

    def test_the_answer_reports_what_it_touched(self):
        self.assertTrue(self.result["archived"])
        self.assertEqual(self.result["counts"]["students"], 1)

    def test_re_adding_the_discipline_does_not_silently_revive_it(self):
        """
        Worth pinning down. The rows are still archived after re-adding — an
        institute reopening a wing decides for itself which cohorts come back,
        and un-archiving three years of students on a checkbox click would be a
        surprise nobody asked for.
        """
        from accounts.affiliations import add_autonomous

        add_autonomous(institute=self.institute, disciplines=["GENERAL"],
                       actor=self.head)
        self.arts.refresh_from_db()
        self.assertFalse(self.arts.is_active)
        self.assertIn("GENERAL", self._codes())


class QueryShapeTests(RemovalFixture):
    """
    The archive writes must not contain a subquery.

    This is the bug that shipped: `department__in=<queryset>` inside an
    `update()` is a correlated subquery, which sqlite runs happily and
    django_mongodb_backend cannot express at all — Atlas answers
    "$in requires an array as a second argument, found: missing".

    So no functional test could have caught it. This one reads the SQL the ORM
    would generate and asserts there is no nested SELECT, which is backend
    -independent and fails on the shape rather than on the execution.
    """

    def _sql(self, queryset):
        return str(queryset.query)

    def test_the_archive_filters_carry_no_nested_select(self):
        from academics.models import Batch, Department, StudentProfile, Subject

        departments = Department.objects.filter(
            institute=self.institute, discipline=Discipline.GENERAL)
        ids = list(departments.values_list("id", flat=True))
        for queryset in (
            Subject.objects.filter(department_id__in=ids),
            Batch.objects.filter(department_id__in=ids),
            StudentProfile.objects.filter(department_id__in=ids),
            User.objects.filter(department_id__in=ids, role=User.Role.TEACHER),
            Department.objects.filter(id__in=ids),
        ):
            with self.subTest(model=queryset.model.__name__):
                self.assertNotIn("SELECT", self._sql(queryset).upper()[6:],
                                 "a subquery crept back in")

    def test_the_bad_shape_is_what_this_is_guarding_against(self):
        """
        The control. If passing a queryset stopped producing a subquery, the
        test above would be asserting nothing and nobody would notice.
        """
        from academics.models import Department, Subject

        departments = Department.objects.filter(
            institute=self.institute, discipline=Discipline.GENERAL)
        bad = Subject.objects.filter(department__in=departments)
        self.assertIn("SELECT", str(bad.query).upper()[6:])


class RevokedStatusTests(RemovalFixture):
    """
    Rows whose discipline is no longer on the institute's record.

    Deliberately not inferred from `is_active`. A batch archived on its own is
    a cohort that finished; a batch in a revoked discipline is part of a wing
    the institute has stopped offering. Showing both as "Archived" would hide
    the difference exactly where it matters.
    """

    def _rows(self, url_name, **params):
        from django.urls import reverse

        self.client.force_login(self.head)
        return self.client.get(
            reverse(url_name), params,
            HTTP_X_REQUESTED_WITH="XMLHttpRequest").json()["data"]["rows"]

    def _revoke(self, archive):
        remove_own(institute=self.institute, discipline=Discipline.GENERAL,
                   archive=archive, actor=self.head)

    def test_nothing_is_revoked_while_the_discipline_is_on_file(self):
        for url in ("academics:api_subjects", "academics:api_batches",
                    "academics:api_departments", "academics:api_students",
                    "academics:api_teachers"):
            with self.subTest(url=url):
                self.assertTrue(self._rows(url))
                self.assertFalse(any(r["revoked"] for r in self._rows(url)), url)

    def test_every_table_flags_the_rows_after_the_discipline_goes(self):
        self._revoke(archive=True)
        for url in ("academics:api_departments", "academics:api_teachers"):
            with self.subTest(url=url):
                rows = self._rows(url)
                self.assertTrue(rows, url)
                self.assertTrue(all(r["revoked"] for r in rows), url)

    def test_archiving_hides_the_students_rather_than_flagging_them(self):
        """
        Not an oversight — the older rule wins, and it should.

        Archiving a batch has always made its students, sessions, records and
        every statistic derived from them vanish from the whole application.
        Archiving a discipline archives its batches, so its students go the
        same way. A "Revoked" pill on a row nobody can see would be decoration.

        They are not deleted, and restoring the batch brings them all back
        still flagged — which the next test shows.
        """
        self._revoke(archive=True)
        self.assertEqual(self._rows("academics:api_students"), [])

    def test_a_student_whose_batch_is_restored_comes_back_flagged(self):
        self._revoke(archive=True)
        self.batch.is_active = True
        self.batch.save()
        rows = self._rows("academics:api_students")
        self.assertTrue(rows)
        self.assertTrue(all(r["revoked"] for r in rows))
        self.assertEqual({r["status"] for r in rows}, {"REVOKED"})

    def test_students_kept_rather_than_archived_show_the_status(self):
        """
        The case the pill is actually for: the discipline is gone, the data was
        kept, and the rows need to say which wing they belong to.
        """
        self._revoke(archive=False)
        rows = self._rows("academics:api_students")
        self.assertTrue(rows)
        self.assertEqual({r["status"] for r in rows}, {"REVOKED"})

    def test_teachers_report_the_status_string(self):
        """`GA.statusPill` renders REVOKED as a red pill; it has to arrive."""
        self._revoke(archive=True)
        self.assertEqual({r["status"] for r in self._rows("academics:api_teachers")},
                         {"REVOKED"})

    def test_keeping_the_data_still_marks_it_revoked(self):
        """
        The discipline is gone either way. "Keep" decides whether the rows stay
        *active*, not whether they still belong to something.
        """
        self._revoke(archive=False)
        rows = self._rows("academics:api_departments")
        self.assertTrue(all(r["revoked"] for r in rows))
        self.assertTrue(all(r["is_active"] for r in rows))

    def test_an_archived_row_in_a_live_discipline_is_not_revoked(self):
        """The distinction the flag exists to draw."""
        self.arts.is_active = False
        self.arts.save()
        rows = {r["code"]: r for r in self._rows("academics:api_departments")}
        self.assertFalse(rows["ARTS"]["revoked"])
        self.assertFalse(rows["ARTS"]["is_active"])

    def test_a_department_with_no_discipline_is_never_revoked(self):
        """It belongs to nothing by design, which is not the same as orphaned."""
        Department.objects.create(institute=self.institute, code="OLD",
                                  name="Legacy", discipline="")
        rows = {r["code"]: r for r in self._rows("academics:api_departments")}
        self.assertFalse(rows["OLD"]["revoked"])


class EndpointTests(RemovalFixture):
    def test_the_choice_is_required_rather_than_defaulted(self):
        response = self._post(views.api_remove_discipline, self.head,
                              discipline="PHARMACY")
        self.assertFalse(self._body(response)["success"])
        self.assertIn("PHARMACY", self._codes())

    def test_keep_is_honoured(self):
        response = self._post(views.api_remove_discipline, self.head,
                              discipline="GENERAL", contents="keep")
        self.assertTrue(self._body(response)["success"], response.content)
        self.arts.refresh_from_db()
        self.assertTrue(self.arts.is_active)

    def test_archive_is_honoured_and_the_message_says_nothing_was_deleted(self):
        response = self._post(views.api_remove_discipline, self.head,
                              discipline="GENERAL", contents="archive")
        body = self._body(response)
        self.assertTrue(body["success"], response.content)
        self.assertIn("Nothing was deleted", body["message"])
        self.arts.refresh_from_db()
        self.assertFalse(self.arts.is_active)

    def test_an_affiliated_discipline_is_refused_by_the_endpoint_too(self):
        response = self._post(views.api_remove_discipline, self.head,
                              discipline="ENGG", contents="archive")
        self.assertEqual(response.status_code, 403)
        self.assertIn("ENGG", self._codes())

    def test_a_teacher_cannot_remove_a_discipline(self):
        response = self._post(views.api_remove_discipline, self.teacher,
                              discipline="GENERAL", contents="archive")
        self.assertEqual(response.status_code, 403)

    def test_the_removed_discipline_is_offered_for_adding_again(self):
        """The requirement, end to end."""
        from accounts.affiliations import available_disciplines

        self._post(views.api_remove_discipline, self.head,
                   discipline="GENERAL", contents="keep")
        offered = [d["value"] for d in available_disciplines(self.institute)]
        self.assertIn("GENERAL", offered)


class ContentsEndpointTests(RemovalFixture):
    def _get(self, user, code):
        request = RequestFactory().get("/")
        request.user = user
        request.session = self.client.session
        request.META["HTTP_X_REQUESTED_WITH"] = "XMLHttpRequest"
        return views.api_discipline_contents(request, code=code)

    def test_it_reports_the_counts_the_modal_shows(self):
        body = self._body(self._get(self.head, "GENERAL"))
        self.assertTrue(body["success"])
        self.assertEqual(body["data"]["counts"]["students"], 1)
        self.assertTrue(body["data"]["autonomous"])

    def test_it_says_who_holds_an_affiliated_discipline(self):
        body = self._body(self._get(self.head, "ENGG"))
        self.assertFalse(body["data"]["autonomous"])
        self.assertEqual(body["data"]["university"], "ENGGU")

    def test_an_unknown_code_is_refused(self):
        self.assertFalse(self._body(self._get(self.head, "NONSENSE"))["success"])
