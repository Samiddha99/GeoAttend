"""
The one and only way this project sends email.

Every outbound message — OTPs, invitations, welcome mails, attendance links,
low-attendance alerts — goes through :func:`send_mail` below. Nothing else in
the codebase may touch ``EmailMultiAlternatives`` or ``django.core.mail`` directly.

Three transports sit behind that single door, chosen by ``EMAIL_PROVIDER``:

* ``sendgrid``  — SendGrid v3 REST API (tracking, custom args, batching).
  Reads ``SENDGRID_API_KEY``.
* ``mailchimp`` — Mailchimp Transactional, formerly Mandrill. Reads
  ``MAILCHIMP_API_KEY``. Note this is *not* the Mailchimp Marketing API; a
  marketing key (one ending ``-usNN``) will not authenticate here.
* ``django``    — Django's own backend and the default, so ``runserver`` still
  prints mail to the console and the test suite still fills ``mail.outbox``.

Each provider keeps its own key setting, so both can stay configured and
switching is a one-word change. ``EMAIL_PROVIDER`` is lowercased, alias-resolved
and validated in ``settings.py``, which is why the dispatch below can read it
directly without re-checking it.

Sending is asynchronous by default (a shared thread pool), matching the original
behaviour: ``send_mail`` returns a :class:`~concurrent.futures.Future`. Call
``.result()`` on it when you need to know whether delivery actually succeeded —
the alert campaign does exactly that so its delivery report is truthful.
"""
import base64
import json
import logging
import mimetypes
import traceback
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from email.utils import parseaddr
import requests

from django.conf import settings

log = logging.getLogger("geoattend")

_executor = None


# --------------------------------------------------------------------------- #
#  Result
# --------------------------------------------------------------------------- #
class MailResult(str):
    """
    The provider's response body, with a reliable success flag attached.

    SendGrid answers a successful send with ``202`` and an *empty* body, so the
    raw text alone cannot tell you whether anything happened. This subclasses
    ``str`` so existing code that prints or logs the result keeps working, while
    callers that care can check ``.ok``.
    """

    def __new__(cls, text="", ok=False, status_code=0, error=""):
        obj = super().__new__(cls, text or "")
        obj.ok = ok
        obj.status_code = status_code
        obj.error = error
        return obj

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"<MailResult ok={self.ok} status={self.status_code} {str(self)[:80]!r}>"


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #
def _pool():
    """One shared pool for the process.

    The original code built a new ``ThreadPoolExecutor`` per call, which would
    spawn hundreds of pools during a 500-student alert run — and crashes outright
    on a 1- or 2-core machine, where ``cpu_count() - 2`` is zero or negative.
    """
    global _executor
    if _executor is None:
        workers = getattr(settings, "EMAIL_MAX_WORKERS", 0) or 0
        if workers <= 0:
            try:
                import multiprocessing

                workers = multiprocessing.cpu_count() - 2
            except Exception:
                workers = 2
        _executor = ThreadPoolExecutor(
            max_workers=max(1, min(workers, 32)), thread_name_prefix="mailer"
        )
    return _executor



def normalise_recipients(value):
    """
    Accept whatever a caller finds convenient and return SendGrid's shape.

        "a@b.com"                        → [{"email": "a@b.com"}]
        ["a@b.com", "c@d.com"]           → [{"email": …}, {"email": …}]
        [{"email": …, "name": …}]        → unchanged
        User / StudentProfile instances  → their .email
    """
    if value is None or value == "" or value == []:
        return []
    if isinstance(value, (str, dict)) or not isinstance(value, (list, tuple, set)):
        value = [value]          # a bare string, dict or model instance
    out = []
    for item in value:
        if isinstance(item, dict):
            if item.get("email"):
                out.append({k: v for k, v in item.items() if k in ("email", "name") and v})
        elif isinstance(item, str):
            if item.strip():
                out.append({"email": item.strip()})
        else:
            email = getattr(item, "email", None)
            if email:
                entry = {"email": email}
                name = getattr(item, "full_name", None) or getattr(item, "name", None)
                if name:
                    entry["name"] = str(name)
                out.append(entry)
    return out


