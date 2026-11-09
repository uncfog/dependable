"""The unread count on the bell, on every signed-in page."""

from .models import Notification


def unread_notifications(request) -> dict:
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {}
    return {"unread_notifications": Notification.objects.for_user(user).unread().count()}
