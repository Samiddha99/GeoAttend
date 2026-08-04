from django.urls import path

from . import views

app_name = "notifications"

urlpatterns = [
    path("", views.alerts_page, name="alerts"),
    path("api/defaults/", views.api_defaults, name="api_defaults"),
    path("api/recipients/", views.api_recipients, name="api_recipients"),
    path("api/preview/", views.api_preview, name="api_preview"),
    path("api/send/", views.api_send, name="api_send"),
    path("api/campaigns/", views.api_campaigns, name="api_campaigns"),
    path("api/campaigns/<oid:pk>/", views.api_campaign_detail, name="api_campaign_detail"),
    path("api/whatsapp-test/", views.api_whatsapp_test, name="api_whatsapp_test"),

    # WhatsApp templates (head only)
    path("templates/", views.templates_page, name="templates"),
    path("api/templates/", views.api_templates, name="api_templates"),
    path("api/templates/create/", views.api_template_create, name="api_template_create"),
    path("api/templates/sync/", views.api_template_sync, name="api_templates_sync"),
    path("api/templates/<oid:pk>/sync/", views.api_template_sync, name="api_template_sync"),
    path("api/templates/<oid:pk>/resubmit/", views.api_template_resubmit,
         name="api_template_resubmit"),
    path("api/templates/<oid:pk>/preview/", views.api_template_preview,
         name="api_template_preview"),
    path("api/templates/<oid:pk>/delete/", views.api_template_delete,
         name="api_template_delete"),
]
