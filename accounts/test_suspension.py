"""
A university suspending a teacher of an institute it affiliates.

Three groups: who may act, what the flag does and does not touch, and who gets
told. The middle group is the one worth reading — suspension is a *fourth*
orthogonal fact about a row, and most of these tests exist to catch it being
quietly folded into `status`.
"""
import json

from django.core import mail
from django.test import RequestFactory, TestCase
from django.urls import reverse

from academics import views
from academics.models import Department
from accounts import suspension
from accounts.institute_approval import sign_in_blocked_reason
from accounts.models import (
    Discipline,
    Institute,
    InstituteAffiliation,
    University,
    UniversityDiscipline,
    User,
)
from core.enums import RowStatus, SUSPENDED_KEY

REASON = "Repeated unexplained absence from allocated classes since March."


class SuspensionFixture(TestCase):
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
            name="Acme", code="ACME", email="office@acme.edu",
            status=Institute.Status.APPROVED)
        InstituteAffiliation.objects.create(
            institute=self.institute, discipline=Discipline.ENGG,
            university=self.university)
        InstituteAffiliation.objects.create(
            institute=self.institute, discipline=Discipline.GENERAL,
            university=None)
        self.head = User.objects.create_user(
            email="head@acme.edu", password="Str0ngPass!23",
            role=User.Role.HEAD, institute=self.institute,
            registration_completed=True)

        self.department = Department.objects.create(
            institute=self.institute, code="CSE", name="Computer Science",
            discipline=Discipline.ENGG)
        self.hod = User.objects.create_user(
            email="hod@acme.edu", password="Str0ngPass!23", role=User.Role.HOD,
            institute=self.institute, department=self.department,
            registration_completed=True)
        self.department.hod = self.hod
        self.department.save(update_fields=["hod"])

        self.teacher = User.objects.create_user(
            email="teacher@acme.edu", password="Str0ngPass!23",
            role=User.Role.TEACHER, institute=self.institute,
            department=self.department, full_name="Asha Rao",
            registration_completed=True)

        # A teacher in the college's autonomous discipline: nobody affiliates
        # them, so nobody may suspend them.
        self.own_department = Department.objects.create(
            institute=self.institute, code="ARTS", name="Arts",
            discipline=Discipline.GENERAL)
        self.autonomous_teacher = User.objects.create_user(
            email="arts@acme.edu", password="Str0ngPass!23",
            role=User.Role.TEACHER, institute=self.institute,
            department=self.own_department, full_name="Ravi Nair",
            registration_completed=True)
        mail.outbox = []

    def call(self, view, user, pk=None, **data):
        request = RequestFactory().post("/", data)
        request.user = user
        request.session = self.client.session
        request.META["HTTP_X_REQUESTED_WITH"] = "XMLHttpRequest"
        return view(request, pk=pk) if pk is not None else view(request)

    def body(self, response):
        return json.loads(response.content)

    def suspend(self, user=None, teacher=None, reason=REASON):
        return self.call(views.api_teacher_suspend, user or self.admin,
                         pk=(teacher or self.teacher).pk, reason=reason)


class WhoMayActTests(SuspensionFixture):
    def test_the_affiliating_university_may(self):
        self.assertTrue(self.body(self.suspend())["success"])
        self.teacher.refresh_from_db()
        self.assertTrue(self.teacher.is_suspended)

    def test_a_teacher_in_an_autonomous_discipline_may_not_be_suspended(self):
        """
        Nobody affiliates them, so there is no body whose rules they are under.
        """
        response = self.suspend(teacher=self.autonomous_teacher)
        self.assertEqual(response.status_code, 403)
        self.autonomous_teacher.refresh_from_db()
        self.assertFalse(self.autonomous_teacher.is_suspended)

    def test_a_university_affiliating_a_different_discipline_may_not(self):
        """
        A college can have engineering under one body and pharmacy under
        another. Reaching the institute is not the same as governing this
        teacher's department.
        """
        other = University.objects.create(name="Pharma U", code="PHU",
                                          email="p@p.edu",
                                          grants_affiliation=True)
        UniversityDiscipline.objects.create(university=other,
                                            discipline=Discipline.PHARMACY)
        InstituteAffiliation.objects.create(
            institute=self.institute, discipline=Discipline.PHARMACY,
            university=other)
        stranger = User.objects.create_user(
            email="a@p.edu", password="Str0ngPass!23",
            role=User.Role.UNIVERSITY, university=other,
            registration_completed=True)
        self.assertFalse(suspension.may_suspend(stranger, self.teacher))

    def test_the_institute_head_cannot_suspend(self):
        response = self.suspend(user=self.head)
        self.assertIn(response.status_code, (302, 403))
        self.teacher.refresh_from_db()
        self.assertFalse(self.teacher.is_suspended)

    def test_a_reason_is_required(self):
        response = self.suspend(reason="")
        self.assertEqual(response.status_code, 403)
        self.teacher.refresh_from_db()
        self.assertFalse(self.teacher.is_suspended)

    def test_a_one_word_reason_is_refused(self):
        """
        It is quoted into an email somebody has to answer. "Misconduct" is not
        something anyone can answer.
        """
        self.assertEqual(self.suspend(reason="bad").status_code, 403)

    def test_suspending_twice_is_refused_rather_than_silently_reapplied(self):
        self.suspend()
        response = self.suspend(reason="Something else entirely, at length.")
        self.assertEqual(response.status_code, 403)
        self.teacher.refresh_from_db()
        self.assertEqual(self.teacher.suspension_reason, REASON)


