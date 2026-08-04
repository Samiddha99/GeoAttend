from django.contrib import admin

from .models import AttendanceRecord, AttendanceSession, MarkAttempt


class RecordInline(admin.TabularInline):
    model = AttendanceRecord
    extra = 0
    readonly_fields = ("student", "status", "marked_at", "distance_m", "ip")


@admin.register(AttendanceSession)
class AttendanceSessionAdmin(admin.ModelAdmin):
    list_display = ("__str__", "teacher", "status", "expected_count",
                    "present_count", "radius_m", "expires_at")
    list_filter = ("status", "subject", "batch", "session_date")
    search_fields = ("token", "subject__code", "teacher__email")
    inlines = [RecordInline]


@admin.register(AttendanceRecord)
class AttendanceRecordAdmin(admin.ModelAdmin):
    list_display = ("student", "session", "status", "marked_at", "distance_m")
    list_filter = ("status", "session__subject", "session__batch")
    search_fields = ("student__user__email",)


@admin.register(MarkAttempt)
class MarkAttemptAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "session", "reason", "distance_m", "ip")
    list_filter = ("reason",)
