"""Who is told, and the one place a notification is written."""

from __future__ import annotations

from collections.abc import Iterable

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction

from apps.core.roles import Role

from .models import Notification, NotificationPreference

User = get_user_model()


def owners(school_id) -> list:
    """The school's active owners."""
    if not school_id:
        return []
    return list(User.objects.filter(
        school_id=school_id, role=Role.SCHOOL_OWNER, is_active=True
    ))


def campus_staff(school_id, branch_id, roles=(Role.PRINCIPAL, Role.BURSAR)) -> list:
    """The active principals and bursars of one campus."""
    if not school_id or not branch_id:
        return []
    return list(User.objects.filter(
        school_id=school_id, branch_id=branch_id, role__in=roles, is_active=True
    ))


def notify(recipients: Iterable, *, kind: str, title: str, body: str = "", url: str = "",
           school_id=None, branch_id=None, event=None, dedupe_key: str = "",
           exclude=None) -> list[Notification]:
    """Write one notification for each person, once.

    ``exclude`` is the person who caused it: nobody is told about their own
    action. A recipient who turned this kind off, or whose account is off, is
    skipped. With a ``dedupe_key``, a person who already has that key is skipped
    too, so a reminder run twice in a day tells nobody twice.
    """
    seen: set[int] = set()
    people = []
    for person in recipients:
        if person is None or person.pk in seen or not person.is_active:
            continue
        if exclude is not None and person.pk == getattr(exclude, "pk", exclude):
            continue
        seen.add(person.pk)
        people.append(person)
    if not people:
        return []

    muted = {
        pref.user_id: pref
        for pref in NotificationPreference.objects.filter(user_id__in=seen)
    }
    written = []
    for person in people:
        pref = muted.get(person.pk)
        if pref is not None and pref.is_muted(kind):
            continue
        if dedupe_key and Notification.objects.filter(
            recipient=person, dedupe_key=dedupe_key
        ).exists():
            continue
        try:
            with transaction.atomic():
                written.append(Notification.objects.create(
                    recipient=person, kind=kind, title=title[:160], body=body[:300],
                    url=url[:300], school_id=school_id, branch_id=branch_id,
                    event_id=getattr(event, "pk", None), dedupe_key=dedupe_key,
                ))
        except IntegrityError:
            # Another run wrote the same reminder first. That is the point of the key.
            continue
    from .push import schedule

    for notification in written:
        schedule(notification)
    return written
