from django.urls import path

from . import views

app_name = "feedback"

urlpatterns = [
    # student
    path("my/", views.my_feedback_page, name="my_feedback"),
    path("api/my/", views.api_my_feedback, name="api_my_feedback"),
    path("api/forms/<oid:pk>/", views.api_form, name="api_form"),
    path("api/forms/<oid:pk>/submit/", views.api_submit, name="api_submit"),

    # staff
    path("", views.feedback_page, name="feedback"),
    path("api/list/", views.api_forms, name="api_forms"),
    path("api/list/<oid:pk>/", views.api_form_detail, name="api_form_detail"),
    path("api/groups/<str:kind>/", views.api_groups, name="api_groups"),
    path("api/groups/<str:kind>/<oid:pk>/", views.api_group_detail,
         name="api_group_detail"),

    # sending, from a class session
    path("api/sessions/<oid:pk>/send/", views.api_send, name="api_send"),
]
