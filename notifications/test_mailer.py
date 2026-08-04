import base64
import json
from unittest.mock import patch

from django.conf import settings
from django.core import mail
from django.test import TestCase, override_settings

from notifications import mailer
from notifications.mailer import MailResult, normalise_recipients, send_mail


class FakeResponse:
    def __init__(self, status_code=202, text=""):
        self.status_code = status_code
        self.text = text


def sendgrid_on(**extra):
    """Force the SendGrid transport on for a test."""
    return override_settings(
        EMAIL_PROVIDER="sendgrid", SENDGRID_API_KEY="SG.test-key",
        EMAIL_ASYNC=False, **extra,
    )


# --------------------------------------------------------------------------- #
#  Recipient handling
# --------------------------------------------------------------------------- #
class RecipientTests(TestCase):
    def test_accepts_a_bare_string(self):
        self.assertEqual(normalise_recipients("a@b.com"), [{"email": "a@b.com"}])

    def test_accepts_a_list_of_strings(self):
        self.assertEqual(
            normalise_recipients(["a@b.com", "c@d.com"]),
            [{"email": "a@b.com"}, {"email": "c@d.com"}],
        )

    def test_passes_sendgrid_dicts_through(self):
        self.assertEqual(
            normalise_recipients([{"email": "a@b.com", "name": "A"}]),
            [{"email": "a@b.com", "name": "A"}],
        )

    def test_drops_junk(self):
        self.assertEqual(normalise_recipients([{"name": "no email"}, "", "  "]), [])
        self.assertEqual(normalise_recipients(None), [])
        self.assertEqual(normalise_recipients([]), [])

    def test_accepts_model_instances(self):
        from accounts.models import User

        user = User(email="s@c.edu", full_name="Ana Sharma")
        self.assertEqual(normalise_recipients(user),
                         [{"email": "s@c.edu", "name": "Ana Sharma"}])

    def test_empty_recipients_short_circuits(self):
        result = send_mail(To=[], Subject="x", wait=True).result()
        self.assertFalse(result.ok)
        self.assertEqual(str(result), "Empty Recipients")
        self.assertEqual(len(mail.outbox), 0)