# --------------------------------------------------------------------------- #
#  Public entry point
# --------------------------------------------------------------------------- #
def send_mail(
    From: str = None,
    To=None,
    Subject: str = "",
    Text: str = " ",
    HTML: str = " ",
    cc=None,
    bcc=None,
    Attachments: list = None,
    From_Name: str = None,
    reply_to=None,
    reply_to_list=None,
    uniqueID: str = None,
    messageGroup: str = None,
    Sandbox_Mode: bool = None,
    utm_source: str = "Sent Email",
    wait: bool = False,
):
    """
    Queue an email and return a ``Future`` resolving to a :class:`MailResult`.

    ``From``/``From_Name``/``reply_to`` default to ``DEFAULT_FROM_EMAIL`` and
    ``EMAIL_SENDER_NAME``, so most callers only supply ``To``, ``Subject`` and a body.

    Pass ``wait=True`` (or call ``.result()`` on the future) to send synchronously
    and get the outcome back — required when you need to record delivery status.

    All mutable defaults are ``None`` sentinels: the original signature's
    ``cc=[]`` / ``Attachments=[{...}]` style shares one list across every call,
    which is a latent aliasing bug even though today's code only reads them.
    """
    kwargs = {
        "From": From or settings.DEFAULT_FROM_EMAIL,
        "To": normalise_recipients(To),
        "Subject": Subject,
        "Text": Text if Text is not None else " ",
        "HTML": HTML if HTML is not None else " ",
        "cc": normalise_recipients(cc),
        "bcc": normalise_recipients(bcc),
        "Attachments": Attachments if Attachments is not None else [],
        "From_Name": From_Name or settings.EMAIL_SENDER_NAME,
        # Accept "a@b.com" as well as {"email": ..., "name": ...} — every
        # transport reads .get("email"), so a bare string would fail the send.
        "reply_to": (normalise_recipients(reply_to)[0] if reply_to else {
            "email": settings.DEFAULT_FROM_EMAIL,
            "name": settings.EMAIL_SENDER_NAME,
        }),
        "reply_to_list": normalise_recipients(reply_to_list),
        # A genuinely unique id per message, so SendGrid event webhooks can be
        # tied back to one send; `messageGroup` is the field meant for grouping.
        "uniqueID": uniqueID or uuid.uuid4().hex,
        "messageGroup": messageGroup or settings.SITE_NAME,
        "Sandbox_Mode": (
            getattr(settings, "SENDGRID_SANDBOX_MODE", False)
            if Sandbox_Mode is None else Sandbox_Mode
        ),
        "utm_source": utm_source,
    }

    if wait or not getattr(settings, "EMAIL_ASYNC", True):
        future = Future()
        future.set_result(send_mail_func(**kwargs))
        return future
    return _pool().submit(send_mail_func, **kwargs)


# --------------------------------------------------------------------------- #
#  Workers
# --------------------------------------------------------------------------- #



def send_mail_func(**kwargs):
    """Do the actual work. Never raises — always returns a :class:`MailResult`."""
    try:
        if not kwargs["To"]:
            return MailResult("Empty Recipients", ok=False, error="No recipients")
        chosen = settings.EMAIL_PROVIDER
        if (chosen == "sendgrid" and not getattr(settings, "SENDGRID_API_KEY", "")) or (chosen == "mailchimp" and not getattr(settings, "MAILCHIMP_API_KEY", "")):
            # Better a clear message than a confusing 401 from the provider.
            return MailResult(
                "None", ok=False,
                error=f"EMAIL_PROVIDER is '{chosen}' but API_KEY is empty.")
        if chosen == "mailchimp":
            return _send_via_mailchimp(**kwargs)
        if chosen == "sendgrid":
            return _send_via_sendgrid(**kwargs)
        return _send_via_django(**kwargs)
    except Exception as exc:
        traceback.print_exc()
        log.error("Email send failed: %s", exc)
        return MailResult("None", ok=False, error=f"{type(exc).__name__}: {exc}")


def _build_attachments(items):
    attachments = []
    for attachment in items or []:
        file = attachment.get("file")
        file_name = attachment.get("file_name")
        if file is None or file_name is None:
            continue
        content = file.open().read() if hasattr(file, "open") else (
            file.read() if hasattr(file, "read") else file
        )
        if isinstance(content, str):
            content = content.encode()
        attachments.append({
            "type": mimetypes.MimeTypes().guess_type(file_name)[0] or "application/octet-stream",
            "content": base64.b64encode(content).decode(),
            "filename": file_name,
            "disposition": "attachment",
        })
    return attachments


