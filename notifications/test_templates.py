"""
WhatsApp template registration, approval tracking, and the rule that only
approved wording may be sent.
"""
import datetime as dt
from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from notifications import message_templates as mt
from notifications import template_service as ts
from notifications import whatsapp as wa
from notifications.models import WhatsAppTemplate
from notifications.tests import AlertBase


def twilio_live(**extra):
    return override_settings(WHATSAPP={
        **settings.WHATSAPP, "ACCOUNT_SID": "ACtest", "AUTH_TOKEN": "tok",
        "FROM_NUMBER": "+14155238886", **extra})


# --------------------------------------------------------------------------- #
#  Placeholder → numbered slot conversion
# --------------------------------------------------------------------------- #
class NumberingTests(TestCase):
    def test_placeholders_become_numbered_slots(self):
        body, order = wa.to_numbered("Hi {{first_name}}, you are at {{percentage}}%")
        self.assertEqual(body, "Hi {{1}}, you are at {{2}}%")
        self.assertEqual(order, ["first_name", "percentage"])

    def test_a_repeated_placeholder_reuses_its_slot(self):
        """WhatsApp expects one slot per distinct variable, not per occurrence."""
        body, order = wa.to_numbered("{{first_name}}, hello {{first_name}} from {{institute}}")
        self.assertEqual(body, "{{1}}, hello {{1}} from {{2}}")
        self.assertEqual(order, ["first_name", "institute"])

    def test_whitespace_inside_braces_is_tolerated(self):
        self.assertEqual(wa.to_numbered("{{ first_name }}")[0], "{{1}}")

    def test_every_shipped_default_converts_cleanly(self):
        for scope in ("OVERALL", "SUBJECT"):
            for key in ("student_whatsapp_body", "whatsapp_body"):
                body = mt.defaults_for(scope)[key]
                numbered, order = wa.to_numbered(body)
                self.assertNotIn("{{student", numbered, f"{scope}/{key}")
                self.assertTrue(order)
                self.assertEqual(mt.unknown_placeholders(body), [])

    def test_sample_variables_line_up_with_the_slots(self):
        _, order = wa.to_numbered("{{student_name}} at {{percentage}}%")
        samples = wa.sample_variables(order)
        self.assertEqual(list(samples), ["1", "2"])
        self.assertEqual(samples["2"], "61.3")


# --------------------------------------------------------------------------- #
#  Validation before anything reaches Twilio
# --------------------------------------------------------------------------- #
class ValidationTests(TestCase):
    def test_empty_body_refused(self):
        self.assertIn("empty", ts.validate_body("   "))

    def test_unknown_placeholder_refused(self):
        error = ts.validate_body("Hi {{studnet_name}}, attend class.")
        self.assertIn("studnet_name", error)

    def test_placeholders_only_refused(self):
        """WhatsApp rejects a template with no fixed wording of its own."""
        self.assertIn("only placeholders", ts.validate_body("{{student_name}} {{percentage}}"))

    def test_over_length_refused(self):
        self.assertIn("1024", ts.validate_body("x" * 1100))

    def test_a_shipped_default_passes(self):
        self.assertIsNone(ts.validate_body(mt.DEFAULT_WHATSAPP_OVERALL))