# --------------------------------------------------------------------------- #
#  SendGrid transport
# --------------------------------------------------------------------------- #
class SendGridTests(TestCase):
    @sendgrid_on()
    def test_payload_shape(self):
        with patch("requests.post", return_value=FakeResponse()) as post:
            result = send_mail(
                To="s@c.edu", Subject="Hello", Text="plain", HTML="<b>rich</b>",
                uniqueID="U1", messageGroup="G1", utm_source="Test",
                wait=True,
            ).result()

        self.assertTrue(result.ok)
        self.assertEqual(result.status_code, 202)
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://api.sendgrid.com/v3/mail/send")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer SG.test-key")

        payload = json.loads(kwargs["data"])
        self.assertEqual(payload["personalizations"][0]["to"], [{"email": "s@c.edu"}])
        self.assertEqual(payload["subject"], "Hello")
        self.assertEqual(payload["content"][0], {"type": "text/plain", "value": "plain"})
        self.assertEqual(payload["content"][1], {"type": "text/html", "value": "<b>rich</b>"})
        self.assertEqual(payload["personalizations"][0]["custom_args"]["unique-message-id"], "U1")
        self.assertEqual(payload["personalizations"][0]["headers"]["message-group"], "G1")
        self.assertEqual(payload["tracking_settings"]["ganalytics"]["utm_source"], "Test")
        self.assertTrue(payload["tracking_settings"]["open_tracking"]["enable"])
        self.assertIn("reply_to", payload)

    @sendgrid_on()
    def test_from_and_reply_to_default_to_settings(self):
        with patch("requests.post", return_value=FakeResponse()) as post:
            send_mail(To="s@c.edu", Subject="x", wait=True).result()
        payload = json.loads(post.call_args.kwargs["data"])
        self.assertEqual(payload["from"]["email"], "no-reply@geoattend.local")
        self.assertEqual(payload["reply_to"]["email"], "no-reply@geoattend.local")

    @sendgrid_on()
    def test_reply_to_list_replaces_reply_to(self):
        with patch("requests.post", return_value=FakeResponse()) as post:
            send_mail(To="s@c.edu", Subject="x",
                      reply_to_list=["one@c.edu", "two@c.edu"], wait=True).result()
        payload = json.loads(post.call_args.kwargs["data"])
        self.assertEqual(len(payload["reply_to_list"]), 2)
        self.assertNotIn("reply_to", payload)

    @sendgrid_on()
    def test_cc_and_bcc(self):
        with patch("requests.post", return_value=FakeResponse()) as post:
            send_mail(To="s@c.edu", Subject="x", cc="cc@c.edu", bcc=["b@c.edu"],
                      wait=True).result()
        personalization = json.loads(post.call_args.kwargs["data"])["personalizations"][0]
        self.assertEqual(personalization["cc"], [{"email": "cc@c.edu"}])
        self.assertEqual(personalization["bcc"], [{"email": "b@c.edu"}])

    @sendgrid_on()
    def test_recipients_are_batched_at_the_sendgrid_limit(self):
        recipients = [f"s{i}@c.edu" for i in range(2500)]
        with patch("requests.post", return_value=FakeResponse()) as post:
            result = send_mail(To=recipients, Subject="x", wait=True).result()
        self.assertTrue(result.ok)
        self.assertEqual(post.call_count, 3)              # 1000 + 1000 + 500
        sizes = [len(json.loads(c.kwargs["data"])["personalizations"][0]["to"])
                 for c in post.call_args_list]
        self.assertEqual(sizes, [1000, 1000, 500])

    @sendgrid_on()
    def test_cc_and_bcc_shrink_the_batch_size(self):
        """cc/bcc count against the same 1000-recipient cap."""
        recipients = [f"s{i}@c.edu" for i in range(1000)]
        cc = [f"c{i}@c.edu" for i in range(10)]
        with patch("requests.post", return_value=FakeResponse()) as post:
            send_mail(To=recipients, Subject="x", cc=cc, wait=True).result()
        sizes = [len(json.loads(c.kwargs["data"])["personalizations"][0]["to"])
                 for c in post.call_args_list]
        self.assertEqual(sizes, [990, 10])

    @sendgrid_on()
    def test_http_error_is_reported_not_raised(self):
        with patch("requests.post", return_value=FakeResponse(401, '{"errors":["bad key"]}')):
            result = send_mail(To="s@c.edu", Subject="x", wait=True).result()
        self.assertFalse(result.ok)
        self.assertEqual(result.status_code, 401)
        self.assertIn("bad key", result.error)

    @sendgrid_on()
    def test_a_failing_batch_stops_the_run(self):
        recipients = [f"s{i}@c.edu" for i in range(2500)]
        with patch("requests.post", return_value=FakeResponse(500, "boom")) as post:
            result = send_mail(To=recipients, Subject="x", wait=True).result()
        self.assertFalse(result.ok)
        self.assertEqual(post.call_count, 1)

    @sendgrid_on()
    def test_network_exception_becomes_a_result(self):
        with patch("requests.post", side_effect=OSError("connection reset")):
            result = send_mail(To="s@c.edu", Subject="x", wait=True).result()
        self.assertFalse(result.ok)
        self.assertIn("connection reset", result.error)

    @sendgrid_on()
    def test_attachments_are_base64_encoded(self):
        import io

        from django.core.files.base import ContentFile

        payload_file = ContentFile(b"col1,col2\n1,2\n")
        with patch("requests.post", return_value=FakeResponse()) as post:
            send_mail(To="s@c.edu", Subject="x", wait=True,
                      Attachments=[{"file": payload_file, "file_name": "report.csv"}]).result()
        attachment = json.loads(post.call_args.kwargs["data"])["attachments"][0]
        self.assertEqual(attachment["filename"], "report.csv")
        self.assertEqual(attachment["type"], "text/csv")
        self.assertEqual(base64.b64decode(attachment["content"]), b"col1,col2\n1,2\n")
        self.assertEqual(io, io)  # keep the import meaningful

    @sendgrid_on()
    def test_unique_id_defaults_to_something_actually_unique(self):
        ids = []
        with patch("requests.post", return_value=FakeResponse()) as post:
            for _ in range(3):
                send_mail(To="s@c.edu", Subject="x", wait=True).result()
            for call in post.call_args_list:
                payload = json.loads(call.kwargs["data"])
                ids.append(payload["personalizations"][0]["custom_args"]["unique-message-id"])
        self.assertEqual(len(set(ids)), 3)

    @sendgrid_on()
    def test_message_group_defaults_to_the_site_name(self):
        with patch("requests.post", return_value=FakeResponse()) as post:
            send_mail(To="s@c.edu", Subject="x", wait=True).result()
        payload = json.loads(post.call_args.kwargs["data"])
        self.assertEqual(
            payload["personalizations"][0]["custom_args"]["message-group"], "GeoAttend")

    @sendgrid_on(SENDGRID_SANDBOX_MODE=True)
    def test_sandbox_mode_flows_into_the_payload(self):
        with patch("requests.post", return_value=FakeResponse()) as post:
            send_mail(To="s@c.edu", Subject="x", wait=True).result()
        payload = json.loads(post.call_args.kwargs["data"])
        self.assertTrue(payload["mail_settings"]["sandbox_mode"]["enable"])


