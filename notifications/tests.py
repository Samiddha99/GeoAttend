import datetime as dt
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from academics.models import (
    Batch, Department, Enrollment, StudentProfile, Subject, TeacherAssignment,
)
from accounts.models import Institute, User
from attendance.models import AttendanceRecord, AttendanceSession
from dashboard.filters import ReportFilters
from notifications import message_templates as mt
from notifications import services as svc
from notifications.models import AlertCampaign, AlertDelivery, WhatsAppTemplate
from notifications import whatsapp as wa
from notifications.whatsapp import Result, normalise_msisdn, send_whatsapp

PW = "Str0ngPass!23"


# --------------------------------------------------------------------------- #
#  Placeholders
# --------------------------------------------------------------------------- #
class TemplateRenderTests(TestCase):
    def test_known_placeholders_are_replaced(self):
        out = mt.render("Hi {{student_name}}, you are at {{percentage}}%.",
                        {"student_name": "Ananya", "percentage": "62.5"})
        self.assertEqual(out, "Hi Ananya, you are at 62.5%.")

    def test_whitespace_inside_braces_is_tolerated(self):
        self.assertEqual(mt.render("{{ student_name }}", {"student_name": "A"}), "A")

    def test_unknown_placeholders_survive_verbatim(self):
        """A typo must stay visible so the sender catches it in the preview."""
        out = mt.render("Hi {{studnet_name}}", {"student_name": "A"})
        self.assertEqual(out, "Hi {{studnet_name}}")
        self.assertEqual(mt.unknown_placeholders("Hi {{studnet_name}}"), ["studnet_name"])

    def test_no_template_tags_are_evaluated(self):
        """Staff-typed text must never be executed as a Django template."""
        nasty = "{% if 1 %}boom{% endif %} {{ user.password }}"
        self.assertEqual(mt.render(nasty, {"student_name": "A"}), nasty)

    def test_none_renders_as_empty_string(self):
        self.assertEqual(mt.render("[{{roll_number}}]", {"roll_number": None}), "[]")

    def test_defaults_exist_for_both_scopes(self):
        for scope in ("OVERALL", "SUBJECT"):
            d = mt.defaults_for(scope)
            self.assertTrue(d["email_subject"] and d["email_body"] and d["whatsapp_body"])
            self.assertEqual(mt.unknown_placeholders(*d.values()), [])
        self.assertIn("{{subject_code}}", mt.defaults_for("SUBJECT")["email_subject"])