# --------------------------------------------------------------------------- #
#  Submission and approval tracking
# --------------------------------------------------------------------------- #
class SubmissionTests(AlertBase):
    def setUp(self):
        super().setUp()
        wa.reset_client()
        self.addCleanup(wa.reset_client)

    def create(self, **kwargs):
        options = dict(
            institute=self.institute, user=self.head,
            audience=WhatsAppTemplate.Audience.GUARDIAN,
            name="Low attendance", body=mt.DEFAULT_WHATSAPP_OVERALL)
        options.update(kwargs)
        return ts.create_template(**options)

    def test_console_mode_simulates_the_whole_round_trip(self):
        template = self.create()
        self.assertTrue(template.content_sid.startswith("HX"))
        self.assertEqual(template.status, WhatsAppTemplate.Status.RECEIVED)
        self.assertTrue(template.variable_order)

    def test_twilio_name_is_whatsapp_safe_and_unique(self):
        a = self.create(name="Low Attendance — Overall!")
        b = self.create(name="Low Attendance — Overall!")
        for name in (a.twilio_name, b.twilio_name):
            self.assertRegex(name, r"^[a-z0-9_]+$")
        self.assertNotEqual(a.twilio_name, b.twilio_name)

    def test_the_live_payload_matches_twilios_content_api(self):
        """
        Registration posts to the Content REST endpoint directly. The SDK's
        contents.create() calls .to_dict() on its argument, so a plain dict
        raised "'dict' object has no attribute 'to_dict'" and nothing was sent.
        """
        posts = []

        def fake_post(url, **kwargs):
            posts.append((url, kwargs["json"]))
            payload = ({"sid": "HXlive"} if url.endswith("/Content")
                       else {"status": "received", "rejection_reason": ""})
            return SimpleNamespace(status_code=201, text="{}",
                                   json=lambda: payload)

        with twilio_live(), patch("requests.post", side_effect=fake_post):
            template = self.create()

        (create_url, request), (approve_url, approval) = posts
        self.assertEqual(create_url, "https://content.twilio.com/v1/Content")
        self.assertEqual(request["friendly_name"], template.twilio_name)
        self.assertEqual(request["language"], "en")
        body = request["types"]["twilio/text"]["body"]
        self.assertIn("{{1}}", body)
        self.assertNotIn("{{guardian_name}}", body)
        self.assertEqual(list(request["variables"])[0], "1")
        # Only the message type in use — no null entries for the other thirteen.
        self.assertEqual(list(request["types"]), ["twilio/text"])

        self.assertEqual(
            approve_url,
            "https://content.twilio.com/v1/Content/HXlive/ApprovalRequests/whatsapp")
        self.assertEqual(approval["category"], "UTILITY")
        self.assertEqual(approval["name"], template.twilio_name)
        self.assertEqual(template.content_sid, "HXlive")
        self.assertEqual(template.status, WhatsAppTemplate.Status.RECEIVED)

    def test_a_twilio_error_leaves_a_retryable_failure(self):
        reply = SimpleNamespace(
            status_code=401, text='{"code":20003}',
            json=lambda: {"code": 20003, "message": "Authenticate"})
        with twilio_live(), patch("requests.post", return_value=reply):
            template = self.create()

        self.assertEqual(template.status, WhatsAppTemplate.Status.FAILED)
        self.assertIn("authentication failed", template.last_error)
        self.assertTrue(template.is_editable)

    def test_status_sync_reads_the_whatsapp_verdict(self):
        template = self.create()
        reply = SimpleNamespace(
            status_code=200, text="{}",
            json=lambda: {"whatsapp": {"status": "rejected",
                                      "rejection_reason": "Promotional content"}})
        with twilio_live(), patch("requests.get", return_value=reply):
            ts.sync_template(template)

        self.assertEqual(template.status, WhatsAppTemplate.Status.REJECTED)
        self.assertEqual(template.rejection_reason, "Promotional content")
        self.assertFalse(template.is_sendable)

    def test_approval_makes_it_sendable(self):
        template = self.create()
        reply = SimpleNamespace(
            status_code=200, text="{}",
            json=lambda: {"whatsapp": {"status": "approved",
                                      "rejection_reason": ""}})
        with twilio_live(), patch("requests.get", return_value=reply):
            ts.sync_template(template)
        self.assertEqual(template.status, WhatsAppTemplate.Status.APPROVED)
        self.assertTrue(template.is_sendable)

    def test_an_unknown_status_is_kept_rather_than_dropped(self):
        template = self.create()
        reply = SimpleNamespace(
            status_code=200, text="{}",
            json=lambda: {"whatsapp": {"status": "in_appeal",
                                      "rejection_reason": ""}})
        with twilio_live(), patch("requests.get", return_value=reply):
            ts.sync_template(template)
        self.assertEqual(template.status, "IN_APPEAL")

    def test_content_variables_follow_the_approved_order(self):
        template = self.create(body="{{first_name}} is at {{percentage}}% of {{threshold}}%")
        variables = template.content_variables(
            {"first_name": "Ana", "percentage": "61.3", "threshold": "75"})
        self.assertEqual(variables, {"1": "Ana", "2": "61.3", "3": "75"})


