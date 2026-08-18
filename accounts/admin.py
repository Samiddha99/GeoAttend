from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.db import models

from .models import (
    ActivityLog,
    EmailOTP,
    FaceEnrolment,
    FaceSample,
    Institute,
    InstituteAffiliation,
    Invitation,
    PhoneOTP,
    University,
    UniversityDiscipline,
    User,
)


class ReadOnlyMixin:
    """
    Visible, never editable.

    For rows the application writes and nobody should hand-edit: one-time
    codes, face embeddings, audit trails. Admin exists here to *answer
    questions* — why did this sign-in fail, what did this student send — not to
    let someone reach in and change an OTP or a stored face vector.
    """

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


class UniversityDisciplineInline(admin.TabularInline):
    model = UniversityDiscipline
    extra = 0


@admin.register(University)
class UniversityAdmin(admin.ModelAdmin):
    list_display = ("name", "short_name", "code", "grants_affiliation",
                    "is_seeded", "claimed", "institute_count", "is_active")
    list_filter = ("grants_affiliation", "is_active", "is_seeded", "state")
    search_fields = ("name", "short_name", "code", "email")
    inlines = [UniversityDisciplineInline]
    readonly_fields = ("created_at", "claimed_at")

    @admin.display(boolean=True, description="Claimed")
    def claimed(self, obj):
        return obj.is_claimed

    @admin.display(description="Institutes")
    def institute_count(self, obj):
        # Affiliated *or* invited — the same two sets accounts.scoping uses,
        # so this number agrees with what the university sees when it signs in.
        return Institute.objects.filter(
            models.Q(affiliations__university=obj) | models.Q(invited_by=obj)
        ).distinct().count()


class InstituteAffiliationInline(admin.TabularInline):
    model = InstituteAffiliation
    extra = 0
    autocomplete_fields = ("university",)
    verbose_name_plural = "Affiliations (blank university = autonomous)"


@admin.register(Institute)
class InstituteAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "email", "state", "district", "status",
                    "invited_by", "is_active", "created_at")
    list_filter = ("status", "is_active", "state", "invited_by")
    search_fields = ("name", "code", "email")
    inlines = [InstituteAffiliationInline]
    readonly_fields = ("created_at", "decided_at", "decided_by")
    fieldsets = (
        (None, {"fields": ("name", "code", "email", "phone", "website", "address")}),
        ("Where", {"fields": ("state", "district")}),
        ("Approval", {
            "fields": ("status", "rejection_reason", "decided_at", "decided_by",
                       "invited_by"),
            "description": "A head cannot sign in until the status is Approved.",
        }),
        ("Status", {"fields": ("logo", "is_active", "created_at")}),
    )


@admin.register(UniversityDiscipline)
class UniversityDisciplineAdmin(admin.ModelAdmin):
    """
    Also an inline on University, and worth its own screen: the question people
    actually ask is "which bodies grant Pharmacy affiliation", which is a
    filter on this table rather than a scan of the other one.
    """

    list_display = ("university", "discipline")
    list_filter = ("discipline", "university__grants_affiliation")
    search_fields = ("university__name", "university__short_name")
    autocomplete_fields = ("university",)


@admin.register(InstituteAffiliation)
class InstituteAffiliationAdmin(admin.ModelAdmin):
    list_display = ("institute", "discipline", "university", "created_at")
    list_filter = ("discipline", "university")
    search_fields = ("institute__name", "university__name")
    autocomplete_fields = ("institute", "university")


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    ordering = ("email",)
    list_display = ("email", "full_name", "role", "institute", "university",
                    "department", "registration_completed", "is_active")
    list_filter = ("role", "is_active", "registration_completed", "institute",
                   "university", "department")
    search_fields = ("email", "full_name", "guardian_mobile")
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Profile", {"fields": ("full_name", "phone", "role", "institute",
                                "university", "department")}),
        ("Guardian", {
            "fields": ("guardian_mobile",),
            "description": "Set only on guardian accounts — this number is the "
                           "login, so changing it hands the account to whoever "
                           "owns the new number.",
        }),
        ("Device", {"fields": ("device_id", "device_bound_at")}),
        ("Status", {"fields": ("is_active", "is_staff", "is_superuser",
                               "email_verified", "registration_completed")}),
        ("Permissions", {"fields": ("groups", "user_permissions")}),
        ("Dates", {"fields": ("last_login", "date_joined", "last_seen")}),
    )
    add_fieldsets = (
        (None, {
            "classes": ("wide",),
            "fields": ("email", "full_name", "role", "institute", "password1", "password2"),
        }),
    )


@admin.register(Invitation)
class InvitationAdmin(admin.ModelAdmin):
    list_display = ("email", "role", "institute", "department", "status", "created_at", "expires_at")
    list_filter = ("role", "status", "institute")
    search_fields = ("email",)


@admin.register(EmailOTP)
class EmailOTPAdmin(admin.ModelAdmin):
    list_display = ("email", "purpose", "is_used", "attempts", "created_at", "expires_at")
    list_filter = ("purpose", "is_used")
    search_fields = ("email",)


@admin.register(ActivityLog)
class ActivityLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "actor", "action", "detail", "ip")
    list_filter = ("action",)
    search_fields = ("detail", "actor__email")


@admin.register(PhoneOTP)
class PhoneOTPAdmin(ReadOnlyMixin, admin.ModelAdmin):
    """
    Guardian sign-in codes.

    Read-only, and the code itself is never shown — only its hash is stored, so
    there is nothing here that would let someone sign in as a guardian. What it
    does answer is "why did this number fail": attempts used, sends used,
    whether it expired.
    """

    list_display = ("mobile", "purpose", "attempts", "sends", "is_used",
                    "created_at", "expires_at")
    list_filter = ("purpose", "is_used")
    search_fields = ("mobile",)
    readonly_fields = [f.name for f in PhoneOTP._meta.fields]


class FaceSampleInline(admin.TabularInline):
    model = FaceSample
    extra = 0
    # The embedding is a 512-float vector: useless on screen and enormous.
    fields = ("pose", "image", "yaw", "detect_score", "captured_at")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(FaceEnrolment)
class FaceEnrolmentAdmin(admin.ModelAdmin):
    """
    Deletable but not editable.

    Deleting is the supported repair — it is how staff clear a bad enrolment so
    a student can capture again. Editing the stored vectors by hand is not a
    thing anyone should do, so the fields are read-only.
    """

    list_display = ("user", "model_name", "created_at", "reset_at", "reset_by")
    list_filter = ("model_name",)
    search_fields = ("user__email", "user__full_name")
    inlines = [FaceSampleInline]
    readonly_fields = [f.name for f in FaceEnrolment._meta.fields]

    def has_add_permission(self, request):
        return False


@admin.register(FaceSample)
class FaceSampleAdmin(ReadOnlyMixin, admin.ModelAdmin):
    list_display = ("enrolment", "pose", "yaw", "detect_score", "captured_at")
    list_filter = ("pose",)
    search_fields = ("enrolment__user__email",)
    readonly_fields = [f.name for f in FaceSample._meta.fields]