# --------------------------------------------------------------------------- #
#  Phone numbers & backends
# --------------------------------------------------------------------------- #
class TwilioSendTests(TestCase):
    """The Twilio transport, with the network mocked out."""

    def setUp(self):
        wa.reset_client()
        self.addCleanup(wa.reset_client)

    # ---- numbers -------------------------------------------------------- #
    def test_normalise_local_number(self):
        self.assertEqual(normalise_msisdn("98765 43210")[0], "+919876543210")
        self.assertEqual(normalise_msisdn("098765-43210")[0], "+919876543210")
        self.assertEqual(normalise_msisdn("+91 98765 43210")[0], "+919876543210")
        self.assertEqual(normalise_msisdn("0091 9876543210")[0], "+919876543210")

    def test_normalise_rejects_rubbish(self):
        self.assertTrue(normalise_msisdn("")[1])
        self.assertTrue(normalise_msisdn("not a number")[1])
        self.assertTrue(normalise_msisdn("12")[1])

    # ---- console mode --------------------------------------------------- #
    def test_no_credentials_means_console_mode(self):
        self.assertFalse(wa.is_configured())
        result = send_whatsapp("9876543210", "hello")
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "console")

    def test_empty_body_is_refused(self):
        self.assertFalse(send_whatsapp("9876543210", "   ").ok)

    def test_bad_number_never_reaches_the_provider(self):
        result = send_whatsapp("nope", "hello")
        self.assertFalse(result.ok)
        self.assertIn("not a phone number", result.error)

    def test_kill_switch(self):
        with override_settings(WHATSAPP={**settings.WHATSAPP, "ENABLED": False}):
            result = send_whatsapp("9876543210", "hi")
        self.assertFalse(result.ok)
        self.assertIn("disabled", result.error)

    # ---- live mode ------------------------------------------------------- #
    def _twilio(self, **extra):
        return override_settings(WHATSAPP={
            **settings.WHATSAPP, "ACCOUNT_SID": "ACtest", "AUTH_TOKEN": "tok",
            "FROM_NUMBER": "+14155238886", **extra})

    def test_free_form_message_payload(self):
        with self._twilio(), patch.object(wa, "get_client") as client:
            client.return_value.messages.create.return_value = SimpleNamespace(
                sid="SM123", status="queued")
            result = send_whatsapp("9876543210", "Your attendance is 61%.")

        self.assertTrue(result.ok)
        self.assertEqual(result.provider_id, "SM123")
        self.assertEqual(result.status, "queued")
        kwargs = client.return_value.messages.create.call_args.kwargs
        self.assertEqual(kwargs["from_"], "whatsapp:+14155238886")
        self.assertEqual(kwargs["to"], "whatsapp:+919876543210")
        self.assertEqual(kwargs["body"], "Your attendance is 61%.")
        self.assertNotIn("content_sid", kwargs)

    def test_content_template_payload(self):
        with self._twilio(), patch.object(wa, "get_client") as client:
            client.return_value.messages.create.return_value = SimpleNamespace(
                sid="SM9", status="accepted")
            send_whatsapp("9876543210", "ignored",
                          content_sid="HXabc", content_variables={"1": "Ana", "2": "61.3"})

        kwargs = client.return_value.messages.create.call_args.kwargs
        self.assertEqual(kwargs["content_sid"], "HXabc")
        self.assertEqual(json.loads(kwargs["content_variables"]), {"1": "Ana", "2": "61.3"})
        self.assertNotIn("body", kwargs)

    def test_the_configured_template_is_used_for_every_send(self):
        with self._twilio(CONTENT_SID="HXdefault"), patch.object(wa, "get_client") as client:
            client.return_value.messages.create.return_value = SimpleNamespace(
                sid="SM1", status="queued")
            send_whatsapp("9876543210", "body text")

        kwargs = client.return_value.messages.create.call_args.kwargs
        self.assertEqual(kwargs["content_sid"], "HXdefault")

    def test_a_from_number_already_prefixed_is_not_doubled(self):
        with self._twilio(FROM_NUMBER="whatsapp:+14155238886"), \
                patch.object(wa, "get_client") as client:
            client.return_value.messages.create.return_value = SimpleNamespace(
                sid="SM1", status="queued")
            send_whatsapp("9876543210", "hi")
        self.assertEqual(
            client.return_value.messages.create.call_args.kwargs["from_"],
            "whatsapp:+14155238886")

    def test_status_callback_is_passed_through(self):
        with self._twilio(STATUS_CALLBACK="https://x.test/hook"), \
                patch.object(wa, "get_client") as client:
            client.return_value.messages.create.return_value = SimpleNamespace(
                sid="SM1", status="queued")
            send_whatsapp("9876543210", "hi")
        self.assertEqual(
            client.return_value.messages.create.call_args.kwargs["status_callback"],
            "https://x.test/hook")

    # ---- failures -------------------------------------------------------- #
    def test_the_24_hour_window_error_is_explained(self):
        """63016 is the one every institute hits first — it must be actionable."""
        error = Exception("bad")
        error.code = 63016
        error.msg = "Failed to send freeform message"
        with self._twilio(), patch.object(wa, "get_client") as client:
            client.return_value.messages.create.side_effect = error
            result = send_whatsapp("9876543210", "hi")

        self.assertFalse(result.ok)
        self.assertIn("24-hour window", result.error)
        self.assertIn("TWILIO_CONTENT_SID", result.error)

    def test_auth_failure_is_explained(self):
        error = Exception("nope")
        error.code = 20003
        with self._twilio(), patch.object(wa, "get_client") as client:
            client.return_value.messages.create.side_effect = error
            result = send_whatsapp("9876543210", "hi")
        self.assertIn("authentication failed", result.error)

    def test_an_unknown_exception_becomes_a_result(self):
        with self._twilio(), patch.object(wa, "get_client") as client:
            client.return_value.messages.create.side_effect = RuntimeError("provider is down")
            result = send_whatsapp("9876543210", "hi")
        self.assertFalse(result.ok)
        self.assertIn("provider is down", result.error)

    def test_result_defaults(self):
        self.assertEqual(Result(False).provider_id, "")


