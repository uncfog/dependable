"""A principal or bursar handing their own position to someone new, by email.

The steps, each one a function here so the screens cannot disagree about a rule:

1. ``request_handover``  the person names their successor and confirms a password.
2. ``approve`` / ``reject``  the school's owner decides. Nothing is emailed to the
   newcomer until the owner approves.
3. ``accept`` / ``decline``  the newcomer opens a single-use link and chooses a
   name and password. They get the same role at the same campus; the old
   account is switched off in the same transaction.

The rules, and why:

* **Only your own position, and nothing above it.** The role and campus are
  copied from the request, which copied them from the person asking. A bursar
  can only ever produce another bursar at their campus.
* **Principals and bursars only.** The owner hands the school over through
  :mod:`apps.accounts.ownership`; platform accounts belong to no school.
* **The owner decides who works at the school.** That is already true of every
  staff account (``MANAGE_STAFF``), so a handover waits for the owner.
* **A new person, not an existing account.** An email that already signs in
  anywhere is refused: moving an existing account between roles is the owner's
  job on the Staff screen, where they can see what it changes.
* **The link is the only key.** 256 random bits, stored as a SHA-256, usable
  once, dead after ``LINK_TTL``.
"""

from __future__ import annotations

import logging
import secrets
from datetime import timedelta
from urllib.parse import urlsplit

from django.contrib.auth import get_user_model
from django.core.mail import EmailMultiAlternatives
from django.db import IntegrityError, transaction
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from apps.audit.models import Category
from apps.audit.recording import record
from apps.core.branding import branding
from apps.core.roles import Role

from .invites import unique_username
from .models import RoleHandover
from .ownership import NewAccount, _origin, hash_token

logger = logging.getLogger(__name__)

LINK_TTL = timedelta(hours=72)
#: Requests one person may make in a day.
DAILY_LIMIT = 3
HANDOVER_ROLES = (Role.PRINCIPAL, Role.BURSAR)

Status = RoleHandover.Status


class HandoverError(Exception):
    """A handover cannot go ahead. The message is written to be shown as is."""


def may_hand_over(user) -> bool:
    return bool(
        user.is_authenticated and user.is_active and user.role in HANDOVER_ROLES
        and user.school_id and user.branch_id
    )


def owners(school_id):
    return get_user_model().objects.filter(
        school_id=school_id, role=Role.SCHOOL_OWNER, is_active=True
    )


def check_recipient(email: str, *, asker) -> str:
    email = (email or "").strip()
    if asker.email and asker.email.lower() == email.lower():
        raise HandoverError("That is your own email address. Enter your successor's.")
    if get_user_model().objects.filter(email__iexact=email).exists():
        # Deliberately does not say where it signs in.
        raise HandoverError(
            "That email address already has an account, so it cannot be used here. "
            "Ask the school owner to change that person's role on the Staff screen."
        )
    return email


def expire_lapsed() -> int:
    return RoleHandover.objects.filter(
        status=Status.PENDING, expires_at__lte=timezone.now()
    ).update(status=Status.EXPIRED, decided_at=timezone.now())


def open_for(user) -> RoleHandover | None:
    expire_lapsed()
    return RoleHandover.objects.filter(from_user=user, status__in=RoleHandover.OPEN).first()


def awaiting_owner(school_id):
    return RoleHandover.objects.filter(
        school_id=school_id, status=Status.AWAITING_APPROVAL
    ).select_related("from_user", "branch")


# --- 1. asking ---------------------------------------------------------------------


def request_handover(*, user, email: str, name: str, origin: str | None = None) -> RoleHandover:
    if not may_hand_over(user):
        raise HandoverError("Only a principal or bursar can hand over their role this way.")
    email = check_recipient(email, asker=user)
    if open_for(user) is not None:
        raise HandoverError("You already have a handover waiting. Cancel it before starting another.")
    since = timezone.now() - timedelta(days=1)
    if RoleHandover.objects.filter(from_user=user, created_at__gte=since).count() >= DAILY_LIMIT:
        raise HandoverError(f"You can start {DAILY_LIMIT} handovers a day. Try again tomorrow.")
    try:
        with transaction.atomic():
            handover = RoleHandover.objects.create(
                school_id=user.school_id, branch_id=user.branch_id, from_user=user,
                role=user.role, to_email=email, to_name=" ".join(name.split())[:150],
            )
            _audit(handover, "handover.requested", {"to": [None, email]})
    except IntegrityError:
        raise HandoverError("You already have a handover waiting. Cancel it before starting another.")
    for owner in owners(user.school_id):
        _send(
            handover, to=owner.email, origin=origin,
            subject=f"{_name(user)} wants to hand over their {handover.get_role_display()} role",
            heading="A role handover needs your approval",
            lines=[
                f"{_name(user)}, {handover.get_role_display()} at {handover.branch.name}, has asked to hand "
                f"their role to {handover.to_name} ({handover.to_email}).",
                "Nothing happens unless you approve. If you approve, we email the newcomer a "
                "link; when they accept, they take over the role and the old account is switched off.",
            ],
            cta_path=reverse("accounts:settings") + "#handovers", cta_label="Review the request",
        )
    return handover


