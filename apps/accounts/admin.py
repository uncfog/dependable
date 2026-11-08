from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from apps.core.admin import TenantScopedAdminMixin

from .models import OwnershipTransfer, User


@admin.register(User)
class UserAdmin(TenantScopedAdminMixin, DjangoUserAdmin):
    """Standard Django user admin plus the tenancy and role fields."""

    list_display = (
        "username", "get_full_name", "email", "school", "branch", "role",
        "job_title", "is_active",
    )
    list_filter = ("role", "school", "branch", "is_active", "is_staff", "is_superuser")
    search_fields = ("username", "first_name", "last_name", "email", "job_title")
    autocomplete_fields = ("school", "branch")
    ordering = ("last_name", "first_name", "username")

    fieldsets = (
        (None, {"fields": ("username", "password", "must_change_password")}),
        ("Personal info", {"fields": ("first_name", "last_name", "email", "phone")}),
        (
            "Tenancy & access",
            {
                "fields": ("school", "branch", "role", "job_title"),
                "description": (
                    "<b>Permission role</b> controls what this account can see. "
                    "<b>Job title</b> is descriptive only and grants nothing."
                ),
            },
        ),
        (
            "Django permissions",
            {
                "fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions"),
                "classes": ("collapse",),
            },
        ),
        ("Important dates", {"fields": ("last_login", "date_joined"), "classes": ("collapse",)}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": (
                    "username", "email", "password1", "password2",
                    "school", "branch", "role", "job_title",
                ),
            },
        ),
    )

    @admin.display(description="Name", ordering="last_name")
    def get_full_name(self, obj):
        return obj.get_full_name()


@admin.register(OwnershipTransfer)
class OwnershipTransferAdmin(admin.ModelAdmin):
    """Read only. A transfer is started from Account settings or the
    ``transfer_school_ownership`` command, and decided from its emailed link."""

    list_display = ("school", "to_email", "status", "started_by", "created_at", "expires_at")
    list_filter = ("status",)
    search_fields = ("school__name", "to_email", "to_name")
    # Never the token hash: it is not secret on its own, but nobody needs it here.
    fields = ("school", "started_by", "to_name", "to_email", "to_user", "afterwards",
              "stay_branch", "status", "created_at", "expires_at", "decided_at")
    readonly_fields = fields

    def get_queryset(self, request):
        return OwnershipTransfer.all_objects.select_related("school", "started_by", "to_user")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