# --------------------------------------------------------------------------- #
#  End-to-end alerting
# --------------------------------------------------------------------------- #
class AlertBase(TestCase):
    def setUp(self):
        self.institute = Institute.objects.create(name="Demo College", code="D", email="d@d.edu")
        self.dept = Department.objects.create(institute=self.institute, name="CSE", code="CSE")
        self.batch = Batch.objects.create(department=self.dept, label="2022-26",
                                          start_year=2022, end_year=2026)
        self.dsa = Subject.objects.create(department=self.dept, code="DSA", name="Data Structures")
        self.dbms = Subject.objects.create(department=self.dept, code="DBMS", name="Databases")

        self.head = self._user("head@d.edu", "HEAD")
        self.hod = self._user("hod@d.edu", "HOD")
        self.dept.hod = self.hod
        self.dept.save()
        self.teacher = self._user("t@d.edu", "TEACHER")
        for subject in (self.dsa, self.dbms):
            TeacherAssignment.objects.create(teacher=self.teacher, subject=subject, batch=self.batch)

        # good  → 100% ; poor → 25% ; nogsm → 25% but no guardian number
        self.good = self._student("good@d.edu", "Good Student", "R1", "+919812345670")
        self.poor = self._student("poor@d.edu", "Poor Student", "R2", "+919812345671")
        self.nogsm = self._student("nogsm@d.edu", "No Guardian", "R3", "")

        for i in range(4):
            session = self._session(self.dsa, i)
            self._mark(session, self.good)
            if i == 0:
                self._mark(session, self.poor)
                self._mark(session, self.nogsm)

    def _user(self, email, role, completed=True):
        return User.objects.create_user(
            email=email, password=PW, full_name=email.split("@")[0].title(), role=role,
            institute=self.institute, department=self.dept, registration_completed=completed,
        )

    def _student(self, email, name, roll, guardian, completed=True):
        user = User.objects.create_user(
            email=email, password=PW, full_name=name, role="STUDENT",
            institute=self.institute, department=self.dept, registration_completed=completed,
        )
        profile = StudentProfile.objects.create(
            user=user, department=self.dept, batch=self.batch, class_roll=roll,
            guardian_name="Guardian of " + name, guardian_mobile=guardian,
        )
        for subject in (self.dsa, self.dbms):
            Enrollment.objects.create(student=profile, subject=subject)
        return profile

    def _session(self, subject, offset):
        day = timezone.localdate() - dt.timedelta(days=offset)
        return AttendanceSession.objects.create(
            teacher=self.teacher, subject=subject, batch=self.batch,
            latitude=22.5726, longitude=88.3639, radius_m=50, session_date=day,
            expected_count=3, status=AttendanceSession.Status.CLOSED,
            expires_at=timezone.now(),
        )

    def _mark(self, session, student):
        AttendanceRecord.objects.create(session=session, student=student,
                                        status=AttendanceRecord.Status.PRESENT)

    def filters(self):
        class Req:
            GET = {}
        return ReportFilters.from_request(Req())

    def make_template(self, audience, body=None, status=None, name=None):
        """An approved WhatsApp template — what every send now requires."""
        from notifications import whatsapp as wa

        audience = getattr(WhatsAppTemplate.Audience, audience, audience)
        body = body or (mt.DEFAULT_STUDENT_WHATSAPP_OVERALL
                        if audience == WhatsAppTemplate.Audience.STUDENT
                        else mt.DEFAULT_WHATSAPP_OVERALL)
        return WhatsAppTemplate.objects.create(
            institute=self.institute, audience=audience,
            name=name or f"{audience} alert",
            twilio_name=f"{audience.lower()}_{WhatsAppTemplate.objects.count()}",
            body=body, variable_order=wa.to_numbered(body)[1],
            content_sid="HX" + "0" * 32,
            status=status or WhatsAppTemplate.Status.APPROVED,
            created_by=self.head,
        )


