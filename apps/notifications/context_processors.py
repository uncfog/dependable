"""The bell on every signed-in page: the unread count, and the newest notice.

``latest_notification`` is the id of the newest notification at the moment the
page was drawn. The page's live check (notifications:live) asks only for newer
ones, so opening a page never pops up something already in the bell.
One query for both.
"""

from django.db.models import Count, Max, Q

from .models import Notification


def unread_notifications(request) -> dict:
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {}
    totals = Notification.objects.for_user(user).aggregate(
        unread=Count("id", filter=Q(read_at__isnull=True)), latest=Max("id"),
    )
    return {
        "unread_notifications": totals["unread"],
        "latest_notification": totals["latest"] or 0,
    }