# --------------------------------------------------------------------------- #
#  Mailchimp Transactional (Mandrill) transport
# --------------------------------------------------------------------------- #
class MandrillResponse:
    """Mandrill answers with JSON, so the fake has to as well."""

    DEFAULT = object()

    def __init__(self, payload=DEFAULT, status_code=200):
        self._payload = ([{"email": "s@c.edu", "status": "sent", "_id": "abc"}]
                         if payload is self.DEFAULT else payload)
        self.status_code = status_code
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


def mailchimp_on(**extra):
    return override_settings(
        EMAIL_PROVIDER="mailchimp", MAILCHIMP_API_KEY="md-test-key",
        EMAIL_ASYNC=False, **extra,
    )


class MailchimpTests(TestCase):
    @mailchimp_on()
    def test_payload_shape(self):
        with patch("requests.post", return_value=MandrillResponse()) as post:
            result = send_mail(To="s@c.edu", Subject="Hi", Text="body",
                               HTML="<b>body</b>", wait=True).result()
        self.assertTrue(result.ok)
        url = post.call_args.args[0]
        body = post.call_args.kwargs["json"]
        self.assertEqual(url, "https://mandrillapp.com/api/1.0/messages/send")
        # The key travels in the body, not an Authorization header.
        self.assertEqual(body["key"], "md-test-key")
        self.assertNotIn("headers", post.call_args.kwargs)
        message = body["message"]
        self.assertEqual(message["to"], [{"email": "s@c.edu", "type": "to"}])
        self.assertEqual(message["subject"], "Hi")
        self.assertEqual(message["text"], "body")
        self.assertEqual(message["html"], "<b>body</b>")
        # Recipients must not see each other.
        self.assertFalse(message["preserve_recipients"])

    @mailchimp_on()
    def test_cc_and_bcc_are_typed_entries_in_the_to_array(self):
        """Mandrill has no separate cc/bcc fields — the type tag is the only marker."""
        with patch("requests.post", return_value=MandrillResponse()) as post:
            send_mail(To="s@c.edu", Subject="Hi", cc="c@c.edu", bcc="b@c.edu",
                      wait=True).result()
        recipients = post.call_args.kwargs["json"]["message"]["to"]
        self.assertEqual(
            [(r["email"], r["type"]) for r in recipients],
            [("s@c.edu", "to"), ("c@c.edu", "cc"), ("b@c.edu", "bcc")],
        )

    @mailchimp_on()
    def test_reply_to_rides_in_headers(self):
        """Mandrill has no reply_to field — it has to go in as a raw header."""
        for value in ("hod@c.edu", {"email": "hod@c.edu", "name": "HoD"}):
            with self.subTest(value=value), \
                    patch("requests.post", return_value=MandrillResponse()) as post:
                send_mail(To="s@c.edu", Subject="Hi", reply_to=value, wait=True).result()
                message = post.call_args.kwargs["json"]["message"]
                self.assertEqual(message["headers"]["Reply-To"], "hod@c.edu")

    @mailchimp_on()
    def test_tags_and_metadata(self):
        with patch("requests.post", return_value=MandrillResponse()) as post:
            send_mail(To="s@c.edu", Subject="Hi", uniqueID="U9",
                      messageGroup="ALERTS", wait=True).result()
        message = post.call_args.kwargs["json"]["message"]
        self.assertEqual(message["tags"], ["ALERTS"])
        self.assertEqual(message["metadata"]["unique-message-id"], "U9")

    @mailchimp_on()
    def test_attachment_uses_name_not_filename(self):
        """SendGrid calls the key `filename`; Mandrill calls it `name`."""
        from django.core.files.base import ContentFile

        with patch("requests.post", return_value=MandrillResponse()) as post:
            send_mail(To="s@c.edu", Subject="x", wait=True,
                      Attachments=[{"file": ContentFile(b"col1,col2\n"),
                                    "file_name": "report.csv"}]).result()
        attachment = post.call_args.kwargs["json"]["message"]["attachments"][0]
        self.assertEqual(attachment["name"], "report.csv")
        self.assertNotIn("filename", attachment)
        self.assertEqual(attachment["type"], "text/csv")
        self.assertEqual(base64.b64decode(attachment["content"]), b"col1,col2\n")

    @mailchimp_on()
    def test_recipients_are_batched(self):
        recipients = [f"s{i}@c.edu" for i in range(2500)]
        with patch("requests.post", return_value=MandrillResponse()) as post:
            send_mail(To=recipients, Subject="x", wait=True).result()
        sizes = [len(call.kwargs["json"]["message"]["to"])
                 for call in post.call_args_list]
        self.assertEqual(sizes, [1000, 1000, 500])

    @mailchimp_on()
    def test_cc_and_bcc_shrink_the_batch(self):
        """cc/bcc sit in the same array, so they eat into the per-call limit."""
        with patch("requests.post", return_value=MandrillResponse()) as post:
            send_mail(To=[f"s{i}@c.edu" for i in range(1000)],
                      cc=[f"c{i}@c.edu" for i in range(10)], wait=True,
                      Subject="x").result()
        sizes = [sum(1 for r in call.kwargs["json"]["message"]["to"] if r["type"] == "to")
                 for call in post.call_args_list]
        self.assertEqual(sizes, [990, 10])

    @mailchimp_on()
    def test_error_object_is_reported(self):
        """A bad key comes back as an object, not the usual per-recipient array."""
        payload = {"status": "error", "code": -1, "name": "Invalid_Key",
                   "message": "Invalid API key"}
        with patch("requests.post", return_value=MandrillResponse(payload, 500)):
            result = send_mail(To="s@c.edu", Subject="x", wait=True).result()
        self.assertFalse(result.ok)
        self.assertIn("Invalid_Key", result.error)

    @mailchimp_on()
    def test_rejected_recipient_is_a_failure_despite_http_200(self):
        payload = [{"email": "s@c.edu", "status": "rejected",
                    "reject_reason": "hard-bounce"}]
        with patch("requests.post", return_value=MandrillResponse(payload)):
            result = send_mail(To="s@c.edu", Subject="x", wait=True).result()
        self.assertFalse(result.ok)
        self.assertIn("hard-bounce", result.error)

    @mailchimp_on()
    def test_queued_counts_as_sent(self):
        payload = [{"email": "s@c.edu", "status": "queued", "_id": "q1"}]
        with patch("requests.post", return_value=MandrillResponse(payload)):
            result = send_mail(To="s@c.edu", Subject="x", wait=True).result()
        self.assertTrue(result.ok)

    @mailchimp_on()
    def test_unparseable_reply_does_not_crash(self):
        with patch("requests.post", return_value=MandrillResponse(None, 502)):
            result = send_mail(To="s@c.edu", Subject="x", wait=True).result()
        self.assertFalse(result.ok)
        self.assertIn("Unexpected reply", result.error)

    @mailchimp_on()
    def test_a_failing_batch_stops_the_run(self):
        payload = {"status": "error", "name": "PaymentRequired", "message": "no credit"}
        with patch("requests.post",
                   return_value=MandrillResponse(payload, 500)) as post:
            send_mail(To=[f"s{i}@c.edu" for i in range(2500)],
                      Subject="x", wait=True).result()
        self.assertEqual(post.call_count, 1)

    @mailchimp_on(MAILCHIMP_API_URL="https://example.test/api/1.0/")
    def test_api_url_is_configurable(self):
        with patch("requests.post", return_value=MandrillResponse()) as post:
            send_mail(To="s@c.edu", Subject="x", wait=True).result()
        self.assertEqual(post.call_args.args[0],
                         "https://example.test/api/1.0/messages/send")


