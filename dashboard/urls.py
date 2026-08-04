from django.urls import path

from . import views

app_name = "dashboard"

urlpatterns = [
    path("", views.home, name="home"),
    path("reports/", views.reports_page, name="reports"),
    path("students/<oid:pk>/", views.student_detail_page, name="student_detail"),

    path("api/summary/", views.api_summary, name="api_summary"),
    path("api/trend/", views.api_trend, name="api_trend"),
    path("api/report/students/", views.api_students_report, name="api_students_report"),
    path("api/report/subjects/", views.api_subjects_report, name="api_subjects_report"),
    path("api/report/batches/", views.api_batches_report, name="api_batches_report"),
    path("api/report/departments/", views.api_departments_report, name="api_departments_report"),
    path("api/report/teachers/", views.api_teachers_report, name="api_teachers_report"),
    path("api/report/distribution/", views.api_distribution, name="api_distribution"),
    path("api/report/low/", views.api_low_attendance, name="api_low_attendance"),
    path("api/student/<oid:pk>/", views.api_student_detail, name="api_student_detail"),
    path("api/me/", views.api_my_summary, name="api_my_summary"),

    path("export/students/", views.export_students, name="export_students"),
    path("export/subjects/", views.export_subjects, name="export_subjects"),
]