class RecipientSelectionTests(AlertBase):
    def test_only_students_below_threshold_are_picked(self):
        recipients = svc.build_recipients(self.head, self.filters(), 75)
        names = {r["name"] for r in recipients}
        self.assertEqual(names, {"Poor Student", "No Guardian"})

    def test_threshold_is_respected(self):
        self.assertEqual(len(svc.build_recipients(self.head, self.filters(), 10)), 0)
        self.assertEqual(len(svc.build_recipients(self.head, self.filters(), 101)), 3)

    def test_subject_scope_uses_that_subject_only(self):
        # Nobody attended DBMS at all, so everyone is at 0% there... but no DBMS
        # class was ever held, so nobody qualifies.
        recipients = svc.build_recipients(
            self.head, self.filters(), 75, svc.SUBJECT, self.dbms)
        self.assertEqual(recipients, [])

        recipients = svc.build_recipients(
            self.head, self.filters(), 75, svc.SUBJECT, self.dsa)
        self.assertEqual({r["name"] for r in recipients}, {"Poor Student", "No Guardian"})
        self.assertEqual(recipients[0]["context"]["subject_code"], "DSA")

    def test_students_with_no_classes_held_are_never_alerted(self):
        """Enrolled only in DBMS, where no class has been conducted → 0/0, not 0%."""
        loner = self._student("loner@d.edu", "Loner", "R9", "+919812345679")
        Enrollment.objects.filter(student=loner, subject=self.dsa).update(is_active=False)
        recipients = svc.build_recipients(self.head, self.filters(), 100)
        self.assertNotIn("Loner", {r["name"] for r in recipients})

    def test_guardian_number_is_normalised_and_problems_flagged(self):
        recipients = {r["name"]: r for r in svc.build_recipients(self.head, self.filters(), 75)}
        self.assertEqual(recipients["Poor Student"]["guardian_number"], "+919812345671")
        self.assertEqual(recipients["No Guardian"]["guardian_number"], "")
        self.assertTrue(recipients["No Guardian"]["guardian_error"])

    def test_inactive_students_are_excluded(self):
        self.poor.user.is_active = False
        self.poor.user.save()
        names = {r["name"] for r in svc.build_recipients(self.head, self.filters(), 75)}
        self.assertNotIn("Poor Student", names)

    def test_context_numbers_match_the_report(self):
        recipient = next(r for r in svc.build_recipients(self.head, self.filters(), 75)
                         if r["name"] == "Poor Student")
        self.assertEqual(recipient["held"], 4)
        self.assertEqual(recipient["attended"], 1)
        self.assertEqual(recipient["missed"], 3)
        self.assertEqual(recipient["context"]["percentage"], "25.0")
        self.assertEqual(recipient["context"]["shortfall"], "50.0")
        self.assertEqual(recipient["context"]["institute"], "Demo College")