class ProviderSelectionTests(TestCase):
    """EMAIL_PROVIDER is the only switch; each provider has its own key."""

    @override_settings(EMAIL_PROVIDER="mailchimp", MAILCHIMP_API_KEY="md-k",
                       SENDGRID_API_KEY="SG.k", EMAIL_ASYNC=False)
    def test_mailchimp_uses_the_mailchimp_key(self):
        with patch("requests.post", return_value=MandrillResponse()) as post:
            send_mail(To="s@c.edu", Subject="x", wait=True).result()
        self.assertEqual(post.call_args.kwargs["json"]["key"], "md-k")

    @override_settings(EMAIL_PROVIDER="sendgrid", MAILCHIMP_API_KEY="md-k",
                       SENDGRID_API_KEY="SG.k", EMAIL_ASYNC=False)
    def test_sendgrid_uses_the_sendgrid_key(self):
        with patch("requests.post", return_value=FakeResponse()) as post:
            send_mail(To="s@c.edu", Subject="x", wait=True).result()
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer SG.k")

    @override_settings(EMAIL_PROVIDER="django", SENDGRID_API_KEY="SG.k",
                       MAILCHIMP_API_KEY="md-k", EMAIL_ASYNC=False)
    def test_django_provider_ignores_both_keys(self):
        with patch("requests.post") as post:
            send_mail(To="s@c.edu", Subject="x", wait=True).result()
        post.assert_not_called()
        self.assertEqual(len(mail.outbox), 1)

    @override_settings(EMAIL_PROVIDER="mailchimp", MAILCHIMP_API_KEY="", EMAIL_ASYNC=False)
    def test_provider_set_but_key_missing_is_an_explicit_error(self):
        """Silently falling back to console mail would look like a successful send."""
        with patch("requests.post") as post:
            result = send_mail(To="s@c.edu", Subject="x", wait=True).result()
        post.assert_not_called()
        self.assertFalse(result.ok)
        self.assertIn("API_KEY is empty", result.error)
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(EMAIL_PROVIDER="sendgrid", SENDGRID_API_KEY="", EMAIL_ASYNC=False)
    def test_sendgrid_without_a_key_is_an_error_too(self):
        with patch("requests.post") as post:
            result = send_mail(To="s@c.edu", Subject="x", wait=True).result()
        post.assert_not_called()
        self.assertFalse(result.ok)

    def test_the_configured_provider_is_always_a_known_name(self):
        """settings.py normalises and validates, so mailer.py can read it directly."""
        self.assertIn(settings.EMAIL_PROVIDER, ("sendgrid", "mailchimp", "django"))


