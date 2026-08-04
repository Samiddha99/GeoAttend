"""
WhatsApp delivery via Twilio.

Plain functions, no class hierarchy. The whole public surface is:

    send_whatsapp(to, message)                      → Result
    send_whatsapp(to, message, content_sid=…, content_variables={…})
    normalise_msisdn("98765 43210")                 → ("+919876543210", None)
    is_configured()                                 → bool

With no Twilio credentials in the environment the module runs in **console
mode**: messages are written to the server log and reported as sent, so the
whole alert flow can be rehearsed before an account exists.

--------------------------------------------------------------------------
IMPORTANT — WhatsApp's 24-hour rule
--------------------------------------------------------------------------
WhatsApp only allows *free-form* text (the ``body=`` form) inside a 24-hour
"customer service window", which opens when the recipient messages your number
first. A guardian who has never messaged the institute is outside that window,
so a free-form alert to them is **rejected by WhatsApp** (error 63016) even
though the credentials are perfectly valid.

For business-initiated notifications you must use a **pre-approved Content
Template** and pass its SID plus the variables:

    send_whatsapp(
        number,
        "",                                   # body is ignored in this mode
        content_sid="HXb5b62575e6e4ff6129ad7c8efe1f983e",
        content_variables={"1": "Ana Sharma", "2": "61.3"},
    )

Set ``TWILIO_CONTENT_SID`` to route every alert through one approved template.

The **sandbox does not exempt you** — Twilio's own docs say "for business-initiated
messages from the Sandbox, you can use only pre-approved templates". Free-form
appears to work there purely because sending ``join <code>`` opens a 24-hour
window; once that lapses, sandbox free-form fails exactly like production. The
sandbox ships three fixed test templates (appointment / order / verification) and
will not accept custom ones, so an alert template must be registered against a
real WhatsApp sender.
"""
import hashlib
import json
import logging
import re
from dataclasses import dataclass

import requests

from django.conf import settings

log = logging.getLogger("geoattend")

_client = None


@dataclass
class Result:
    """What every send returns."""

    ok: bool
    provider_id: str = ""
    error: str = ""
    status: str = ""


# --------------------------------------------------------------------------- #
#  Phone numbers
# --------------------------------------------------------------------------- #
def normalise_msisdn(raw, default_country_code=None):
    """
    '98765 43210' → '+919876543210'.  Returns ``(number, error)``.

    Twilio is strict about E.164, so we normalise once here rather than trusting
    whatever the spreadsheet happened to contain.
    """
    if not raw:
        return "", "no number on record"
    cc = str(default_country_code or _conf("DEFAULT_COUNTRY_CODE", "91")).lstrip("+")
    text = re.sub(r"[\s\-().]", "", str(raw).strip())
    if text.startswith("00"):
        text = "+" + text[2:]
    plus = text.startswith("+")
    digits = re.sub(r"\D", "", text)
    if not digits:
        return "", f"'{raw}' is not a phone number"
    if not plus:
        # Local format: drop a trunk zero, then prepend the country code unless
        # the number already carries it.
        digits = digits.lstrip("0")
        if not (digits.startswith(cc) and len(digits) > 10):
            digits = cc + digits
    if not 8 <= len(digits) <= 15:
        return "", f"'{raw}' does not look like a valid number"
    return "+" + digits, None


# --------------------------------------------------------------------------- #
#  Configuration
# --------------------------------------------------------------------------- #
def _conf(key, default=""):
    return settings.WHATSAPP.get(key, default)


def is_configured():
    """True when real Twilio credentials are present."""
    return bool(_conf("ACCOUNT_SID") and _conf("AUTH_TOKEN") and _conf("FROM_NUMBER"))


def get_client():
    """Lazily build (and reuse) the Twilio client. Patch this in tests."""
    global _client
    if _client is None:
        from twilio.rest import Client

        _client = Client(_conf("ACCOUNT_SID"), _conf("AUTH_TOKEN"))
    return _client


def reset_client():
    """Drop the cached client — call after changing credentials."""
    global _client
    _client = None


# --------------------------------------------------------------------------- #
#  Sending
# --------------------------------------------------------------------------- #
def send_whatsapp(to, message, *, content_sid=None, content_variables=None):
    """
    Send one WhatsApp message. Never raises — always returns a :class:`Result`.

        result = send_whatsapp("+919812345670", "Your attendance is 61%.")
        if not result.ok:
            log.error(result.error)

    Pass ``content_sid`` (and optionally ``content_variables``) to send an
    approved template instead of free-form text — required for anyone outside
    the 24-hour window. See the module docstring.
    """
    if not _conf("ENABLED", True):
        return Result(False, error="WhatsApp sending is disabled in settings.")

    number, error = normalise_msisdn(to)
    if error:
        return Result(False, error=error)

    content_sid = content_sid or _conf("CONTENT_SID") or None
    if not content_sid and not (message or "").strip():
        return Result(False, error="Message body is empty.")

    if not is_configured():
        return _console(number, message, content_sid, content_variables)

    try:
        sender = _conf("FROM_NUMBER")
        payload = {
            "from_": sender if sender.startswith("whatsapp:") else f"whatsapp:{sender}",
            "to": f"whatsapp:{number}",
        }
        if _conf("STATUS_CALLBACK"):
            payload["status_callback"] = _conf("STATUS_CALLBACK")
        if content_sid:
            payload["content_sid"] = content_sid
            if content_variables:
                payload["content_variables"] = json.dumps(content_variables)
        else:
            payload["body"] = message

        sent = get_client().messages.create(**payload)
        # Twilio queues first and delivers asynchronously, so "queued"/"accepted"
        # is success for this request; the real outcome arrives on the status
        # callback later.
        return Result(True, provider_id=sent.sid or "", status=sent.status or "")
    except Exception as exc:
        return Result(False, error=_explain(exc))


