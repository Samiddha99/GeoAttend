from django.contrib import admin

from .models import AlertCampaign, AlertDelivery, WhatsAppTemplate


class DeliveryInline(admin.TabularInline):
    model = AlertDelivery
    extra = 0
    readonly_fields = ("student", "channel", "target", "percentage", "status", "error")
    can_delete = False


@admin.register(AlertCampaign)
class AlertCampaignAdmin(admin.ModelAdmin):
    list_display = ("__str__", "institute", "created_by", "scope", "threshold",
                    "total_recipients", "email_sent", "whatsapp_sent", "created_at")
    list_filter = ("scope", "institute", "created_at")
    inlines = [DeliveryInline]


@admin.register(AlertDelivery)
class AlertDeliveryAdmin(admin.ModelAdmin):
    list_display = ("student", "channel", "target", "percentage", "status", "created_at")
    list_filter = ("channel", "status")
    search_fields = ("student__user__email", "target")


@admin.register(WhatsAppTemplate)
class WhatsAppTemplateAdmin(admin.ModelAdmin):
    """
    Approved wording, owned by an institute *or* a university.

    `owner` is a computed column because the two FKs are mutually exclusive —
    exactly one is set, enforced in the model rather than by a constraint
    (MongoDB has none). Showing both columns would leave one blank on every
    row; showing neither would hide who a template belongs to, which is the
    first thing anyone looks for.

    `content_sid` and `status` are read-only: they are WhatsApp's answers, not
    ours, and typing an approval in here would let the app try to send wording
    WhatsApp has never seen.
    """

    list_display = ("name", "owner_label", "audience", "status", "is_active",
                    "created_at", "last_synced_at")
    list_filter = ("audience", "status", "is_active", "category", "language")
    search_fields = ("name", "twilio_name", "body", "content_sid")
    date_hierarchy = "created_at"
    autocomplete_fields = ("institute", "university", "created_by")
    readonly_fields = ("content_sid", "status", "rejection_reason", "last_error",
                       "created_at", "submitted_at", "last_synced_at")
    fieldsets = (
        ("Owner", {
            "fields": ("institute", "university"),
            "description": "Exactly one of these. Both or neither is refused "
                           "when the row is saved.",
        }),
        ("Wording", {"fields": ("name", "twilio_name", "audience", "category",
                                "language", "body", "variable_order")}),
        ("WhatsApp", {
            "fields": ("content_sid", "status", "rejection_reason", "last_error",
                       "submitted_at", "last_synced_at"),
            "description": "Reported by WhatsApp. Read-only — editing these "
                           "would let the app send wording that was never "
                           "approved.",
        }),
        ("Status", {"fields": ("is_active", "created_by", "created_at")}),
    )

    @admin.display(description="Owner", ordering="institute__name")
    def owner_label(self, obj):
        return obj.owner or "—"
