"""Web Push: each notification, also on the phone's or computer's notification centre.

Off until ``VAPID_PUBLIC_KEY``, ``VAPID_PRIVATE_KEY`` and ``VAPID_SUBJECT`` are all
set (``python manage.py vapid_keys`` makes a pair). Until then the bell works as
before and nothing is sent anywhere.

The owner chose full details on the lock screen: the push carries the same title
and text as the bell. Because staff share devices, signing out of a browser also
removes that browser's subscription (see ``forget_on_logout``), so the next person
at the desk is not shown the last one's notifications.

Sending is an HTTPS call to the browser's push service (Google, Mozilla, Apple or
Microsoft). It runs after the database commits, on a small background pool, so a
slow push service never slows down recording a payment. Addresses the push
service reports as gone (404 or 410) are deleted.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from django.conf import settings
from django.db import close_old_connections, transaction
from django.urls import reverse
from django.utils import timezone

log = logging.getLogger(__name__)

#: Push services that browsers actually use. A subscription naming any other host
#: is refused, so nobody can make the server send requests somewhere else.
PUSH_HOSTS = (
    "fcm.googleapis.com",
    "android.googleapis.com",
    "push.services.mozilla.com",
    "push.apple.com",
    "notify.windows.com",
)

#: How long a push service keeps trying a phone that is switched off.
TTL_SECONDS = 24 * 60 * 60
TIMEOUT_SECONDS = 5

_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="webpush")


def configured() -> bool:
    return bool(
        settings.WEBPUSH_VAPID_PUBLIC_KEY
        and settings.WEBPUSH_VAPID_PRIVATE_KEY
        and settings.WEBPUSH_VAPID_SUBJECT
    )


def allowed_endpoint(endpoint: str) -> bool:
    parts = urlsplit(endpoint or "")
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host or len(endpoint) > 500:
        return False
    return any(host == known or host.endswith("." + known) for known in PUSH_HOSTS)


def payload(notification) -> str:
    """What the device shows: the bell's own title and text, and where a tap goes."""
    return json.dumps({
        "title": notification.title,
        "body": notification.body,
        # Through 'open', so a tap also marks it read in the bell.
        "url": reverse("notifications:open", args=[notification.pk]),
        "tag": f"schoolcord-{notification.pk}",
    })


def _in_worker(function, *args):
    """Run on a pool thread, then let go of that thread's database connection.

    The cleanup lives here rather than in deliver(), so running deliver() on a
    request's or a test's own connection never closes that connection mid-transaction.
    """
    try:
        return function(*args)
    finally:
        close_old_connections()


def _run(function, *args):
    """Hand work to the background pool. Tests replace this to run it in place."""
    _POOL.submit(_in_worker, function, *args)


def deliver(notification_id: int) -> int:
    """Send one notification to every device its recipient turned on. Returns how many."""
    from pywebpush import WebPushException, webpush

    from .models import Notification, PushSubscription

    sent = 0
    notification = Notification.objects.filter(pk=notification_id).first()
    if notification is None or not configured():
        return 0
    data = payload(notification)
    for subscription in PushSubscription.objects.filter(user_id=notification.recipient_id):
        try:
            webpush(
                subscription_info={
                    "endpoint": subscription.endpoint,
                    "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
                },
                data=data,
                vapid_private_key=settings.WEBPUSH_VAPID_PRIVATE_KEY,
                vapid_claims={"sub": settings.WEBPUSH_VAPID_SUBJECT},
                ttl=TTL_SECONDS,
                timeout=TIMEOUT_SECONDS,
            )
        except WebPushException as error:
            status = getattr(getattr(error, "response", None), "status_code", None)
            if status in (404, 410):
                subscription.delete()  # the browser unsubscribed or was reset
            else:
                log.warning("webpush: %s to subscription %s failed: %s",
                            notification_id, subscription.pk, status or error)
            continue
        except Exception:  # noqa: BLE001 -- one bad device must not stop the others
            log.exception("webpush: %s to subscription %s failed",
                          notification_id, subscription.pk)
            continue
        PushSubscription.objects.filter(pk=subscription.pk).update(last_sent_at=timezone.now())
        sent += 1
    return sent


def schedule(notification) -> None:
    """Send it once the notification is saved for good. Nothing if push is off."""
    if not configured():
        return
    from .models import PushSubscription

    if not PushSubscription.objects.filter(user_id=notification.recipient_id).exists():
        return
    transaction.on_commit(lambda: _run(deliver, notification.pk))


SESSION_KEY = "push_endpoints"


def remember_in_session(request, endpoint: str) -> None:
    endpoints = [e for e in request.session.get(SESSION_KEY, []) if e != endpoint]
    request.session[SESSION_KEY] = (endpoints + [endpoint])[-5:]


def forget_on_logout(sender, request, user, **kwargs):
    """``user_logged_out``: this browser stops receiving the person's notifications."""
    from .models import PushSubscription

    if request is None or user is None:
        return
    endpoints = request.session.get(SESSION_KEY, [])
    if endpoints:
        PushSubscription.objects.filter(user=user, endpoint__in=endpoints).delete()