# --------------------------------------------------------------------------- #
#  Django fallback
# --------------------------------------------------------------------------- #
class FallbackTransportTests(TestCase):
    def test_default_provider_uses_django_backend(self):
        """This is what keeps `runserver` printing mail and tests filling outbox."""
        with patch("requests.post") as post:
            result = send_mail(To="s@c.edu", Subject="Hi", Text="body",
                               HTML="<b>body</b>", wait=True).result()
        post.assert_not_called()
        self.assertTrue(result.ok)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["s@c.edu"])
        self.assertEqual(mail.outbox[0].subject, "Hi")
        self.assertEqual(mail.outbox[0].alternatives[0][0], "<b>body</b>")

    def test_cc_bcc_and_headers_survive(self):
        send_mail(To="s@c.edu", Subject="Hi", cc="c@c.edu", bcc="b@c.edu",
                  uniqueID="U9", messageGroup="G9", wait=True).result()
        message = mail.outbox[0]
        self.assertEqual(message.cc, ["c@c.edu"])
        self.assertEqual(message.bcc, ["b@c.edu"])
        self.assertEqual(message.extra_headers["unique-message-id"], "U9")


# --------------------------------------------------------------------------- #
#  Async behaviour & result object
# --------------------------------------------------------------------------- #
class MailResultTests(TestCase):
    def test_behaves_like_a_string(self):
        result = MailResult("body text", ok=True, status_code=202)
        self.assertEqual(result, "body text")
        self.assertEqual(f"{result}", "body text")
        self.assertTrue(result.ok)

    def test_empty_success_body_is_still_ok(self):
        """SendGrid answers 202 with an empty body — .ok is the only honest signal."""
        result = MailResult("", ok=True, status_code=202)
        self.assertEqual(str(result), "")
        self.assertTrue(result.ok)

    @override_settings(EMAIL_ASYNC=True)
    def test_async_send_returns_a_future_that_resolves(self):
        future = send_mail(To="s@c.edu", Subject="Hi")
        result = future.result(timeout=10)
        self.assertTrue(result.ok)
        self.assertEqual(len(mail.outbox), 1)

    @override_settings(EMAIL_ASYNC=True, EMAIL_MAX_WORKERS=1)
    def test_pool_is_shared_and_never_zero_workers(self):
        """cpu_count()-2 is <= 0 on small machines; the pool must still build."""
        mailer._executor = None
        try:
            first = mailer._pool()
            self.assertGreaterEqual(first._max_workers, 1)
            self.assertIs(first, mailer._pool())
        finally:
            mailer._executor = None

    def test_mutable_defaults_are_not_shared_between_calls(self):
        send_mail(To="a@c.edu", Subject="one", cc=["x@c.edu"], wait=True).result()
        send_mail(To="b@c.edu", Subject="two", wait=True).result()
        self.assertEqual(mail.outbox[0].cc, ["x@c.edu"])
        self.assertEqual(mail.outbox[1].cc, [])       # would leak with cc=[] defaults


