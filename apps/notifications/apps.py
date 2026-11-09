from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.notifications"
    label = "notifications"
    verbose_name = "Notifications"

    def ready(self):
        from django.db.models.signals import post_save

        from apps.audit.models import AuditEvent

        from .rules import handle

        post_save.connect(handle, sender=AuditEvent, dispatch_uid="notifications_from_audit")

        from django.contrib.auth.signals import user_logged_out

        from .push import forget_on_logout

        user_logged_out.connect(forget_on_logout, dispatch_uid="notifications_forget_push")
