from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import ActivityLog, EmailOTP, Institute, Invitation, User


@admin.register(Institute)
class InstituteAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "email", "is_active", "created_at")
    search_fields = ("name", "code", "email")


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    ordering = ("email",)
    list_display = ("email", "full_name", "role", "institute", "department",
                    "registration_completed", "is_active")
    list_filter = ("role", "is_active", "registration_completed", "institute", "department")
    search_fields = ("email", "full_name")
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Profile", {"fields": ("full_name", "phone", "role", "institute", "department")}),
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
