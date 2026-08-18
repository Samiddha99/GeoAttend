from django.urls import path

from . import catalogue_views, views

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
    # The university's own catalogue — a different layer from the institute's
    # departments above, so a separate module and a separate URL space.
    path("catalogue/departments/", catalogue_views.departments_page,
         name="catalogue_departments"),
    path("api/catalogue/departments/", catalogue_views.api_departments,
         name="api_catalogue_departments"),
    path("api/catalogue/departments/save/", catalogue_views.api_department_save,
         name="api_catalogue_department_create"),
    path("api/catalogue/departments/<oid:pk>/save/",
         catalogue_views.api_department_save, name="api_catalogue_department_save"),
    path("api/catalogue/departments/<oid:pk>/toggle/",
         catalogue_views.api_department_toggle,
         name="api_catalogue_department_toggle"),
    path("api/catalogue/departments/<oid:pk>/delete/",
         catalogue_views.api_department_delete,
         name="api_catalogue_department_delete"),
    path("catalogue/batches/", catalogue_views.batches_page,
         name="catalogue_batches"),
    path("api/catalogue/batches/", catalogue_views.api_batches,
         name="api_catalogue_batches"),
    path("api/catalogue/batches/save/", catalogue_views.api_batch_save,
         name="api_catalogue_batch_create"),
    path("api/catalogue/batches/<oid:pk>/save/", catalogue_views.api_batch_save,
         name="api_catalogue_batch_save"),
    path("api/catalogue/batches/<oid:pk>/toggle/",
         catalogue_views.api_batch_toggle, name="api_catalogue_batch_toggle"),
    path("api/catalogue/batches/<oid:pk>/delete/",
         catalogue_views.api_batch_delete, name="api_catalogue_batch_delete"),
    path("catalogue/subjects/", catalogue_views.subjects_page,
         name="catalogue_subjects"),
    path("api/catalogue/subjects/", catalogue_views.api_subjects,
         name="api_catalogue_subjects"),
    path("api/catalogue/subjects/save/", catalogue_views.api_subject_save,
         name="api_catalogue_subject_create"),
    path("api/catalogue/subjects/<oid:pk>/save/",
         catalogue_views.api_subject_save, name="api_catalogue_subject_save"),
    path("api/catalogue/subjects/<oid:pk>/toggle/",
         catalogue_views.api_subject_toggle,
         name="api_catalogue_subject_toggle"),
    path("api/catalogue/subjects/<oid:pk>/delete/",
         catalogue_views.api_subject_delete,
         name="api_catalogue_subject_delete"),
    path("api/departments/options/", views.api_department_options,
         name="api_department_options"),
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
    # Suspension — a university's decision about a person, not an edit. See
    # accounts/suspension.py for who may take it and who may undo it.
    path("api/teachers/<oid:pk>/suspension/", views.api_teacher_suspend,
         name="api_teacher_suspend"),
    path("api/teachers/<oid:pk>/suspension/lift/",
         views.api_teacher_lift_suspension, name="api_teacher_lift_suspension"),

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
    path("api/students/<oid:pk>/reset-face/", views.api_student_reset_face,
         name="api_student_reset_face"),
    path("api/students/<oid:pk>/face/", views.api_student_face,
         name="api_student_face"),
    path("api/students/<oid:pk>/face/<str:pose>/", views.api_student_face_image,
         name="api_student_face_image"),
    path("api/imports/", views.api_import_jobs, name="api_import_jobs"),
    path("api/imports/<oid:pk>/", views.api_import_job_detail, name="api_import_job_detail"),
]
