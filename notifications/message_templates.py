"""
Default low-attendance message bodies and the ``{{placeholder}}`` engine.

Senders may edit any of these before sending; edits apply to that send only and
never overwrite the defaults below.

Rendering deliberately does **not** use the Django template engine — these
strings are typed by staff into a textarea, and we want dumb, predictable
substitution with no tag evaluation, no filters and no attribute traversal.
"""
import re

PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")

#: Every placeholder a sender may use, with the help text shown in the UI.
PLACEHOLDERS = {
    "student_name": "Student's full name",
    "first_name": "Student's first name",
    "class_roll": "Class roll",
    "exam_roll": "Exam / registration roll",
    "roll_number": "Class roll (old name, kept so saved templates keep working)",
    "batch": "Batch, e.g. 2022-26",
    "department": "Department name",
    "institute": "Institute name",
    "guardian_name": "Guardian's name (falls back to 'Guardian')",
    "student_email": "Student's email address",
    "student_mobile": "Student's own WhatsApp number",
    "percentage": "Attendance percentage, e.g. 62.5",
    "threshold": "The threshold used for this alert",
    "shortfall": "How many points below the threshold",
    "held": "Classes conducted in the range",
    "attended": "Classes the student attended",
    "missed": "Classes the student missed",
    "subject_code": "Subject code (subject-specific alerts)",
    "subject_name": "Subject name (subject-specific alerts)",
    "subject_list": "Per-subject breakdown, one line each (overall alerts)",
    "from_date": "Start of the reporting range",
    "to_date": "End of the reporting range",
    "sender_name": "Name of the person sending",
    "sender_role": "Role of the person sending",
}

# --------------------------------------------------------------------------- #
#  Defaults
# --------------------------------------------------------------------------- #
DEFAULT_EMAIL_SUBJECT_OVERALL = (
    "Attendance alert: you are at {{percentage}}% (below {{threshold}}%)"
)

DEFAULT_EMAIL_BODY_OVERALL = """Dear {{student_name}},

This is a formal notice from {{institute}} regarding your attendance.

Between {{from_date}} and {{to_date}} your overall attendance stands at \
{{percentage}}%, which is below the required {{threshold}}%. You have attended \
{{attended}} of {{held}} classes and missed {{missed}}.

Subject-wise breakdown:
{{subject_list}}

Please make sure you attend all upcoming classes. If there is a medical or \
personal reason for your absence, contact your department office as soon as \
possible so it can be recorded.

Regards,
{{sender_name}}
{{sender_role}}, {{institute}}
"""

DEFAULT_EMAIL_SUBJECT_SUBJECT = (
    "Attendance alert in {{subject_code}}: {{percentage}}% (below {{threshold}}%)"
)

DEFAULT_EMAIL_BODY_SUBJECT = """Dear {{student_name}},

Your attendance in {{subject_code}} — {{subject_name}} has fallen below the \
required level.

Between {{from_date}} and {{to_date}} you attended {{attended}} of {{held}} \
classes in this subject, which is {{percentage}}% against a required \
{{threshold}}% — a shortfall of {{shortfall}} percentage points.

Please attend all remaining classes in this subject. If there is a medical or \
personal reason for your absence, contact your department office as soon as \
possible so it can be recorded.

Regards,
{{sender_name}}
{{sender_role}}, {{institute}}
"""

# --- WhatsApp to the student themselves -------------------------------------- #
# Addresses the student directly, unlike the guardian version below.
DEFAULT_STUDENT_WHATSAPP_OVERALL = """*{{institute}} — Attendance Alert*

Hi {{first_name}},

Your overall attendance is *{{percentage}}%*, below the required \
{{threshold}}% — a shortfall of {{shortfall}} points.

Classes held: {{held}}
Attended: {{attended}}
Missed: {{missed}}
Period: {{from_date}} to {{to_date}}

Please attend all upcoming classes. If you have a medical or personal reason for \
being absent, contact the {{department}} department office so it can be recorded.

- {{sender_name}}, {{institute}}"""

DEFAULT_STUDENT_WHATSAPP_SUBJECT = """*{{institute}} — Attendance Alert*

Hi {{first_name}},

Your attendance in *{{subject_code}} — {{subject_name}}* is *{{percentage}}%*, \
below the required {{threshold}}%.

Classes held: {{held}}
Attended: {{attended}}
Missed: {{missed}}
Period: {{from_date}} to {{to_date}}

Please attend all remaining classes in this subject. If you have a medical or \
personal reason for being absent, contact the {{department}} department office.

- {{sender_name}}, {{institute}}"""

# WhatsApp bodies stay short — long messages get truncated in previews and are
# unpleasant to read on a phone.
DEFAULT_WHATSAPP_OVERALL = """*{{institute}} — Attendance Alert*

Dear {{guardian_name}},

Your ward *{{student_name}}* ({{class_roll}}, {{batch}}) has an overall \
attendance of *{{percentage}}%*, which is below the required {{threshold}}%.

Classes held: {{held}}
Attended: {{attended}}
Missed: {{missed}}
Period: {{from_date}} to {{to_date}}

Please encourage regular attendance. For any concern, contact the \
{{department}} department office.

- {{sender_name}}, {{institute}}"""

DEFAULT_WHATSAPP_SUBJECT = """*{{institute}} — Attendance Alert*

Dear {{guardian_name}},

Your ward *{{student_name}}* ({{class_roll}}, {{batch}}) has *{{percentage}}%* \
attendance in *{{subject_code}} — {{subject_name}}*, below the required \
{{threshold}}%.

Classes held: {{held}}
Attended: {{attended}}
Missed: {{missed}}
Period: {{from_date}} to {{to_date}}

Please encourage regular attendance. For any concern, contact the \
{{department}} department office.

- {{sender_name}}, {{institute}}"""


def defaults_for(scope):
    """Return the starting templates for 'OVERALL' or 'SUBJECT' alerts."""
    if scope == "SUBJECT":
        return {
            "email_subject": DEFAULT_EMAIL_SUBJECT_SUBJECT,
            "email_body": DEFAULT_EMAIL_BODY_SUBJECT,
            "student_whatsapp_body": DEFAULT_STUDENT_WHATSAPP_SUBJECT,
            "whatsapp_body": DEFAULT_WHATSAPP_SUBJECT,
        }
    return {
        "email_subject": DEFAULT_EMAIL_SUBJECT_OVERALL,
        "email_body": DEFAULT_EMAIL_BODY_OVERALL,
        "student_whatsapp_body": DEFAULT_STUDENT_WHATSAPP_OVERALL,
        "whatsapp_body": DEFAULT_WHATSAPP_OVERALL,
    }


# --------------------------------------------------------------------------- #
#  Rendering
# --------------------------------------------------------------------------- #
def render(text, context):
    """
    Replace every ``{{placeholder}}`` we recognise; leave the rest untouched.

    Unknown placeholders survive verbatim on purpose — a visible ``{{studnet}}``
    in the preview is how the sender spots their typo before it reaches a parent.
    """
    def swap(match):
        key = match.group(1)
        if key in context:
            value = context[key]
            return "" if value is None else str(value)
        return match.group(0)

    return PLACEHOLDER_RE.sub(swap, text or "")


def unknown_placeholders(*texts):
    """Placeholders used in the drafts that we cannot fill — surfaced as warnings."""
    found = set()
    for text in texts:
        found.update(PLACEHOLDER_RE.findall(text or ""))
    return sorted(found - set(PLACEHOLDERS))