# --------------------------------------------------------------------------- #
#  Who may manage them
# --------------------------------------------------------------------------- #
class PermissionTests(AlertBase):
    URLS = ("notifications:templates", "notifications:api_templates")

    def test_only_the_head_may_manage_templates(self):
        for user in (self.hod, self.teacher, self.good.user):
            self.client.force_login(user)
            for name in self.URLS:
                self.assertEqual(self.client.get(reverse(name)).status_code, 403,
                                 f"{user.role} → {name}")

    def test_the_head_may(self):
        self.client.force_login(self.head)
        for name in self.URLS:
            self.assertEqual(self.client.get(reverse(name)).status_code, 200)

    def test_creating_via_the_api(self):
        self.client.force_login(self.head)
        res = self.client.post(reverse("notifications:api_template_create"), {
            "audience": "STUDENT", "name": "Student overall",
            "body": mt.DEFAULT_STUDENT_WHATSAPP_OVERALL, "category": "UTILITY",
        })
        self.assertTrue(res.json()["success"])
        self.assertEqual(WhatsAppTemplate.objects.count(), 1)

    def test_a_bad_body_is_refused_by_the_api(self):
        self.client.force_login(self.head)
        res = self.client.post(reverse("notifications:api_template_create"), {
            "audience": "STUDENT", "name": "Bad", "body": "{{student_name}}",
        })
        self.assertEqual(res.status_code, 400)
        self.assertEqual(WhatsAppTemplate.objects.count(), 0)

    def test_templates_are_scoped_to_the_institute(self):
        from accounts.models import Institute, User

        other = Institute.objects.create(name="Other", code="O", email="o@o.edu")
        intruder = User.objects.create_user(
            email="spy@o.edu", password="Str0ngPass!23", role="HEAD",
            institute=other, registration_completed=True)
        mine = self.make_template("GUARDIAN")

        self.client.force_login(intruder)
        rows = self.client.get(reverse("notifications:api_templates")).json()["data"]["rows"]
        self.assertEqual(rows, [])
        self.assertEqual(
            self.client.post(
                reverse("notifications:api_template_sync", args=[mine.id])).status_code, 404)


