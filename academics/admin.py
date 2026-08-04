from django.contrib import admin

from .models import (
    Batch,
    Department,
    Enrollment,
    ImportJob,
    StudentProfile,
    Subject,
    TeacherAssignment,
)


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "institute", "hod", "is_active")
    list_filter = ("institute", "is_active")
    search_fields = ("name", "code")


@admin.register(Batch)
class BatchAdmin(admin.ModelAdmin):
    list_display = ("label", "department", "start_year", "end_year", "is_active")
    list_filter = ("department", "is_active")


@admin.register(Subject)
class SubjectAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "department", "semester", "credits", "is_active")
    list_filter = ("department", "semester", "is_active")
    search_fields = ("code", "name")


@admin.register(TeacherAssignment)
class TeacherAssignmentAdmin(admin.ModelAdmin):
    list_display = ("teacher", "subject", "batch", "is_active")
    list_filter = ("batch", "subject", "is_active")


@admin.register(StudentProfile)
class StudentProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "class_roll", "exam_roll", "batch", "department",
                    "guardian_mobile", "is_active")
    list_filter = ("department", "batch", "is_active")
    search_fields = ("user__email", "user__full_name", "class_roll", "exam_roll", "guardian_mobile")


@admin.register(Enrollment)
class EnrollmentAdmin(admin.ModelAdmin):
    list_display = ("student", "subject", "is_active")
    list_filter = ("subject", "is_active")


@admin.register(ImportJob)
class ImportJobAdmin(admin.ModelAdmin):
    list_display = ("file_name", "department", "status", "total_rows",
                    "created_count", "updated_count", "error_count", "created_at")
    list_filter = ("status", "department")
