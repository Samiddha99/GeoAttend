from django.contrib import admin

from .models import AlertCampaign, AlertDelivery


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