class SendCampaignTests(AlertBase):
    def _send(self, user=None, **kwargs):
        options = dict(
            user=user or self.head, filters=self.filters(), threshold=75,
            scope=svc.OVERALL, subject=None, drafts=mt.defaults_for("OVERALL"),
            channels={"email": True, "whatsapp": True},
            guardian_template=self.make_template("GUARDIAN"),
        )
        options.update(kwargs)
        return svc.send_campaign(**options)

    def test_both_channels_are_delivered_and_recorded(self):
        campaign = self._send()
        self.assertEqual(campaign.total_recipients, 2)
        self.assertEqual(campaign.email_sent, 2)
        self.assertEqual(campaign.whatsapp_sent, 1)      # one guardian has no number
        self.assertEqual(campaign.skipped, 1)
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(AlertDelivery.objects.count(), 4)

    def test_placeholders_are_filled_in_the_delivered_body(self):
        self._send()
        body = AlertDelivery.objects.filter(
            channel=AlertDelivery.Channel.WHATSAPP,
            status=AlertDelivery.Status.SENT).first().body
        self.assertIn("Poor Student", body)
        self.assertIn("25.0%", body)
        self.assertNotIn("{{", body)

    def test_email_subject_is_rendered(self):
        self._send(channels={"email": True, "whatsapp": False})
        self.assertIn("25.0%", mail.outbox[0].subject)
        self.assertNotIn("{{", mail.outbox[0].subject)

    def test_single_channel(self):
        campaign = self._send(channels={"email": False, "whatsapp": True})
        self.assertEqual(campaign.email_sent, 0)
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(campaign.whatsapp_sent, 1)

    def test_selection_can_narrow_but_never_widen(self):
        campaign = self._send(student_ids=[self.poor.id])
        self.assertEqual(campaign.total_recipients, 1)

        # `good` is above the threshold — asking for them must change nothing
        campaign = self._send(student_ids=[self.poor.id, self.good.id])
        self.assertEqual(campaign.total_recipients, 1)
        self.assertEqual(
            AlertDelivery.objects.filter(campaign=campaign, student=self.good).count(), 0)

    def test_unactivated_students_are_skipped_for_email(self):
        pending = self._student("new@d.edu", "New Student", "R4", "+919812345675",
                                completed=False)
        Enrollment.objects.filter(student=pending).delete()
        Enrollment.objects.create(student=pending, subject=self.dsa)
        campaign = self._send()
        skipped = AlertDelivery.objects.get(
            campaign=campaign, student=pending, channel=AlertDelivery.Channel.EMAIL)
        self.assertEqual(skipped.status, AlertDelivery.Status.SKIPPED)
        self.assertIn("not activated", skipped.error)

    def test_missing_guardian_is_skipped_with_a_reason(self):
        campaign = self._send()
        record = AlertDelivery.objects.get(
            campaign=campaign, student=self.nogsm, channel=AlertDelivery.Channel.WHATSAPP)
        self.assertEqual(record.status, AlertDelivery.Status.SKIPPED)
        self.assertIn("no number on record", record.error)

    def test_one_failure_does_not_abort_the_campaign(self):
        with override_settings(WHATSAPP={**settings.WHATSAPP, "ENABLED": False}):
            campaign = self._send()
        self.assertEqual(campaign.whatsapp_sent, 0)
        self.assertEqual(campaign.whatsapp_failed, 1)
        self.assertEqual(campaign.email_sent, 2)         # email still went out

    def test_teacher_scope_is_narrower_than_head(self):
        other_teacher = self._user("t2@d.edu", "TEACHER")
        campaign = self._send(user=other_teacher)
        self.assertEqual(campaign.total_recipients, 0)