def _console(number, message, content_sid, content_variables):
    """No credentials — write to the log so the flow still works in dev."""
    body = message
    if content_sid:
        body = f"[template {content_sid}] {json.dumps(content_variables or {})}\n{message}"
    log.info("[WhatsApp → %s]\n%s\n%s", number, body, "-" * 50)
    if _conf("CONSOLE_ECHO", True):
        print(f"\n--- WhatsApp to {number} ---\n{body}\n{'-' * 50}")
    return Result(True, provider_id=f"console-{abs(hash((number, body))) % 10**10}",
                  status="console")


#: Twilio error codes worth translating — the raw text is unhelpful to staff.
_HINTS = {
    63016: ("WhatsApp rejected a free-form message sent outside the 24-hour window. "
            "Business-initiated alerts need a pre-approved Content Template — set "
            "TWILIO_CONTENT_SID."),
    63015: "The recipient has not opted in to messages from this sender.",
    63007: "That 'from' number is not a WhatsApp-enabled Twilio sender.",
    63003: "The recipient's number is not reachable on WhatsApp.",
    21211: "Twilio rejected the recipient number as invalid.",
    20003: "Twilio authentication failed — check TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN.",
}


def _explain(exc):
    """Turn a Twilio exception into something a HoD can act on."""
    code = getattr(exc, "code", None)
    detail = getattr(exc, "msg", None) or str(exc)
    log.error("WhatsApp send failed (%s): %s", code, detail)
    if code in _HINTS:
        return f"{_HINTS[code]} (Twilio {code})"
    if code:
        return f"Twilio {code}: {detail}"[:300]
    return f"{type(exc).__name__}: {detail}"[:300]


# --------------------------------------------------------------------------- #
#  Content templates (Twilio Content API)
#
#  WhatsApp will not accept business-initiated free-form text, so an institute
#  registers its wording once and Meta approves it.  Three calls cover the whole
#  lifecycle:
#
#      create_content(...)      POST   /v1/Content
#      submit_for_approval(...) POST   /v1/Content/{sid}/ApprovalRequests/whatsapp
#      fetch_approval(...)      GET    /v1/Content/{sid}/ApprovalRequests
# --------------------------------------------------------------------------- #
PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")

#: Twilio → our own status vocabulary.  Anything unrecognised is kept verbatim
#: so a new WhatsApp state shows up in the UI rather than silently vanishing.
STATUS_MAP = {
    "received": "RECEIVED",
    "pending": "PENDING",
    "approved": "APPROVED",
    "rejected": "REJECTED",
    "paused": "PAUSED",
    "disabled": "DISABLED",
}


def to_numbered(body):
    """
    Convert our placeholder names into WhatsApp's numbered slots.

        "Hi {{first_name}}, you are at {{percentage}}%"
        → ("Hi {{1}}, you are at {{2}}%", ["first_name", "percentage"])

    A placeholder used twice reuses its slot, which is what WhatsApp expects.
    """
    order = []

    def swap(match):
        name = match.group(1)
        if name not in order:
            order.append(name)
        return "{{%d}}" % (order.index(name) + 1)

    return PLACEHOLDER_RE.sub(swap, body or ""), order


def sample_variables(order):
    """Example values Twilio shows reviewers alongside the template."""
    samples = {
        "student_name": "Ananya Sharma", "first_name": "Ananya",
        "class_roll": "01", "exam_roll": "CSE22001",
        "roll_number": "01", "batch": "2022-26",
        "department": "Computer Science", "institute": "Demo Institute",
        "guardian_name": "Mr. R. Sharma", "student_email": "ananya@demo.edu",
        "student_mobile": "+919812345670", "percentage": "61.3",
        "threshold": "75", "shortfall": "13.7", "held": "31",
        "attended": "19", "missed": "12", "subject_code": "DSA",
        "subject_name": "Data Structures", "subject_list": "DSA: 19/31 (61.3%)",
        "from_date": "01 Jan 2026", "to_date": "31 Jul 2026",
        "sender_name": "Dr. A. Banerjee", "sender_role": "Head of Department",
    }
    return {str(i): samples.get(name, name.replace("_", " ").title())
            for i, name in enumerate(order, start=1)}