# --------------------------------------------------------------------------- #
#  Only approved wording may be sent
# --------------------------------------------------------------------------- #
class ApprovedOnlyTests(AlertBase):
    def send(self, **extra):
        self.client.force_login(self.hod)
        payload = {"threshold": 75, "email_students": "0",
                   "whatsapp_students": "0", "whatsapp_guardians": "1"}
        payload.update(extra)
        return self.client.post(reverse("notifications:api_send"), payload)

    def test_a_pending_template_cannot_be_used(self):
        pending = self.make_template("GUARDIAN", status=WhatsAppTemplate.Status.PENDING)
        res = self.send(guardian_template=pending.id)
        self.assertEqual(res.status_code, 403)
        self.assertIn("not approved", res.json()["message"])

    def test_a_rejected_template_cannot_be_used(self):
        rejected = self.make_template("GUARDIAN", status=WhatsAppTemplate.Status.REJECTED)
        self.assertEqual(self.send(guardian_template=rejected.id).status_code, 403)

    def test_the_wrong_audience_cannot_be_used(self):
        """A student template must not be sent to a guardian."""
        student = self.make_template("STUDENT")
        self.assertEqual(self.send(guardian_template=student.id).status_code, 403)

    def test_another_institutes_template_cannot_be_used(self):
        from accounts.models import Institute

        other = Institute.objects.create(name="Other", code="O", email="o@o.edu")
        alien = WhatsAppTemplate.objects.create(
            institute=other, audience=WhatsAppTemplate.Audience.GUARDIAN,
            name="Alien", twilio_name="alien", body="Hello {{student_name}}",
            variable_order=["student_name"], content_sid="HXalien",
            status=WhatsAppTemplate.Status.APPROVED)
        self.assertEqual(self.send(guardian_template=alien.id).status_code, 403)

    def test_a_deactivated_template_cannot_be_used(self):
        template = self.make_template("GUARDIAN")
        template.is_active = False
        template.save()
        self.assertEqual(self.send(guardian_template=template.id).status_code, 403)

    def test_an_approved_template_works(self):
        approved = self.make_template("GUARDIAN")
        res = self.send(guardian_template=approved.id)
        self.assertTrue(res.json()["success"])

    def test_the_campaign_records_which_template_went_out(self):
        from notifications.models import AlertCampaign

        approved = self.make_template("GUARDIAN")
        self.send(guardian_template=approved.id)
        campaign = AlertCampaign.objects.get()
        self.assertEqual(campaign.guardian_template, approved)
        # the wording is copied so the audit survives the template changing later
        self.assertEqual(campaign.whatsapp_body, approved.body)

    def test_the_send_uses_content_sid_not_free_form(self):
        approved = self.make_template("GUARDIAN")
        with patch("notifications.services.send_whatsapp") as sender:
            sender.return_value = wa.Result(True, provider_id="SM1", status="queued")
            self.send(guardian_template=approved.id)

        kwargs = sender.call_args.kwargs
        self.assertEqual(kwargs["content_sid"], approved.content_sid)
        self.assertIn("1", kwargs["content_variables"])
        self.assertNotIn("{{", str(kwargs["content_variables"]))

    def test_the_alerts_page_only_offers_approved_templates(self):
        self.make_template("GUARDIAN", name="Good")
        self.make_template("GUARDIAN", name="Waiting",
                           status=WhatsAppTemplate.Status.PENDING)
        self.client.force_login(self.hod)
        body = self.client.get(reverse("notifications:alerts")).content.decode()
        self.assertIn("Good", body)
        self.assertNotIn("Waiting", body)