def _send_via_mailchimp(**kwargs):
    """
    Mailchimp Transactional (formerly Mandrill).

        POST https://mandrillapp.com/api/1.0/messages/send

    Note this is *Mailchimp Transactional*, not the Mailchimp Marketing API —
    marketing keys will not work here. The key travels in the JSON body rather
    than a header, which is unusual but is what the API expects.

    Two shape differences from SendGrid worth knowing:
      * cc and bcc live inside the same ``to`` array, tagged with ``type``
      * attachments use ``name``, not ``filename``
    """
    base = getattr(settings, "MAILCHIMP_API_URL", "https://mandrillapp.com/api/1.0")
    print(settings.MAILCHIMP_API_KEY)
    message = {
        "from_email": kwargs["From"],
        "from_name": kwargs["From_Name"],
        "subject": kwargs["Subject"],
        "text": kwargs["Text"] or " ",
        "html": kwargs["HTML"] or " ",
        "track_opens": True,
        "track_clicks": True,
        # Off, so a batch of guardians never see each other's addresses.
        "preserve_recipients": False,
        "tags": [str(kwargs["messageGroup"])[:50]],
        "metadata": {"unique-message-id": kwargs["uniqueID"]},
        "headers": {},
    }

    reply_to = kwargs["reply_to_list"] or ([kwargs["reply_to"]] if kwargs["reply_to"] else [])
    addresses = [r["email"] for r in reply_to if r.get("email")]
    if addresses:
        message["headers"]["Reply-To"] = ", ".join(addresses)

    attachments = [
        {"type": a["type"], "name": a["filename"], "content": a["content"]}
        for a in _build_attachments(kwargs["Attachments"])
    ]
    if attachments:
        message["attachments"] = attachments

    if kwargs["Sandbox_Mode"]:
        # Mandrill has no sandbox flag; skip the call rather than really send.
        return MailResult("", ok=True, status_code=200)

    # cc/bcc count toward the same 1000-recipient ceiling as `to`.
    extras = ([dict(r, type="cc") for r in kwargs["cc"]]
              + [dict(r, type="bcc") for r in kwargs["bcc"]])
    limit = max(1, 1000 - len(extras))
    recipients = kwargs["To"]
    batches = (
        [recipients] if len(recipients) <= limit
        else [recipients[i:i + limit] for i in range(0, len(recipients), limit)]
    )

    result = MailResult("None", ok=False, error="Nothing sent")
    for batch in batches:
        # A fresh dict per batch — sharing one and mutating `to` works only
        # because requests serialises immediately, which is too subtle to rely on.
        payload = dict(message, to=[dict(r, type="to") for r in batch] + extras)
        response = requests.post(
            f"{base.rstrip('/')}/messages/send",
            json={"key": settings.MAILCHIMP_API_KEY, "message": payload},
            timeout=getattr(settings, "EMAIL_TIMEOUT", 20),
        )
        result = _read_mailchimp(response)
        if not result.ok:
            log.error("Mailchimp rejected a message: %s", result.error)
            break            # stop rather than hammer a failing API
    return result


def _read_mailchimp(response):
    """
    Turn Mandrill's reply into a :class:`MailResult`.

    Success is a JSON array, one entry per recipient, each with its own status —
    so a 200 can still mean "rejected". Errors come back as an object with
    ``status: "error"``, usually on HTTP 500.
    """
    try:
        payload = response.json()
    except ValueError:
        return MailResult(response.text, ok=False, status_code=response.status_code,
                          error=f"HTTP {response.status_code}: {response.text[:300]}")

    if isinstance(payload, dict) and payload.get("status") == "error":
        name = payload.get("name", "Error")
        return MailResult(
            response.text, ok=False, status_code=response.status_code,
            error=f"{name}: {payload.get('message', '')}"[:300])

    entries = [e for e in (payload if isinstance(payload, list) else [payload])
               if isinstance(e, dict)]
    if not entries:
        return MailResult(response.text, ok=False, status_code=response.status_code,
                          error=f"Unexpected reply from Mailchimp: {response.text[:200]}")
    accepted = {"sent", "queued", "scheduled"}
    bad = [e for e in entries if str(e.get("status", "")).lower() not in accepted]
    if bad:
        first = bad[0]
        return MailResult(
            response.text, ok=False, status_code=response.status_code,
            error=("{email}: {status}{reason}".format(
                email=first.get("email", "?"),
                status=first.get("status", "?"),
                reason=f" ({first['reject_reason']})" if first.get("reject_reason") else "",
            ) + (f" — and {len(bad) - 1} more" if len(bad) > 1 else ""))[:300])

    return MailResult(response.text, ok=True, status_code=response.status_code)