class AlertApiTests(AlertBase):
    def test_recipients_endpoint(self):
        self.client.force_login(self.hod)
        res = self.client.get(reverse("notifications:api_recipients"),
                              {"threshold": 75, "scope": "OVERALL"})
        data = res.json()["data"]
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["missing_guardians"], 1)
        self.assertEqual(data["reachable_guardians"], 1)

    def test_threshold_is_validated(self):
        self.client.force_login(self.hod)
        for bad in ("0", "150", "abc"):
            res = self.client.get(reverse("notifications:api_recipients"), {"threshold": bad})
            self.assertEqual(res.status_code, 400, bad)

    def test_subject_scope_requires_a_subject_in_scope(self):
        outside = Department.objects.create(institute=self.institute, name="ECE", code="ECE")
        alien = Subject.objects.create(department=outside, code="SS", name="Signals")
        self.client.force_login(self.hod)
        res = self.client.get(reverse("notifications:api_recipients"),
                              {"scope": "SUBJECT", "subject": alien.id, "threshold": 75})
        self.assertEqual(res.status_code, 403)

    def test_send_requires_a_channel(self):
        self.client.force_login(self.hod)
        res = self.client.post(reverse("notifications:api_send"), {
            "threshold": 75, "email_students": "0", "whatsapp_guardians": "0",
        })
        self.assertEqual(res.status_code, 400)
        self.assertIn("at least one channel", res.json()["message"])

    def test_send_rejects_empty_bodies(self):
        self.client.force_login(self.hod)
        res = self.client.post(reverse("notifications:api_send"), {
            "threshold": 75, "email_students": "1", "whatsapp_guardians": "0",
            "email_subject": "", "email_body": "",
        })
        self.assertEqual(res.status_code, 400)

    def test_send_end_to_end(self):
        self.client.force_login(self.hod)
        guardian = self.make_template("GUARDIAN")
        res = self.client.post(reverse("notifications:api_send"), {
            "threshold": 75, "email_students": "1", "whatsapp_guardians": "1",
            "guardian_template": guardian.id,
            **mt.defaults_for("OVERALL"),
        })
        self.assertTrue(res.json()["success"])
        campaign = AlertCampaign.objects.get()
        self.assertEqual(campaign.created_by, self.hod)
        self.assertEqual(campaign.total_recipients, 2)

    def test_preview_renders_a_real_recipient(self):
        self.client.force_login(self.teacher)
        res = self.client.post(reverse("notifications:api_preview"), {
            "threshold": 75, "email_subject": "Hi {{student_name}}",
            "email_body": "You are at {{percentage}}%", "whatsapp_body": "{{guardian_name}}",
        })
        data = res.json()["data"]
        self.assertIn("Poor Student", data["email_subject"])
        self.assertIn("25.0%", data["email_body"])
        self.assertEqual(data["unknown"], [])

    def test_preview_reports_typos(self):
        self.client.force_login(self.teacher)
        res = self.client.post(reverse("notifications:api_preview"), {
            "threshold": 75, "email_subject": "Hi {{studnet_name}}",
            "email_body": "x", "whatsapp_body": "y",
        })
        self.assertEqual(res.json()["data"]["unknown"], ["studnet_name"])

    def test_students_cannot_reach_the_alert_screens(self):
        self.client.force_login(self.good.user)
        for name in ("notifications:alerts", "notifications:api_recipients",
                     "notifications:api_campaigns"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 403, name)

    def test_teacher_cannot_send_a_whatsapp_test(self):
        self.client.force_login(self.teacher)
        res = self.client.post(reverse("notifications:api_whatsapp_test"), {"to": "9876543210"})
        self.assertEqual(res.status_code, 403)

    def test_history_is_scoped_to_the_sender_for_teachers(self):
        svc.send_campaign(
            user=self.hod, filters=self.filters(), threshold=75, scope=svc.OVERALL,
            subject=None, drafts=mt.defaults_for("OVERALL"),
            channels={"email": True, "whatsapp": False},
        )
        self.client.force_login(self.teacher)
        res = self.client.get(reverse("notifications:api_campaigns"))
        self.assertEqual(res.json()["data"]["rows"], [])

        self.client.force_login(self.head)
        res = self.client.get(reverse("notifications:api_campaigns"))
        self.assertEqual(len(res.json()["data"]["rows"]), 1)

    def test_campaign_detail_is_not_readable_across_institutes(self):
        campaign = svc.send_campaign(
            user=self.head, filters=self.filters(), threshold=75, scope=svc.OVERALL,
            subject=None, drafts=mt.defaults_for("OVERALL"),
            channels={"email": True, "whatsapp": False},
        )
        other = Institute.objects.create(name="Other", code="O", email="o@o.edu")
        intruder = User.objects.create_user(
            email="spy@o.edu", password=PW, role="HEAD", institute=other,
            registration_completed=True)
        self.client.force_login(intruder)
        res = self.client.get(
            reverse("notifications:api_campaign_detail", args=[campaign.id]))
        self.assertEqual(res.status_code, 404)


