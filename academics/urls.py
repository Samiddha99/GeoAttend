from django.urls import path

from . import views

app_name = "academics"

urlpatterns = [
    # pages
    path("departments/", views.departments_page, name="departments"),
    path("subjects/", views.subjects_page, name="subjects"),
    path("batches/", views.batches_page, name="batches"),
    path("teachers/", views.teachers_page, name="teachers"),
    path("students/", views.students_page, name="students"),
    path("all-students/", views.all_students_page, name="all_students"),

    # lookups
    path("api/lookups/", views.api_lookups, name="api_lookups"),

    # departments
    path("api/departments/", views.api_departments, name="api_departments"),
    path("api/departments/save/", views.api_department_save, name="api_department_create"),
    path("api/departments/<oid:pk>/save/", views.api_department_save, name="api_department_save"),
    path("api/departments/<oid:pk>/delete/", views.api_department_delete, name="api_department_delete"),

    # invitations
    path("api/invitations/", views.api_invitations, name="api_invitations"),
    path("api/invitations/<oid:pk>/resend/", views.api_invitation_resend, name="api_invitation_resend"),
    path("api/invitations/<oid:pk>/revoke/", views.api_invitation_revoke, name="api_invitation_revoke"),

    # subjects
    path("api/subjects/", views.api_subjects, name="api_subjects"),
    path("api/subjects/save/", views.api_subject_save, name="api_subject_create"),
    path("api/subjects/<oid:pk>/save/", views.api_subject_save, name="api_subject_save"),
    path("api/subjects/<oid:pk>/delete/", views.api_subject_delete, name="api_subject_delete"),

    # batches
    path("api/batches/", views.api_batches, name="api_batches"),
    path("api/batches/save/", views.api_batch_save, name="api_batch_create"),
    path("api/batches/<oid:pk>/save/", views.api_batch_save, name="api_batch_save"),
    path("api/batches/<oid:pk>/toggle/", views.api_batch_toggle, name="api_batch_toggle"),
    path("api/batches/<oid:pk>/delete/", views.api_batch_delete, name="api_batch_delete"),

    # teachers
    path("api/teachers/", views.api_teachers, name="api_teachers"),
    path("api/teachers/invite/", views.api_teacher_invite, name="api_teacher_invite"),
    path("api/teachers/<oid:pk>/assignments/", views.api_teacher_assignments_save,
         name="api_teacher_assignments"),
    path("api/teachers/<oid:pk>/toggle/", views.api_teacher_toggle, name="api_teacher_toggle"),

    # students
    path("api/students/", views.api_students, name="api_students"),
    path("api/students/import/", views.api_students_import, name="api_students_import"),
    path("api/students/template/", views.api_students_template, name="api_students_template"),
    path("api/students/export/", views.api_students_export, name="api_students_export"),
    path("api/students/<oid:pk>/save/", views.api_student_save, name="api_student_save"),
    path("api/students/<oid:pk>/toggle/", views.api_student_toggle, name="api_student_toggle"),
    path("api/students/<oid:pk>/resend/", views.api_student_resend, name="api_student_resend"),
    path("api/students/<oid:pk>/reset-device/", views.api_student_reset_device,
         name="api_student_reset_device"),
    path("api/imports/", views.api_import_jobs, name="api_import_jobs"),
    path("api/imports/<oid:pk>/", views.api_import_job_detail, name="api_import_job_detail"),
]