def _send_via_django(**kwargs):
    """
    Fallback with no API key: Django's own backend.

    Console in development, locmem in tests — so the project runs and the suite
    passes without any provider account.
    """
    from django.core.mail import EmailMultiAlternatives

    reply_to = kwargs["reply_to_list"] or ([kwargs["reply_to"]] if kwargs["reply_to"] else [])
    message = EmailMultiAlternatives(
        subject=kwargs["Subject"],
        body=kwargs["Text"] or " ",
        from_email=f'{kwargs["From_Name"]} <{kwargs["From"]}>',
        to=[r["email"] for r in kwargs["To"]],
        cc=[r["email"] for r in kwargs["cc"]],
        bcc=[r["email"] for r in kwargs["bcc"]],
        reply_to=[r["email"] for r in reply_to if r.get("email")],
        headers={
            "unique-message-id": kwargs["uniqueID"],
            "message-group": kwargs["messageGroup"],
        },
    )
    if kwargs["HTML"] and kwargs["HTML"].strip():
        message.attach_alternative(kwargs["HTML"], "text/html")
    for attachment in _build_attachments(kwargs["Attachments"]):
        message.attach(attachment["filename"],
                       base64.b64decode(attachment["content"]), attachment["type"])
    if kwargs["Sandbox_Mode"]:
        return MailResult("", ok=True, status_code=202)
    sent = message.send(fail_silently=False)
    return MailResult("", ok=bool(sent), status_code=202 if sent else 0,
                      error="" if sent else "Backend reported 0 messages sent")


def _send_via_sendgrid(**kwargs):
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + settings.SENDGRID_API_KEY,
    }
    payload = {
        "personalizations": [{
            "headers": {
                "unique-message-id": kwargs["uniqueID"],
                "message-group": kwargs["messageGroup"],
            },
            "custom_args": {
                "unique-message-id": kwargs["uniqueID"],
                "message-group": kwargs["messageGroup"],
            },
        }],
        "from": {"email": kwargs["From"], "name": kwargs["From_Name"]},
        "subject": kwargs["Subject"],
        "content": [
            {"type": "text/plain", "value": kwargs["Text"] or " "},
            {"type": "text/html", "value": kwargs["HTML"] or " "},
        ],
        "mail_settings": {
            "sandbox_mode": {"enable": kwargs["Sandbox_Mode"]},
            "bypass_spam_management": {"enable": True},
            "bypass_bounce_management": {"enable": True},
            "bypass_unsubscribe_management": {"enable": True},
        },
        "tracking_settings": {
            "click_tracking": {"enable": True, "enable_text": True},
            "open_tracking": {"enable": True},
            "ganalytics": {
                "enable": True,
                "utm_source": kwargs["utm_source"],
                "utm_medium": "Email",
                "utm_campaign": "Email Communication",
            },
        },
    }
    if kwargs["reply_to_list"]:
        payload["reply_to_list"] = kwargs["reply_to_list"]
    else:
        payload["reply_to"] = kwargs["reply_to"]

    attachments = _build_attachments(kwargs["Attachments"])
    if attachments:
        payload["attachments"] = attachments
    if kwargs["cc"]:
        payload["personalizations"][0]["cc"] = kwargs["cc"]
    if kwargs["bcc"]:
        payload["personalizations"][0]["bcc"] = kwargs["bcc"]

    # SendGrid caps a single request at 1000 recipients, cc and bcc included.
    limit = max(1, 1000 - (len(kwargs["cc"]) + len(kwargs["bcc"])))
    recipients = kwargs["To"]
    batches = (
        [recipients] if len(recipients) <= limit
        else [recipients[i:i + limit] for i in range(0, len(recipients), limit)]
    )

    result = MailResult("None", ok=False, error="Nothing sent")
    for batch in batches:
        payload["personalizations"][0]["to"] = batch
        response = requests.post(
            "https://api.sendgrid.com/v3/mail/send",
            data=json.dumps(payload),
            headers=headers,
            timeout=getattr(settings, "SENDGRID_TIMEOUT", 20),
        )
        ok = 200 <= response.status_code < 300
        result = MailResult(
            response.text, ok=ok, status_code=response.status_code,
            error="" if ok else f"HTTP {response.status_code}: {response.text[:300]}",
        )
        if not ok:
            log.error("SendGrid rejected a message: %s", result.error)
            break            # stop early rather than hammering a failing API
    return result



# --------------------------------------------------------------------------- #
#  Convenience used by every templated email in the project
# --------------------------------------------------------------------------- #
def send_template_mail(subject, to, template, context=None, **options):
    """
    Render ``emails/<template>.html`` + ``.txt`` and send them as one message.

    This is what the rest of the project calls; it funnels straight into
    :func:`send_mail`, so there is still exactly one place that sends email.
    """
    from django.template.loader import render_to_string
    from django.utils import timezone

    context = {
        "SITE_NAME": settings.SITE_NAME,
        "SITE_URL": settings.SITE_URL,
        "year": timezone.now().year,
        **(context or {}),
    }
    return send_mail(
        To=to,
        Subject=subject,
        Text=render_to_string(f"emails/{template}.txt", context),
        HTML=render_to_string(f"emails/{template}.html", context),
        **options,
    )