class AutoSyncTests(AlertBase):
    """
    Opening the templates or alerts screen refreshes undecided templates.

    The guards matter more than the feature: this runs inside a request the
    user is waiting on, so it must cost nothing once everything is decided.
    """

    def setUp(self):
        super().setUp()
        self.addCleanup(wa.reset_client)

    _seq = 0

    def _template(self, status=WhatsAppTemplate.Status.RECEIVED, **kwargs):
        # twilio_name is unique per institute, so each row needs its own.
        type(self)._seq += 1
        return WhatsAppTemplate.objects.create(
            institute=self.institute, created_by=self.head,
            audience=WhatsAppTemplate.Audience.GUARDIAN,
            name=f"Low attendance {self._seq}",
            twilio_name=f"low_attendance_{self._seq}",
            body="Hi {{student_name}}", variable_order=["student_name"],
            content_sid=f"HX{self._seq}", status=status, **kwargs)

    def _reply(self, status="approved", reason=""):
        return SimpleNamespace(
            status_code=200, text="{}",
            json=lambda: {"whatsapp": {"status": status, "rejection_reason": reason}})

    # ------------------------------------------------------------- the guards
    def test_nothing_is_polled_when_every_template_is_decided(self):
        """The steady state: no pending templates, so no network at all."""
        self._template(status=WhatsAppTemplate.Status.APPROVED)
        self._template(status=WhatsAppTemplate.Status.REJECTED)
        with twilio_live(), patch("requests.get") as get:
            ts.autosync(self.institute)
        get.assert_not_called()

    def test_a_recently_synced_template_is_skipped(self):
        template = self._template(last_synced_at=timezone.now())
        with twilio_live(), patch("requests.get") as get:
            ts.autosync(self.institute)
        get.assert_not_called()
        self.assertEqual(template.status, WhatsAppTemplate.Status.RECEIVED)

    def test_a_stale_template_is_refreshed(self):
        template = self._template(
            last_synced_at=timezone.now() - dt.timedelta(hours=1))
        with twilio_live(), patch("requests.get", return_value=self._reply()) as get:
            ts.autosync(self.institute)
        get.assert_called_once()
        template.refresh_from_db()
        self.assertEqual(template.status, WhatsAppTemplate.Status.APPROVED)

    def test_a_never_synced_template_is_refreshed(self):
        template = self._template(last_synced_at=None)
        with twilio_live(), patch("requests.get", return_value=self._reply("rejected", "Promo")):
            ts.autosync(self.institute)
        template.refresh_from_db()
        self.assertEqual(template.status, WhatsAppTemplate.Status.REJECTED)
        self.assertEqual(template.rejection_reason, "Promo")

    def test_the_short_timeout_is_used(self):
        """A slow Twilio must not hold the page for the full default."""
        self._template(last_synced_at=None)
        with twilio_live(), patch("requests.get", return_value=self._reply()) as get:
            ts.autosync(self.institute)
        self.assertEqual(get.call_args.kwargs["timeout"],
                         settings.WHATSAPP["AUTOSYNC_TIMEOUT"])
        self.assertLess(get.call_args.kwargs["timeout"], settings.WHATSAPP["TIMEOUT"])

    def test_it_can_be_turned_off(self):
        self._template(last_synced_at=None)
        conf = {**settings.WHATSAPP, "ACCOUNT_SID": "AC", "AUTH_TOKEN": "t",
                "FROM_NUMBER": "+1", "AUTOSYNC": False}
        with override_settings(WHATSAPP=conf), patch("requests.get") as get:
            ts.autosync(self.institute)
        get.assert_not_called()

    def test_console_mode_makes_no_calls(self):
        self._template(last_synced_at=None)
        with patch("requests.get") as get:
            ts.autosync(self.institute)      # no credentials configured
        get.assert_not_called()

    def test_a_twilio_outage_does_not_break_the_page(self):
        """The page must render even when Twilio is unreachable."""
        template = self._template(last_synced_at=None)
        with twilio_live(), patch("requests.get", side_effect=OSError("timed out")):
            ts.autosync(self.institute)      # must not raise
        template.refresh_from_db()
        self.assertEqual(template.status, WhatsAppTemplate.Status.RECEIVED)
        self.assertIn("timed out", template.last_error)

    def test_only_this_institute_is_polled(self):
        from accounts.models import Institute

        other = Institute.objects.create(name="Other", code="O", email="o@o.edu")
        WhatsAppTemplate.objects.create(
            institute=other, created_by=self.head,
            audience=WhatsAppTemplate.Audience.GUARDIAN, name="x",
            twilio_name="x", body="Hi {{student_name}}",
            variable_order=["student_name"], content_sid="HX2",
            status=WhatsAppTemplate.Status.RECEIVED)
        self._template(status=WhatsAppTemplate.Status.APPROVED)
        with twilio_live(), patch("requests.get") as get:
            ts.autosync(self.institute)
        get.assert_not_called()

    # ------------------------------------------------------------- the pages
    def test_opening_the_templates_list_syncs(self):
        self._template(last_synced_at=None)
        client = self.client_class()
        client.force_login(self.head)
        with twilio_live(), patch("requests.get", return_value=self._reply()) as get:
            res = client.get(reverse("notifications:api_templates"))
        self.assertEqual(res.status_code, 200)
        get.assert_called_once()

    def test_opening_the_alerts_page_syncs(self):
        self._template(last_synced_at=None)
        client = self.client_class()
        client.force_login(self.head)
        with twilio_live(), patch("requests.get", return_value=self._reply()) as get:
            res = client.get(reverse("notifications:alerts"))
        self.assertEqual(res.status_code, 200)
        get.assert_called_once()