# --------------------------------------------------------------------------- #
#  Everything really does go through the one function
# --------------------------------------------------------------------------- #
class SingleEntryPointTests(TestCase):
    def test_no_module_sends_email_directly(self):
        """Guard the rule: only mailer.py may touch django.core.mail."""
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent
        offenders = []
        for app in ("accounts", "academics", "attendance", "dashboard", "core",
                    "notifications", "config"):
            for path in (root / app).rglob("*.py"):
                if "__pycache__" in path.parts or "migrations" in path.parts:
                    continue
                if path.name.startswith("test") or path.name in (
                    "tests.py", "mailer.py", "settings.py"
                ):
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
                if "EmailMultiAlternatives" in text or "from django.core.mail" in text:
                    offenders.append(str(path.relative_to(root)))
        self.assertEqual(offenders, [], f"These bypass the mailer: {offenders}")

    def test_account_emails_route_through_the_mailer(self):
        from accounts.emails import send_otp

        with patch("notifications.mailer.send_mail_func",
                   return_value=MailResult("", ok=True, status_code=202)) as sender:
            send_otp("s@c.edu", "123456", "verify your email")
        self.assertEqual(sender.call_count, 1)
        self.assertEqual(sender.call_args.kwargs["To"], [{"email": "s@c.edu"}])
        self.assertIn("123456", sender.call_args.kwargs["Subject"])

    def test_attendance_request_routes_through_the_mailer(self):
        from notifications.mailer import send_template_mail

        with patch("notifications.mailer.send_mail") as sender:
            send_template_mail("Subj", "s@c.edu", "otp",
                               {"code": "1", "purpose": "x", "ttl": 10})
        self.assertEqual(sender.call_args.kwargs["To"], "s@c.edu")
        self.assertIn("Subj", sender.call_args.kwargs["Subject"])