class StudentWhatsAppTests(AlertBase):
    """The third channel: WhatsApp straight to the student's own number."""

    def setUp(self):
        super().setUp()
        # `poor` keeps a roster mobile; give `nogsm` none at all so the
        # missing-number path is exercised on the student channel too.
        self.poor.mobile = "9876500011"
        self.poor.save()
        self.nogsm.mobile = ""
        self.nogsm.save()
        self.nogsm.user.phone = ""
        self.nogsm.user.save()

    def _send(self, **kwargs):
        options = dict(
            user=self.head, filters=self.filters(), threshold=75,
            scope=svc.OVERALL, subject=None, drafts=mt.defaults_for("OVERALL"),
            channels={"email": False, "student_whatsapp": True, "whatsapp": False},
            student_template=self.make_template("STUDENT"),
            guardian_template=self.make_template("GUARDIAN"),
        )
        options.update(kwargs)
        return svc.send_campaign(**options)

    # ---- number resolution -------------------------------------------- #
    def test_student_number_comes_from_the_roster(self):
        recipient = next(r for r in svc.build_recipients(self.head, self.filters(), 75)
                         if r["name"] == "Poor Student")
        self.assertEqual(recipient["student_number"], "+919876500011")
        self.assertEqual(recipient["context"]["student_mobile"], "+919876500011")

    def test_falls_back_to_the_users_own_phone(self):
        self.poor.mobile = ""
        self.poor.save()
        self.poor.user.phone = "+919812340000"
        self.poor.user.save()
        recipient = next(r for r in svc.build_recipients(self.head, self.filters(), 75)
                         if r["name"] == "Poor Student")
        self.assertEqual(recipient["student_number"], "+919812340000")

    def test_missing_student_number_is_flagged(self):
        recipient = next(r for r in svc.build_recipients(self.head, self.filters(), 75)
                         if r["name"] == "No Guardian")
        self.assertEqual(recipient["student_number"], "")
        self.assertTrue(recipient["student_error"])

    # ---- delivery ------------------------------------------------------ #
    def test_sends_to_the_student_and_records_the_channel(self):
        campaign = self._send()
        self.assertTrue(campaign.whatsapp_students)
        self.assertFalse(campaign.whatsapp_guardians)
        self.assertEqual(campaign.student_whatsapp_sent, 1)   # only `poor` has a number
        self.assertEqual(campaign.skipped, 1)
        self.assertEqual(campaign.whatsapp_sent, 0)
        delivery = AlertDelivery.objects.get(
            campaign=campaign, student=self.poor,
            channel=AlertDelivery.Channel.WHATSAPP_STUDENT)
        self.assertEqual(delivery.status, AlertDelivery.Status.SENT)
        self.assertEqual(delivery.target, "+919876500011")

    def test_student_body_addresses_the_student_not_the_guardian(self):
        campaign = self._send()
        body = AlertDelivery.objects.get(
            campaign=campaign, student=self.poor,
            channel=AlertDelivery.Channel.WHATSAPP_STUDENT).body
        self.assertIn("Hi Poor,", body)             # {{first_name}}
        self.assertNotIn("Your ward", body)         # that is the guardian wording
        self.assertNotIn("{{", body)

    def test_missing_number_is_skipped_with_a_reason(self):
        campaign = self._send()
        delivery = AlertDelivery.objects.get(
            campaign=campaign, student=self.nogsm,
            channel=AlertDelivery.Channel.WHATSAPP_STUDENT)
        self.assertEqual(delivery.status, AlertDelivery.Status.SKIPPED)
        self.assertIn("no number on record", delivery.error)

    def test_all_three_channels_at_once(self):
        campaign = self._send(channels={"email": True, "student_whatsapp": True,
                                        "whatsapp": True})
        self.assertEqual(campaign.email_sent, 2)
        self.assertEqual(campaign.student_whatsapp_sent, 1)
        self.assertEqual(campaign.whatsapp_sent, 1)
        self.assertEqual(campaign.sent_total, 4)
        # two students × three channels
        self.assertEqual(AlertDelivery.objects.filter(campaign=campaign).count(), 6)
        self.assertEqual(campaign.channel_label, "Email + WA→student + WA→guardian")

    def test_student_and_guardian_bodies_are_independent(self):
        campaign = self._send(channels={"email": False, "student_whatsapp": True,
                                        "whatsapp": True})
        student_body = AlertDelivery.objects.get(
            campaign=campaign, student=self.poor,
            channel=AlertDelivery.Channel.WHATSAPP_STUDENT).body
        guardian_body = AlertDelivery.objects.get(
            campaign=campaign, student=self.poor,
            channel=AlertDelivery.Channel.WHATSAPP).body
        self.assertNotEqual(student_body, guardian_body)
        self.assertIn("Your ward", guardian_body)

    def test_subject_scope_uses_the_subject_student_template(self):
        campaign = svc.send_campaign(
            user=self.head, filters=self.filters(), threshold=75,
            scope=svc.SUBJECT, subject=self.dsa, drafts=mt.defaults_for("SUBJECT"),
            channels={"email": False, "student_whatsapp": True, "whatsapp": False},
            student_template=self.make_template(
                "STUDENT", body=mt.DEFAULT_STUDENT_WHATSAPP_SUBJECT),
        )
        body = AlertDelivery.objects.filter(
            campaign=campaign, status=AlertDelivery.Status.SENT).first().body
        self.assertIn("DSA — Data Structures", body)
        self.assertIn("Hi Poor,", body)

    def test_failures_are_counted_separately_from_guardian_failures(self):
        with override_settings(WHATSAPP={**settings.WHATSAPP, "ENABLED": False}):
            campaign = self._send(channels={"email": False, "student_whatsapp": True,
                                            "whatsapp": True})
        self.assertEqual(campaign.student_whatsapp_failed, 1)
        self.assertEqual(campaign.whatsapp_failed, 1)
        self.assertEqual(campaign.failed_total, 2)

    # ---- API ------------------------------------------------------------ #
    def test_recipients_endpoint_counts_student_numbers(self):
        self.client.force_login(self.hod)
        data = self.client.get(reverse("notifications:api_recipients"),
                               {"threshold": 75}).json()["data"]
        self.assertEqual(data["reachable_students"], 1)
        self.assertEqual(data["missing_student_numbers"], 1)

    def test_send_requires_an_approved_student_template(self):
        self.client.force_login(self.hod)
        res = self.client.post(reverse("notifications:api_send"), {
            "threshold": 75, "email_students": "0", "whatsapp_students": "1",
            "whatsapp_guardians": "0",
        })
        self.assertEqual(res.status_code, 400)
        self.assertIn("approved student WhatsApp template", res.json()["message"])

    def test_send_via_api_with_only_the_student_channel(self):
        self.client.force_login(self.hod)
        student = self.make_template("STUDENT")
        res = self.client.post(reverse("notifications:api_send"), dict({
            "threshold": 75, "email_students": "0", "whatsapp_students": "1",
            "whatsapp_guardians": "0", "student_template": student.id,
        }, **mt.defaults_for("OVERALL")))
        self.assertTrue(res.json()["success"])
        campaign = AlertCampaign.objects.get()
        self.assertTrue(campaign.whatsapp_students)
        self.assertEqual(campaign.student_whatsapp_sent, 1)
        self.assertIn("WhatsApp to students", res.json()["message"])

    def test_preview_renders_all_three_messages(self):
        self.client.force_login(self.hod)
        data = self.client.post(reverse("notifications:api_preview"), dict({
            "threshold": 75,
            "student_template": self.make_template("STUDENT").id,
            "guardian_template": self.make_template("GUARDIAN").id,
        }, **mt.defaults_for("OVERALL"))).json()["data"]
        self.assertIn("Hi Poor,", data["student_whatsapp_body"])
        self.assertIn("Your ward", data["whatsapp_body"])
        self.assertTrue(data["email_body"])
        self.assertEqual(data["student_whatsapp_to"], "+919876500011")
        self.assertEqual(data["unknown"], [])

    def test_defaults_include_the_student_template_for_both_scopes(self):
        for scope in ("OVERALL", "SUBJECT"):
            drafts = mt.defaults_for(scope)
            self.assertTrue(drafts["student_whatsapp_body"])
            self.assertNotEqual(drafts["student_whatsapp_body"], drafts["whatsapp_body"])
            self.assertEqual(mt.unknown_placeholders(*drafts.values()), [])