def cancel(handover: RoleHandover, *, by) -> None:
    with transaction.atomic():
        locked = RoleHandover.objects.select_for_update().get(pk=handover.pk)
        if locked.from_user_id != by.pk:
            raise HandoverError("Only the person who started a handover can cancel it.")
        if locked.status not in RoleHandover.OPEN:
            raise HandoverError("That handover is no longer waiting, so there is nothing to cancel.")
        _close(locked, Status.CANCELLED, "handover.cancelled")


# --- 2. the owner decides ------------------------------------------------------------


def _owner_of(handover, by) -> None:
    if not (by.is_active and by.role == Role.SCHOOL_OWNER and by.school_id == handover.school_id):
        raise HandoverError("Only the school's owner can decide a handover.")


def approve(handover: RoleHandover, *, by, origin: str | None = None) -> bool:
    """Approve and email the link. Returns whether the email went out."""
    token = secrets.token_urlsafe(32)
    with transaction.atomic():
        locked = RoleHandover.objects.select_for_update().select_related(
            "from_user", "branch", "school"
        ).get(pk=handover.pk)
        _owner_of(locked, by)
        if locked.status != Status.AWAITING_APPROVAL:
            raise HandoverError("That handover is no longer waiting for approval.")
        _still_theirs(locked)
        check_recipient(locked.to_email, asker=locked.from_user)
        locked.status = Status.PENDING
        locked.approved_by = by
        locked.token_hash = hash_token(token)
        locked.expires_at = timezone.now() + LINK_TTL
        locked.save(update_fields=["status", "approved_by", "token_hash", "expires_at"])
        _audit(locked, "handover.approved", {"status": ["awaiting_approval", "pending"]})
    role = locked.get_role_display()
    emailed = _send(
        locked, to=locked.to_email, origin=origin,
        subject=f"You have been asked to take over as {role} at {locked.school.name}",
        heading=f"Take over as {role}",
        greeting=f"Hello {locked.to_name},",
        lines=[
            f"{_name(locked.from_user)} is handing their role as {role} at {locked.branch.name}, "
            f"{locked.school.name}, to you, and the school's owner has approved it.",
            "Nothing changes until you accept. The link works once, until "
            f"{timezone.localtime(locked.expires_at):%d %B %Y at %I:%M %p}.",
        ],
        cta_path=reverse("accounts:handover_accept", args=[token]), cta_label="Review and accept",
    )
    _send(
        locked, to=locked.from_user.email, origin=origin,
        subject="Your role handover was approved",
        heading="Your handover was approved",
        lines=[f"We have emailed {locked.to_name} a link. When they accept, they take over as "
               f"{role} and your account is switched off. You can cancel until then."],
        cta_path=reverse("accounts:settings") + "#handover", cta_label="Open Account settings",
    )
    return emailed


def reject(handover: RoleHandover, *, by, origin: str | None = None) -> None:
    with transaction.atomic():
        locked = RoleHandover.objects.select_for_update().select_related("from_user").get(pk=handover.pk)
        _owner_of(locked, by)
        if locked.status != Status.AWAITING_APPROVAL:
            raise HandoverError("That handover is no longer waiting for approval.")
        _close(locked, Status.REJECTED, "handover.rejected")
    _send(
        locked, to=locked.from_user.email, origin=origin,
        subject="Your role handover was not approved",
        heading="Your handover was not approved",
        lines=[f"The school's owner did not approve handing your role to {locked.to_name}. "
               "Nothing has changed. Speak to the owner if you expected otherwise."],
    )


# --- 3. the newcomer -----------------------------------------------------------------


def decline(handover: RoleHandover, *, origin: str | None = None) -> None:
    with transaction.atomic():
        locked = RoleHandover.objects.select_for_update().select_related("from_user").get(pk=handover.pk)
        if not locked.is_acceptable:
            raise HandoverError("This handover is no longer waiting for an answer.")
        _close(locked, Status.DECLINED, "handover.declined")
    _send(
        locked, to=locked.from_user.email, origin=origin,
        subject=f"{locked.to_name} declined your role handover",
        heading="Your handover was declined",
        lines=[f"{locked.to_name} declined to take over your role. Nothing has changed."],
    )


