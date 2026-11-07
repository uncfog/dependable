from django.contrib import admin

from apps.core.admin import TenantScopedAdminMixin

from .models import Family, FamilyMembership


@admin.register(Family)
class FamilyAdmin(TenantScopedAdminMixin):
    tenant_branch_field = None
    list_display = ("name", "family_code", "school", "contact_phone")
    list_filter = ("school",)
    search_fields = ("name", "family_code", "contact_phone")


@admin.register(FamilyMembership)
class FamilyMembershipAdmin(TenantScopedAdminMixin):
    """A record to read. Links are made on the family screens, by the service's rules."""

    list_display = ("family", "student", "branch", "linked_by", "created_at", "ended_at")
    list_filter = ("branch",)
    search_fields = ("family__name", "student__admission_number", "student__last_name")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
