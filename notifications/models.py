from django.conf import settings
from django.db import models

from academics.models import StudentProfile, Subject
from accounts.models import Institute


class WhatsAppTemplate(models.Model):
    """
    A WhatsApp message template owned by an institute and approved by WhatsApp.

    WhatsApp refuses business-initiated free-form text, so every alert must go out
    as a template that Meta has approved in advance.  The head writes the wording
    here using the project's ``{{placeholder}}`` names; on submission it is
    converted to WhatsApp's numbered ``{{1}}``/``{{2}}`` form and pushed to
    Twilio's Content API.  ``variable_order`` remembers which placeholder owns
    which slot so the numbers can be refilled per recipient at send time.
    """

    class Audience(models.TextChoices):
        STUDENT = "STUDENT", "WhatsApp to student"
        GUARDIAN = "GUARDIAN", "WhatsApp to guardian"

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"                 # saved here, not yet at Twilio
        RECEIVED = "RECEIVED", "Received"        # Twilio has it, queued for Meta
        PENDING = "PENDING", "Pending review"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"
        PAUSED = "PAUSED", "Paused"
        DISABLED = "DISABLED", "Disabled"
        FAILED = "FAILED", "Submission failed"   # never reached Twilio

    #: Statuses that mean "usable for sending right now".
    SENDABLE = {Status.APPROVED}

    institute = models.ForeignKey(
        Institute, on_delete=models.CASCADE, related_name="whatsapp_templates")
    audience = models.CharField(max_length=10, choices=Audience.choices)
    name = models.CharField(max_length=120, help_text="Shown to staff when picking a template.")
    twilio_name = models.SlugField(
        max_length=120, help_text="Lowercase letters, digits and underscores only.")
    language = models.CharField(max_length=10, default="en")
    category = models.CharField(max_length=20, default="UTILITY")

    body = models.TextField(help_text="Uses {{placeholder}} names, not numbers.")
    variable_order = models.JSONField(
        default=list, blank=True,
        help_text="Placeholder names in slot order: index 0 fills {{1}}.")

    content_sid = models.CharField(max_length=40, blank=True, db_index=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    rejection_reason = models.CharField(max_length=400, blank=True)
    last_error = models.CharField(max_length=400, blank=True)

    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name="whatsapp_templates")
    created_at = models.DateTimeField(auto_now_add=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["audience", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["institute", "twilio_name"], name="uniq_wa_template_name"),
        ]

    def __str__(self):
        return f"{self.name} ({self.get_audience_display()}) · {self.status}"

    @property
    def is_sendable(self):
        return self.is_active and self.status in self.SENDABLE and bool(self.content_sid)

    @property
    def is_editable(self):
        """Only a draft or a failed submission may still be changed."""
        return self.status in (self.Status.DRAFT, self.Status.FAILED)

    def preview(self, context):
        """Render the staff-facing body with a real recipient's values."""
        from . import message_templates as mt

        return mt.render(self.body, context)

    def content_variables(self, context):
        """{'1': value, '2': value, …} in the order WhatsApp approved."""
        return {
            str(i): str(context.get(name, ""))
            for i, name in enumerate(self.variable_order, start=1)
        }


class AlertCampaign(models.Model):
    """
    One "send low-attendance alerts" action: who sent it, to whom, with what
    wording and what came back from each channel.
    """

    class Scope(models.TextChoices):
        OVERALL = "OVERALL", "Overall attendance"
        SUBJECT = "SUBJECT", "Subject-specific"

    institute = models.ForeignKey(Institute, on_delete=models.CASCADE, related_name="campaigns")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="campaigns"
    )
    scope = models.CharField(max_length=10, choices=Scope.choices, default=Scope.OVERALL)
    subject = models.ForeignKey(
        Subject, on_delete=models.SET_NULL, null=True, blank=True, related_name="campaigns"
    )
    threshold = models.FloatField()
    date_from = models.DateField()
    date_to = models.DateField()

    email_students = models.BooleanField(default=True)
    whatsapp_students = models.BooleanField(default=False)
    whatsapp_guardians = models.BooleanField(default=True)

    email_subject = models.CharField(max_length=250, blank=True)
    email_body = models.TextField(blank=True)
    student_whatsapp_body = models.TextField(blank=True)
    whatsapp_body = models.TextField(blank=True, help_text="Sent to the guardian.")
    # WhatsApp requires an approved template; these record which one was used.
    student_template = models.ForeignKey(
        "WhatsAppTemplate", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="student_campaigns")
    guardian_template = models.ForeignKey(
        "WhatsAppTemplate", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="guardian_campaigns")

    total_recipients = models.PositiveIntegerField(default=0)
    email_sent = models.PositiveIntegerField(default=0)
    email_failed = models.PositiveIntegerField(default=0)
    student_whatsapp_sent = models.PositiveIntegerField(default=0)
    student_whatsapp_failed = models.PositiveIntegerField(default=0)
    whatsapp_sent = models.PositiveIntegerField(default=0)
    whatsapp_failed = models.PositiveIntegerField(default=0)
    skipped = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        label = self.subject.code if self.subject else "overall"
        return f"{label} < {self.threshold}% · {self.created_at:%d %b %Y %H:%M}"

    @property
    def channel_label(self):
        parts = []
        if self.email_students:
            parts.append("Email")
        if self.whatsapp_students:
            parts.append("WA→student")
        if self.whatsapp_guardians:
            parts.append("WA→guardian")
        return " + ".join(parts) or "—"

    @property
    def sent_total(self):
        return self.email_sent + self.student_whatsapp_sent + self.whatsapp_sent

    @property
    def failed_total(self):
        return self.email_failed + self.student_whatsapp_failed + self.whatsapp_failed


class AlertDelivery(models.Model):
    """One message to one person on one channel — the audit trail."""

    class Channel(models.TextChoices):
        EMAIL = "EMAIL", "Email to student"
        # "WHATSAPP" predates the student channel and has always meant the
        # guardian; kept as-is so historical rows stay meaningful.
        WHATSAPP = "WHATSAPP", "WhatsApp to guardian"
        WHATSAPP_STUDENT = "WA_STUDENT", "WhatsApp to student"

    class Status(models.TextChoices):
        SENT = "SENT", "Sent"
        FAILED = "FAILED", "Failed"
        SKIPPED = "SKIPPED", "Skipped"

    campaign = models.ForeignKey(
        AlertCampaign, on_delete=models.CASCADE, related_name="deliveries"
    )
    student = models.ForeignKey(
        StudentProfile, on_delete=models.CASCADE, related_name="alert_deliveries"
    )
    channel = models.CharField(max_length=10, choices=Channel.choices)
    target = models.CharField(max_length=150, help_text="Email address or phone number")
    percentage = models.FloatField(default=0)
    subject_line = models.CharField(max_length=250, blank=True)
    body = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=Status.choices)
    error = models.CharField(max_length=300, blank=True)
    provider_id = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["student__class_roll", "channel"]
        indexes = [models.Index(fields=["campaign", "status"])]

    def __str__(self):
        return f"{self.student} · {self.channel} · {self.status}"
