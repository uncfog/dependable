"""The whole log, for the platform. Read-only: no adding, changing or deleting."""

from django.contrib import admin

from apps.core.admin import TenantScopedAdminMixin

from .models import AuditEvent


@admin.register(AuditEvent)
class AuditEventAdmin(TenantScopedAdminMixin):
    list_display = ("created_at", "action", "target_label", "actor_name", "source", "school")
    list_filter = ("category", "action", "source", "school")
    search_fields = ("target_label", "target_id", "actor_name", "action")
    date_hierarchy = "created_at"
    ordering = ("-created_at", "-id")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
