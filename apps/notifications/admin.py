from django.contrib import admin

from .models import Notification, Reminder


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    """Read-only: notifications are written by the app, never by hand."""

    list_display = ("title", "recipient", "kind", "created_at", "read_at")
    list_filter = ("kind",)
    search_fields = ("title", "recipient__username")
    readonly_fields = [f.name for f in Notification._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(Reminder)
class ReminderAdmin(admin.ModelAdmin):
    list_display = ("target_label", "user", "due_on", "sent_at")
    readonly_fields = [f.name for f in Reminder._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
