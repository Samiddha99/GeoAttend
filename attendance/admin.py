from django.contrib import admin

from .models import (
    AbsenceAttachment,
    AbsenceReason,
    AttendanceRecord,
    AttendanceSession,
    FaceVerifyTicket,
    ManualMarkRequest,
    MarkAttempt,
    PlannedAbsence,
    PlannedAbsenceDecision,
)


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


# --------------------------------------------------------------------------- #
#  Absence requests
# --------------------------------------------------------------------------- #
class AttachmentInline(admin.TabularInline):
    """
    Evidence, shown against whichever request it belongs to.

    Declared twice below with a different `fk_name`, because an attachment
    hangs off either a per-class reason or a planned absence — never both. See
    AbsenceAttachment.clean().
    """

    model = AbsenceAttachment
    extra = 0
    fields = ("original_name", "content_type", "size_bytes", "file", "uploaded_at")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


class ReasonAttachmentInline(AttachmentInline):
    fk_name = "reason"


class PlannedAttachmentInline(AttachmentInline):
    fk_name = "planned"


@admin.register(AbsenceReason)
class AbsenceReasonAdmin(admin.ModelAdmin):
    list_display = ("student", "session", "status", "submitted_at",
                    "reviewed_by", "reviewed_at")
    list_filter = ("status", "session__subject__department__institute")
    search_fields = ("student__user__email", "student__user__full_name",
                     "reason", "review_remark")
    date_hierarchy = "submitted_at"
    autocomplete_fields = ("student", "session", "reviewed_by")
    inlines = [ReasonAttachmentInline]
    readonly_fields = ("submitted_at",)


class DecisionInline(admin.TabularInline):
    model = PlannedAbsenceDecision
    extra = 0
    autocomplete_fields = ("subject", "reviewed_by")


@admin.register(PlannedAbsence)
class PlannedAbsenceAdmin(admin.ModelAdmin):
    list_display = ("student", "from_date", "to_date", "days", "all_subjects",
                    "overall_status", "created_at", "cancelled_at")
    list_filter = ("all_subjects", "student__department__institute")
    search_fields = ("student__user__email", "student__user__full_name", "reason")
    date_hierarchy = "from_date"
    autocomplete_fields = ("student",)
    inlines = [DecisionInline, PlannedAttachmentInline]
    readonly_fields = ("created_at",)


@admin.register(PlannedAbsenceDecision)
class PlannedAbsenceDecisionAdmin(admin.ModelAdmin):
    list_display = ("planned", "subject", "status", "reviewed_by", "reviewed_at")
    list_filter = ("status", "subject__department")
    search_fields = ("planned__student__user__email", "subject__code",
                     "review_remark")
    autocomplete_fields = ("planned", "subject", "reviewed_by")


@admin.register(AbsenceAttachment)
class AbsenceAttachmentAdmin(admin.ModelAdmin):
    """
    Deletable but not editable.

    Deleting is a legitimate repair — evidence uploaded to the wrong request,
    or a file that should not have been kept. Editing the recorded size or
    sniffed content type by hand would only make the record disagree with the
    file it describes.
    """

    list_display = ("original_name", "parent", "content_type", "size_label",
                    "uploaded_at")
    list_filter = ("content_type",)
    search_fields = ("original_name",)
    readonly_fields = [f.name for f in AbsenceAttachment._meta.fields]

    @admin.display(description="Belongs to")
    def parent(self, obj):
        return obj.reason or obj.planned

    def has_add_permission(self, request):
        return False


# --------------------------------------------------------------------------- #
#  Face verification during marking
# --------------------------------------------------------------------------- #
@admin.register(FaceVerifyTicket)
class FaceVerifyTicketAdmin(admin.ModelAdmin):
    """
    One student's attempt to prove who they are, for one session.

    Read-only: every field is evidence about a marking attempt, and an
    editable ticket is one somebody could reuse. The token is deliberately
    absent from the list — it is a bearer credential while the ticket is live.
    """

    list_display = ("student", "session", "attempts", "distance_m", "used_at",
                    "created_at", "expires_at")
    list_filter = ("session__subject__department__institute",)
    search_fields = ("student__user__email", "student__user__full_name", "ip")
    date_hierarchy = "created_at"
    readonly_fields = [f.name for f in FaceVerifyTicket._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(ManualMarkRequest)
class ManualMarkRequestAdmin(admin.ModelAdmin):
    list_display = ("student", "session", "status", "attempts", "best_score",
                    "decided_by", "decided_at", "created_at")
    list_filter = ("status",)
    search_fields = ("student__user__email", "student__user__full_name", "reason")
    date_hierarchy = "created_at"
    autocomplete_fields = ("session", "student", "decided_by")
    # The snapshot is a base64 image and the ticket is a credential; neither
    # belongs in an edit form.
    exclude = ("snapshot", "ticket")
    readonly_fields = ("created_at", "decided_at")