def _still_theirs(locked: RoleHandover) -> None:
    person = locked.from_user
    if not (person.is_active and person.role == locked.role
            and person.school_id == locked.school_id and person.branch_id == locked.branch_id):
        raise HandoverError(
            "The person handing over no longer holds this role, so it cannot go ahead."
        )
    if not locked.branch.is_active:
        raise HandoverError("That campus has been closed, so this handover cannot go ahead.")


def accept(handover: RoleHandover, *, new_account: NewAccount, origin: str | None = None):
    """Give the newcomer the role and switch the old account off. Returns the new account."""
    User = get_user_model()
    with transaction.atomic():
        locked = RoleHandover.objects.select_for_update().select_related(
            "from_user", "branch", "school"
        ).get(pk=handover.pk)
        if locked.status == Status.PENDING and locked.expires_at <= timezone.now():
            _close(locked, Status.EXPIRED, None)
            raise HandoverError("This link has expired. Ask for the handover to be started again.")
        if locked.status != Status.PENDING:
            raise HandoverError("This handover is no longer waiting to be accepted.")
        old = User.objects.select_for_update().get(pk=locked.from_user_id)
        locked.from_user = old
        _still_theirs(locked)
        check_recipient(locked.to_email, asker=old)

        newcomer = User(
            username=unique_username(locked.to_email), email=locked.to_email,
            first_name=new_account.first_name[:150], last_name=new_account.last_name[:150],
            school_id=locked.school_id, branch_id=locked.branch_id, role=locked.role,
            job_title=old.job_title,
        )
        newcomer.set_password(new_account.password)
        newcomer.save()
        old.is_active = False
        old.save(update_fields=["is_active"])

        locked.status = Status.ACCEPTED
        locked.to_user = newcomer
        locked.decided_at = timezone.now()
        locked.save(update_fields=["status", "to_user", "decided_at"])
        _audit(locked, "handover.completed",
               {"account": [old.get_username(), newcomer.get_username()]})
    role = locked.get_role_display()
    for person in [old, *owners(locked.school_id)]:
        _send(
            locked, to=person.email, origin=origin,
            subject=f"{_name(newcomer)} is now {role} at {locked.branch.name}",
            heading="The role was handed over",
            lines=[f"{_name(newcomer)} ({newcomer.email}) accepted and is now {role} at "
                   f"{locked.branch.name}. {_name(old)}'s account has been switched off. "
                   "Nothing either of them recorded has been deleted."],
        )
    return newcomer


# --- records and mail ------------------------------------------------------------------


def _name(person) -> str:
    return person.get_full_name() or person.get_username()


def _close(locked: RoleHandover, status: str, action: str | None) -> None:
    before = locked.status
    locked.status = status
    locked.decided_at = timezone.now()
    locked.token_hash = ""
    locked.save(update_fields=["status", "decided_at", "token_hash"])
    if action:
        _audit(locked, action, {"status": [before, status]})


def _audit(handover: RoleHandover, action: str, changes: dict) -> None:
    record(
        action=action, category=Category.ACCESS, target_type="rolehandover",
        target_id=handover.pk,
        target_label=f"{handover.get_role_display()} to {handover.to_email}"[:160],
        school_id=handover.school_id, branch_id=handover.branch_id, changes=changes,
    )


def _send(handover, *, to: str, subject: str, heading: str, lines: list[str],
          origin=None, greeting: str = "", cta_path: str | None = None,
          cta_label: str = "") -> bool:
    """One of the handover emails. Best effort, like every platform email."""
    if not to:
        return False
    site = _origin(origin)
    if site is None:
        logger.warning("Handover email for %s not sent: no site address to link to.", handover.pk)
        return False
    context = {
        **branding(), "heading": heading, "greeting": greeting, "lines": lines,
        "cta_url": site + cta_path if cta_path else None, "cta_label": cta_label,
        "protocol": urlsplit(site).scheme, "domain": urlsplit(site).netloc, "site_url": site,
    }
    try:
        message = EmailMultiAlternatives(
            subject=subject, body=render_to_string("email/handover_notice.txt", context), to=[to],
        )
        message.attach_alternative(render_to_string("email/handover_notice.html", context), "text/html")
        message.send()
    except Exception:
        logger.exception("Handover email for %s could not be sent", handover.pk)
        return False
    return True
