from django.urls import path

from . import views

app_name = "attendance"

urlpatterns = [
    # teacher
    path("generate/", views.generate_page, name="generate"),
    path("sessions/", views.sessions_page, name="sessions"),
    path("sessions/<oid:pk>/", views.session_detail_page, name="session_detail"),

    path("api/my-batches/", views.api_teacher_batches, name="api_my_batches"),
    path("api/batches/<oid:batch_id>/subjects/", views.api_batch_subjects, name="api_batch_subjects"),
    path("api/sessions/create/", views.api_session_create, name="api_session_create"),
    path("api/sessions/", views.api_sessions, name="api_sessions"),
    path("api/sessions/<oid:pk>/status/", views.api_session_status, name="api_session_status"),
    path("api/sessions/<oid:pk>/attempts/", views.api_session_attempts, name="api_session_attempts"),
    path("api/sessions/<oid:pk>/export/", views.api_session_export, name="api_session_export"),
    path("api/sessions/<oid:pk>/mark/", views.api_manual_mark, name="api_manual_mark"),
    # Must precede the <str:action> catch-all below, which would otherwise
    # match action="reason" and send a student to a teacher-only view.
    path("api/sessions/<oid:pk>/reason/", views.api_absence_reason_submit,
         name="api_absence_reason_submit"),
    path("api/sessions/<oid:pk>/<str:action>/", views.api_session_action, name="api_session_action"),

    # student
    path("mark/<str:token>/", views.mark_page, name="mark"),
    path("api/mark/<str:token>/", views.api_mark, name="api_mark"),
    # Runs the gates and hands back a ticket for the face-matching socket.
    path("api/mark/<str:token>/start/", views.api_mark_start, name="api_mark_start"),
    path("api/manual-requests/<oid:pk>/decide/", views.api_manual_request_decide,
         name="api_manual_request_decide"),
    path("me/", views.my_attendance_page, name="my_attendance"),

    # absence reasons
    path("reasons/", views.absence_reasons_page, name="absence_reasons"),
    path("my-reasons/", views.my_absence_reasons_page, name="my_absence_reasons"),
    path("api/reasons/", views.api_absence_reasons, name="api_absence_reasons"),
    path("api/reasons/<oid:pk>/review/", views.api_absence_reason_review,
         name="api_absence_reason_review"),

    # planned (future) absences
    path("api/planned/", views.api_planned_absences, name="api_planned_absences"),
    path("api/planned/submit/", views.api_planned_absence_submit,
         name="api_planned_absence_submit"),
    path("api/planned/<oid:pk>/cancel/", views.api_planned_absence_cancel,
         name="api_planned_absence_cancel"),
    path("api/planned/decisions/<oid:pk>/review/", views.api_planned_decision_review,
         name="api_planned_decision_review"),

    # evidence attached to either kind of request
    path("api/attachments/<oid:pk>/", views.api_attachment_download,
         name="api_attachment_download"),
    path("api/my-subjects/", views.api_my_subjects, name="api_my_subjects"),
]