CONTENT_API = "https://content.twilio.com/v1"


def _content_get(path, timeout=None):
    """GET from the Content API and return ``(data, error)``."""
    try:
        response = requests.get(
            f"{CONTENT_API}{path}",
            auth=(_conf("ACCOUNT_SID"), _conf("AUTH_TOKEN")),
            timeout=timeout or _conf("TIMEOUT", 20),
        )
    except Exception as exc:
        log.error("Content API GET %s failed: %s", path, exc)
        return None, f"{type(exc).__name__}: {exc}"[:300]
    return _content_result(response, path)


def _content_post(path, body):
    """
    POST to the Content API and return ``(data, error)``.

    Deliberately not the SDK. ``contents.create()`` calls ``.to_dict()`` on its
    argument, so a plain dict raises "'dict' object has no attribute 'to_dict'"
    and the request never leaves the process; the typed models that replaced it
    are version-specific and serialise thirteen null message types alongside
    the one being used. The REST endpoint takes exactly the JSON documented in
    the module docstring, and has not moved.
    """
    try:
        response = requests.post(
            f"{CONTENT_API}{path}",
            json=body,
            auth=(_conf("ACCOUNT_SID"), _conf("AUTH_TOKEN")),
            timeout=_conf("TIMEOUT", 20),
        )
    except Exception as exc:                       # network, DNS, TLS
        log.error("Content API call failed: %s", exc)
        return None, f"{type(exc).__name__}: {exc}"[:300]

    return _content_result(response, path)


def _content_result(response, path):
    """Shared reply handling for both Content API verbs."""
    try:
        data = response.json()
    except ValueError:
        data = None
    # A gateway error page parses as JSON `null`, not a dict — guard the shape
    # rather than the parse, or the error path itself raises.
    if not isinstance(data, dict):
        data = {}
    if response.status_code >= 400:
        code = data.get("code")
        # response.text on a gateway error is "null" or "{}" — useless to a HoD.
        detail = (data.get("message")
                  or (response.text[:200] if len(response.text or "") > 4 else "")
                  or f"Twilio returned HTTP {response.status_code}.")
        log.error("Content API %s -> %s %s: %s", path, response.status_code, code, detail)
        if code in _HINTS:
            return None, f"{_HINTS[code]} (Twilio {code})"
        return None, (f"Twilio {code}: {detail}" if code else detail)[:300]
    return data, None


def create_content(friendly_name, body, language="en"):
    """
    Register the wording with Twilio.  Returns ``(content_sid, order, error)``.

    Nothing is sent to WhatsApp yet — that is :func:`submit_for_approval`.
    """
    numbered, order = to_numbered(body)
    if not numbered.strip():
        return "", [], "The template body is empty."
    if not is_configured():
        # Console mode: fabricate a SID so the whole workflow is testable.
        fake = "HX" + hashlib.sha256(f"{friendly_name}{numbered}".encode()).hexdigest()[:32]
        log.info("[WhatsApp template stub] %s → %s\n%s", friendly_name, fake, numbered)
        return fake, order, None
    data, error = _content_post("/Content", {
        "friendly_name": friendly_name,
        "language": language,
        "variables": sample_variables(order),
        "types": {"twilio/text": {"body": numbered}},
    })
    if error:
        return "", order, error
    return data.get("sid", ""), order, None


def submit_for_approval(content_sid, name, category="UTILITY"):
    """Ask WhatsApp to review it.  Returns ``(status, rejection_reason, error)``."""
    if not is_configured():
        return "RECEIVED", "", None
    data, error = _content_post(
        f"/Content/{content_sid}/ApprovalRequests/whatsapp",
        {"name": name, "category": category})
    if error:
        return "", "", error
    raw = str(data.get("status", "") or "").lower()
    return (STATUS_MAP.get(raw, raw.upper() or "RECEIVED"),
            data.get("rejection_reason") or "", None)


def fetch_approval(content_sid, timeout=None):
    """
    Poll the current state.  Returns ``(status, rejection_reason, error)``.

    `timeout` lets an opportunistic sync (one triggered by opening a page)
    give up quickly rather than holding the request open for the full default.
    """
    if not is_configured():
        return "APPROVED", "", None      # console mode: pretend Meta said yes
    data, error = _content_get(f"/Content/{content_sid}/ApprovalRequests", timeout)
    if error:
        return "", "", error
    whatsapp = data.get("whatsapp") or {}
    if not isinstance(whatsapp, dict):
        whatsapp = {}
    raw = str(whatsapp.get("status", "") or "").lower()
    return (STATUS_MAP.get(raw, raw.upper()),
            whatsapp.get("rejection_reason") or "", None)


def delete_content(content_sid):
    """Remove a template from Twilio. Returns an error string, or None."""
    if not is_configured() or not content_sid:
        return None
    try:
        get_client().content.v1.contents(content_sid).delete()
        return None
    except Exception as exc:
        return _explain(exc)