class OrthogonalityTests(SuspensionFixture):
    """
    The point of the whole design: four facts, four fields, no folding.
    """

    def test_status_and_is_active_are_untouched(self):
        self.suspend()
        self.teacher.refresh_from_db()
        self.assertEqual(self.teacher.status, RowStatus.ACTIVE)
        self.assertTrue(self.teacher.is_active)

    def test_the_row_still_reads_as_suspended_in_the_status_column(self):
        self.suspend()
        self.teacher.refresh_from_db()
        self.assertEqual(views._row_status(self.teacher), SUSPENDED_KEY)

    def test_suspension_leads_over_revocation(self):
        """
        A revoked row is explained by its discipline, which is in the next
        column. A suspension is about this one person and is the thing
        somebody has to act on.
        """
        self.suspend()
        self.teacher.refresh_from_db()
        self.teacher.is_revoked = True
        self.assertEqual(views._row_status(self.teacher), SUSPENDED_KEY)

    def test_lifting_restores_the_status_the_institute_had_set(self):
        """
        The reason this is not a status value. Had the suspension overwritten
        ACTIVE, there would be nothing to put back here.
        """
        self.teacher.is_active = False
        self.teacher.save()
        self.assertEqual(self.teacher.status, RowStatus.ARCHIVED)
        self.suspend()
        self.call(views.api_teacher_lift_suspension, self.admin,
                  pk=self.teacher.pk)
        self.teacher.refresh_from_db()
        self.assertFalse(self.teacher.is_suspended)
        self.assertEqual(self.teacher.status, RowStatus.ARCHIVED)

    def test_a_save_elsewhere_does_not_clear_the_flag(self):
        """
        `User.save` rewrites `status` on every write. If suspension lived
        there, an unrelated edit would quietly lift it.
        """
        self.suspend()
        self.teacher.refresh_from_db()
        self.teacher.full_name = "Asha R. Rao"
        self.teacher.save()
        self.teacher.refresh_from_db()
        self.assertTrue(self.teacher.is_suspended)

    def test_assignments_and_records_are_not_touched(self):
        from academics.models import TeacherAssignment

        self.suspend()
        self.assertEqual(
            TeacherAssignment.objects.filter(teacher=self.teacher,
                                             is_active=False).count(), 0)


class SignInTests(SuspensionFixture):
    def test_a_suspended_teacher_cannot_sign_in(self):
        self.suspend()
        self.teacher.refresh_from_db()
        reason = sign_in_blocked_reason(self.teacher)
        self.assertIsNotNone(reason)
        self.assertIn("ENGGU", reason)

    def test_the_reason_is_quoted_so_they_can_answer_it(self):
        self.suspend()
        self.teacher.refresh_from_db()
        self.assertIn(REASON, sign_in_blocked_reason(self.teacher))

    def test_it_says_who_can_lift_it(self):
        self.suspend()
        self.teacher.refresh_from_db()
        self.assertIn("your institute cannot",
                      sign_in_blocked_reason(self.teacher))

    def test_the_login_endpoint_refuses_them(self):
        self.suspend()
        response = self.client.post(
            reverse("accounts:api_login"),
            {"email": "teacher@acme.edu", "password": "Str0ngPass!23"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        body = json.loads(response.content)
        self.assertFalse(body["success"])
        self.assertIn("suspended", body["message"].lower())

    def test_an_unsuspended_teacher_is_not_blocked(self):
        self.assertIsNone(sign_in_blocked_reason(self.teacher))

    def test_lifting_lets_them_back_in(self):
        self.suspend()
        self.call(views.api_teacher_lift_suspension, self.admin,
                  pk=self.teacher.pk)
        self.teacher.refresh_from_db()
        self.assertIsNone(sign_in_blocked_reason(self.teacher))


class NotificationTests(SuspensionFixture):
    def test_all_three_are_emailed(self):
        self.suspend()
        self.assertEqual(len(mail.outbox), 1)
        recipients = set(mail.outbox[0].to)
        self.assertEqual(recipients, {"teacher@acme.edu", "hod@acme.edu",
                                      "head@acme.edu"})

    def test_login_addresses_not_the_institutes_letterhead(self):
        """
        `Institute.email` is a public contact nobody signs in to — see
        accounts/recipients.py, where addressing it silently discarded 112
        messages.
        """
        self.suspend()
        self.assertNotIn("office@acme.edu", mail.outbox[0].to)

    def test_the_reason_is_in_the_message(self):
        self.suspend()
        self.assertIn(REASON, mail.outbox[0].body)

    def test_one_message_rather_than_three(self):
        """
        A suspension the teacher can see their HoD was copied on is one that
        will not be quietly disputed later.
        """
        self.suspend()
        self.assertEqual(len(mail.outbox), 1)

    def test_one_person_holding_two_roles_is_emailed_once(self):
        self.department.hod = self.head
        self.department.save(update_fields=["hod"])
        self.suspend()
        self.assertEqual(sorted(mail.outbox[0].to),
                         ["head@acme.edu", "teacher@acme.edu"])

    def test_a_department_with_no_hod_still_reaches_the_other_two(self):
        self.department.hod = None
        self.department.save(update_fields=["hod"])
        self.suspend()
        self.assertEqual(set(mail.outbox[0].to),
                         {"teacher@acme.edu", "head@acme.edu"})

    def test_lifting_tells_them_too(self):
        self.suspend()
        mail.outbox = []
        self.call(views.api_teacher_lift_suspension, self.admin,
                  pk=self.teacher.pk)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("lifted", mail.outbox[0].subject.lower())

    def test_a_mail_failure_does_not_undo_the_suspension(self):
        """
        The decision is the record; the email is the courtesy. Losing the
        second must not lose the first.
        """
        from unittest.mock import patch

        with patch("accounts.emails.send_teacher_suspension",
                   side_effect=RuntimeError("provider down")):
            response = self.suspend()
        self.assertTrue(self.body(response)["success"])
        self.teacher.refresh_from_db()
        self.assertTrue(self.teacher.is_suspended)


class LiftingTests(SuspensionFixture):
    def _stranger(self, *, affiliates=None):
        other = University.objects.create(name="Other", code="OTH",
                                          email="o@o.edu",
                                          grants_affiliation=True)
        if affiliates:
            UniversityDiscipline.objects.create(university=other,
                                                discipline=affiliates)
            InstituteAffiliation.objects.create(
                institute=self.institute, discipline=affiliates,
                university=other)
        return User.objects.create_user(
            email="s@o.edu", password="Str0ngPass!23",
            role=User.Role.UNIVERSITY, university=other,
            registration_completed=True)

    def test_a_university_that_does_not_reach_the_institute_gets_a_404(self):
        """
        Scoped out one layer earlier than the permission check, and rightly:
        a body with no relationship to the college should not learn that this
        teacher exists, let alone that they are suspended.
        """
        from django.http import Http404

        self.suspend()
        with self.assertRaises(Http404):
            self.call(views.api_teacher_lift_suspension,
                      self._stranger(), pk=self.teacher.pk)
        self.teacher.refresh_from_db()
        self.assertTrue(self.teacher.is_suspended)

    def test_a_second_affiliating_university_still_may_not_lift_it(self):
        """
        This one *can* see the teacher — it affiliates the same college for
        pharmacy — which is exactly why the permission check has to exist
        separately from the scoping.
        """
        self.suspend()
        stranger = self._stranger(affiliates=Discipline.PHARMACY)
        response = self.call(views.api_teacher_lift_suspension, stranger,
                             pk=self.teacher.pk)
        self.assertEqual(response.status_code, 403)
        self.assertIn("ENGGU", self.body(response)["message"])
        self.teacher.refresh_from_db()
        self.assertTrue(self.teacher.is_suspended)

    def test_it_can_still_be_lifted_after_the_discipline_is_delinked(self):
        """
        Checked against `suspended_by`, not the current affiliation. Otherwise
        a delink would strand the teacher barred by a university that could no
        longer reach them.
        """
        self.suspend()
        InstituteAffiliation.objects.filter(
            institute=self.institute, discipline=Discipline.ENGG
        ).update(university=None)
        self.teacher.refresh_from_db()
        suspension.lift(teacher=self.teacher, reason="", actor=self.admin)
        self.teacher.refresh_from_db()
        self.assertFalse(self.teacher.is_suspended)

    def test_lifting_an_unsuspended_teacher_is_refused(self):
        response = self.call(views.api_teacher_lift_suspension, self.admin,
                             pk=self.teacher.pk)
        self.assertEqual(response.status_code, 403)

    def test_lifting_clears_the_reason_and_the_issuer(self):
        self.suspend()
        self.call(views.api_teacher_lift_suspension, self.admin,
                  pk=self.teacher.pk)
        self.teacher.refresh_from_db()
        self.assertEqual(self.teacher.suspension_reason, "")
        self.assertIsNone(self.teacher.suspended_at)
        self.assertIsNone(self.teacher.suspended_by_id)


class InstituteCannotUndoTests(SuspensionFixture):
    def test_the_head_cannot_reactivate_a_suspended_teacher(self):
        """
        An institute able to clear a suspension by ticking Active would make
        the whole thing a note rather than a sanction.
        """
        self.suspend()
        self.teacher.refresh_from_db()
        self.teacher.is_active = False
        self.teacher.save()
        response = self.call(views.api_teacher_toggle, self.head,
                             pk=self.teacher.pk)
        self.assertEqual(response.status_code, 403)
        self.teacher.refresh_from_db()
        self.assertFalse(self.teacher.is_active)

    def test_the_refusal_names_who_to_ask(self):
        self.suspend()
        self.teacher.refresh_from_db()
        self.teacher.is_active = False
        self.teacher.save()
        body = self.body(self.call(views.api_teacher_toggle, self.head,
                                   pk=self.teacher.pk))
        self.assertIn("ENGGU", body["message"])

    def test_the_head_cannot_deactivate_them_either(self):
        """
        The sharper half of the rule. Archiving *releases the PAN* — see
        accounts/pan.py, where the "one college at a time" test keys on "not
        archived". A college able to archive a suspended teacher could hand
        them to the next college with the bar still standing, and the sanction
        would follow nobody.
        """
        self.suspend()
        self.teacher.refresh_from_db()
        self.assertTrue(self.teacher.is_active)
        response = self.call(views.api_teacher_toggle, self.head,
                             pk=self.teacher.pk)
        self.assertEqual(response.status_code, 403)
        self.teacher.refresh_from_db()
        self.assertTrue(self.teacher.is_active)

    def test_archiving_them_would_have_released_the_pan(self):
        """
        The escape hatch the rule above closes, demonstrated rather than
        asserted: with the row archived, `pan.holder` reports nobody holding
        it and another college could add them.
        """
        from accounts import pan as pan_rules

        self.teacher.pan_number = "ABCDE1234F"
        self.teacher.save(update_fields=["pan_number"])
        self.suspend()
        self.teacher.refresh_from_db()
        self.assertIsNotNone(pan_rules.holder("ABCDE1234F"))
        # What the head is now prevented from doing:
        self.teacher.is_active = False
        self.teacher.save()
        self.assertIsNone(pan_rules.holder("ABCDE1234F"))

    def test_the_hod_cannot_edit_them(self):
        self.suspend()
        response = self.call(views.api_teacher_assignments_save, self.hod,
                             pk=self.teacher.pk, full_name="Someone Else")
        self.assertEqual(response.status_code, 403)
        self.teacher.refresh_from_db()
        self.assertEqual(self.teacher.full_name, "Asha Rao")

    def test_the_head_cannot_edit_them(self):
        self.suspend()
        response = self.call(views.api_teacher_assignments_save, self.head,
                             pk=self.teacher.pk, full_name="Someone Else")
        self.assertEqual(response.status_code, 403)
        self.teacher.refresh_from_db()
        self.assertEqual(self.teacher.full_name, "Asha Rao")

    def test_the_refusal_says_the_record_is_frozen_and_names_the_university(self):
        self.suspend()
        body = self.body(self.call(views.api_teacher_toggle, self.head,
                                   pk=self.teacher.pk))
        self.assertIn("frozen", body["message"])
        self.assertIn("ENGGU", body["message"])

    def test_an_unsuspended_teacher_is_still_the_institutes_to_manage(self):
        """The freeze is the exception, not a new default."""
        response = self.call(views.api_teacher_toggle, self.head,
                             pk=self.teacher.pk)
        self.assertTrue(self.body(response)["success"])
        self.teacher.refresh_from_db()
        self.assertFalse(self.teacher.is_active)

    def test_the_table_marks_the_row_frozen_for_the_institute(self):
        self.suspend()
        self.client.force_login(self.head)
        row = {r["email"]: r for r in self.client.get(
            reverse("academics:api_teachers"),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest").json()["data"]["rows"]
        }["teacher@acme.edu"]
        self.assertTrue(row["frozen"])
        self.assertFalse(row["can_edit"])

    def test_the_university_does_not_see_the_row_as_frozen(self):
        self.suspend()
        self.client.force_login(self.admin)
        row = {r["email"]: r for r in self.client.get(
            reverse("academics:api_teachers"),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest").json()["data"]["rows"]
        }["teacher@acme.edu"]
        self.assertFalse(row["frozen"])


class TablePayloadTests(SuspensionFixture):
    def rows(self, user):
        self.client.force_login(user)
        return {r["email"]: r for r in self.client.get(
            reverse("academics:api_teachers"),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest").json()["data"]["rows"]}

    def test_the_university_is_offered_the_button_only_where_it_may_act(self):
        rows = self.rows(self.admin)
        self.assertTrue(rows["teacher@acme.edu"]["can_suspend"])
        self.assertFalse(rows["arts@acme.edu"]["can_suspend"])

    def test_the_institute_head_is_never_offered_it(self):
        rows = self.rows(self.head)
        self.assertFalse(rows["teacher@acme.edu"]["can_suspend"])
        self.assertFalse(rows["teacher@acme.edu"]["can_lift"])

    def test_the_row_carries_the_reason_and_the_issuer(self):
        self.suspend()
        row = self.rows(self.admin)["teacher@acme.edu"]
        self.assertTrue(row["suspended"])
        self.assertEqual(row["suspension_reason"], REASON)
        self.assertEqual(row["suspended_by"], "ENGGU")
        self.assertEqual(row["status"], SUSPENDED_KEY)
        self.assertTrue(row["can_lift"])

    def test_the_institute_head_sees_the_suspension_but_cannot_lift_it(self):
        self.suspend()
        row = self.rows(self.head)["teacher@acme.edu"]
        self.assertTrue(row["suspended"])
        self.assertFalse(row["can_lift"])


class ReasonVisibilityTests(SuspensionFixture):
    """
    Who may read *why* somebody was suspended.

    The fact is not a secret — a teacher who cannot sign in is something the
    directory should not hide. The reason is: it is a staff matter between the
    university, the institute and the person, and it is written as prose that
    was never meant for a student to read.
    """

    def setUp(self):
        super().setUp()
        from academics.models import Batch, StudentProfile

        self.batch = Batch.objects.create(
            department=self.department, label="2022-26",
            start_year=2022, end_year=2026)
        # `face_enrolled` because a student without one is held at the capture
        # page by middleware and never reaches an API at all — which would make
        # this test pass for entirely the wrong reason.
        self.student = User.objects.create_user(
            email="student@acme.edu", password="Str0ngPass!23",
            role=User.Role.STUDENT, institute=self.institute,
            department=self.department, registration_completed=True,
            face_enrolled=True)
        StudentProfile.objects.create(
            user=self.student, department=self.department, batch=self.batch,
            class_roll="1")
        self.suspend()

    def row(self, user):
        self.client.force_login(user)
        rows = self.client.get(
            reverse("academics:api_teachers"),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest").json()["data"]["rows"]
        return {r["email"]: r for r in rows}["teacher@acme.edu"]

    def test_a_student_sees_that_they_are_suspended(self):
        self.assertTrue(self.row(self.student)["suspended"])

    def test_a_student_never_receives_the_reason(self):
        """
        Withheld from the payload, not hidden in the browser. Hiding it
        client-side would not be hiding it at all.
        """
        self.assertEqual(self.row(self.student)["suspension_reason"], "")

    def test_the_head_the_hod_and_the_university_all_see_it(self):
        for user in (self.head, self.hod, self.admin):
            self.assertEqual(self.row(user)["suspension_reason"], REASON,
                             user.role)

    def test_the_date_carries_a_time_as_well(self):
        """
        "Suspended on 13 Aug" is ambiguous on the day it happens, which is the
        day somebody is most likely to be asking about it.
        """
        import re

        self.assertRegex(self.row(self.admin)["suspended_at"],
                         r"^\d{2} \w{3} \d{4}, \d{2}:\d{2}$")

    def test_an_unsuspended_teacher_carries_no_date(self):
        from accounts import suspension as service

        self.teacher.refresh_from_db()
        service.lift(teacher=self.teacher, reason="", actor=self.admin)
        row = self.row(self.admin)
        self.assertFalse(row["suspended"])
        self.assertEqual(row["suspended_at"], "")
        self.assertEqual(row["suspension_reason"], "")
