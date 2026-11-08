"""Handing a school to a new owner, by email.

The owner starts a transfer naming someone's email; that person accepts from a
single-use link; the two roles swap in one transaction. Every rule lives here so
the settings screen, the acceptance page and the platform's management command
cannot disagree about any of them.

The rules, and why:

* **One owner, never two and never none.** There is one proprietor per school
  (see ``StaffAccountForm.ASSIGNABLE_ROLES``). A transfer replaces the owner; it
  never adds one. Promotion and demotion happen in the same transaction, under a
  lock on the transfer and on the school, so there is no moment with two owners
  and no moment with none.
* **An account belongs to one school.** ``User.school`` is a single foreign key,
  so an email that already signs in somewhere else cannot become this school's
  owner. The refusal does not say where it signs in: that would tell one school
  who works at another.
* **The link is the only key.** 256 random bits, stored as a SHA-256, usable
  once, dead after ``TRANSFER_TTL``.
* **Nothing the school owns moves.** Students, payments, the plan, open plan
  orders and the audit log are the school's. Only two accounts change.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlsplit

from django.conf import settings
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
from .models import OwnershipTransfer

logger = logging.getLogger(__name__)

#: The same three days as a password reset link (PASSWORD_RESET_TIMEOUT).
TRANSFER_TTL = timedelta(hours=72)

#: Transfers a school may start in a day. Each sends an email to an address the
#: owner typed, so the limit bounds what a hijacked session could send.
DAILY_LIMIT = 3

Status = OwnershipTransfer.Status
Afterwards = OwnershipTransfer.Afterwards


class TransferError(Exception):
    """A transfer cannot go ahead. The message is written to be shown as is."""


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


# --- who it can go to ---------------------------------------------------------


def recipient_for(school, email: str, *, current_owner=None):
    """The existing account ``email`` belongs to at ``school``, or ``None`` if new.

    Raises :class:`TransferError` for every address that cannot become the owner.
    """
    email = (email or "").strip()
    if current_owner is not None and current_owner.email.lower() == email.lower():
        raise TransferError("That is your own email address. Enter the new owner's.")

    matches = list(get_user_model().objects.filter(email__iexact=email)[:2])
    if not matches:
        return None
    if len(matches) > 1:
        # Emails are unique in practice (signup and staff forms refuse a second),
        # but sign-in by email cannot tell two apart, so neither can this.
        raise TransferError(
            "That email address cannot be used. Ask the new owner for a different one."
        )
    person = matches[0]
    if person.school_id != school.pk or person.is_superuser:
        # Another school's account, or platform staff. Deliberately vague.
        raise TransferError(
            "That email address already belongs to an account that cannot be "
            "moved to your school. Ask the new owner to use a different one."
        )
    if not person.is_active:
        raise TransferError(
            f"{person.get_full_name() or person.username} has been switched off. "
            "Ask the platform to switch them back on first, or use another email."
        )
    if person.role == Role.SCHOOL_OWNER:
        raise TransferError("That account already owns this school.")
    return person


def current_owners(school):
    return get_user_model().objects.filter(
        school=school, role=Role.SCHOOL_OWNER, is_active=True
    )


def open_transfer(school) -> OwnershipTransfer | None:
    """The school's pending transfer, if any. Marks a lapsed one expired first."""
    expire_lapsed(school)
    return OwnershipTransfer.all_objects.filter(school=school, status=Status.PENDING).first()


def expire_lapsed(school=None) -> int:
    lapsed = OwnershipTransfer.all_objects.filter(
        status=Status.PENDING, expires_at__lte=timezone.now()
    )
    if school is not None:
        lapsed = lapsed.filter(school=school)
    return lapsed.update(status=Status.EXPIRED, decided_at=timezone.now())


# --- starting -------------------------------------------------------------------


@dataclass
class Started:
    transfer: OwnershipTransfer
    #: The raw token, which exists only here and in the email.
    token: str
    emailed: bool


def start_transfer(
    *,
    school,
    email: str,
    name: str,
    afterwards: str,
    stay_branch=None,
    started_by=None,
    origin: str | None = None,
) -> Started:
    """Open a transfer and email the link. ``started_by`` is ``None`` for the platform.

    ``origin`` is the site the link points at (``https://host``); without it the
    configured ``PUBLIC_BASE_URL`` is used.
    """
    if started_by is not None:
        if not (
            started_by.is_active
            and started_by.role == Role.SCHOOL_OWNER
            and started_by.school_id == school.pk
        ):
            raise TransferError("Only the school's owner can hand the school over.")
    recipient_for(school, email, current_owner=started_by)
    if afterwards not in Afterwards.values:
        raise TransferError("Choose what happens to the current owner's account.")
    if afterwards == Afterwards.STAY_PRINCIPAL:
        if stay_branch is None or stay_branch.school_id != school.pk or not stay_branch.is_active:
            raise TransferError("Choose the campus the current owner will run.")
    else:
        stay_branch = None

    if open_transfer(school) is not None:
        raise TransferError(
            "A transfer is already waiting to be accepted. Cancel it before starting another."
        )
    since = timezone.now() - timedelta(days=1)
    if OwnershipTransfer.all_objects.filter(school=school, created_at__gte=since).count() >= DAILY_LIMIT:
        raise TransferError(
            f"A school can start {DAILY_LIMIT} transfers a day. Try again tomorrow."
        )

    token = secrets.token_urlsafe(32)
    try:
        with transaction.atomic():
            transfer = OwnershipTransfer.all_objects.create(
                school=school,
                started_by=started_by,
                to_email=email.strip(),
                to_name=" ".join(name.split())[:150],
                afterwards=afterwards,
                stay_branch=stay_branch,
                token_hash=hash_token(token),
                expires_at=timezone.now() + TRANSFER_TTL,
            )
            _audit(transfer, "ownership.transfer_started", {"to": [None, transfer.to_email]})
    except IntegrityError:
        # The partial unique index: another tab started one a moment ago.
        raise TransferError(
            "A transfer is already waiting to be accepted. Cancel it before starting another."
        )

    emailed = _send(
        "ownership_invite",
        subject=f"You have been asked to take over {school.name} on {_product()}",
        to=transfer.to_email,
        transfer=transfer,
        origin=origin,
        accept_path=reverse("accounts:ownership_accept", args=[token]),
    )
    if transfer.started_by is not None:
        _send(
            "ownership_started",
            subject=f"You started handing {school.name} to {transfer.to_name}",
            to=transfer.started_by.email,
            transfer=transfer,
            origin=origin,
        )
    return Started(transfer=transfer, token=token, emailed=emailed)


# --- ending without a handover ------------------------------------------------------


def cancel_transfer(transfer: OwnershipTransfer, *, by) -> None:
    """The owner (or the platform) withdraws a pending transfer."""
    with transaction.atomic():
        locked = OwnershipTransfer.all_objects.select_for_update().get(pk=transfer.pk)
        if locked.status != Status.PENDING:
            raise TransferError("That transfer is no longer waiting, so there is nothing to cancel.")
        locked.status = Status.CANCELLED
        locked.decided_at = timezone.now()
        locked.save(update_fields=["status", "decided_at"])
        _audit(locked, "ownership.transfer_cancelled", {"status": ["pending", "cancelled"]})


def decline_transfer(transfer: OwnershipTransfer, *, origin: str | None = None) -> None:
    """The person it was for says no. The owner is told."""
    with transaction.atomic():
        locked = OwnershipTransfer.all_objects.select_for_update().get(pk=transfer.pk)
        if not locked.is_open:
            raise TransferError("This transfer is no longer waiting for an answer.")
        locked.status = Status.DECLINED
        locked.decided_at = timezone.now()
        locked.save(update_fields=["status", "decided_at"])
        _audit(locked, "ownership.transfer_declined", {"status": ["pending", "declined"]})
    if locked.started_by is not None:
        _send(
            "ownership_outcome",
            subject=f"{locked.to_name} declined to take over {locked.school.name}",
            to=locked.started_by.email,
            transfer=locked,
            origin=origin,
            outcome="declined",
        )


# --- the handover ------------------------------------------------------------------


@dataclass
class NewAccount:
    first_name: str
    last_name: str
    password: str


def accept_transfer(
    transfer: OwnershipTransfer,
    *,
    user=None,
    new_account: NewAccount | None = None,
    origin: str | None = None,
):
    """Make the transfer's recipient the owner. Returns the new owner's account.

    ``user`` is the signed-in account accepting, when the email already belongs
    to someone at the school; ``new_account`` is the name and password chosen on
    the acceptance page when it does not.
    """
    from apps.schools.models import Branch, School

    User = get_user_model()
    with transaction.atomic():
        locked = OwnershipTransfer.all_objects.select_for_update().get(pk=transfer.pk)
        # Locks the school row too, so two transfers can never interleave.
        school = School.all_objects.select_for_update().get(pk=locked.school_id)

        if locked.status == Status.PENDING and locked.expires_at <= timezone.now():
            locked.status = Status.EXPIRED
            locked.decided_at = timezone.now()
            locked.save(update_fields=["status", "decided_at"])
            raise TransferError("This link has expired. Ask the owner to start the transfer again.")
        if locked.status != Status.PENDING:
            raise TransferError("This transfer is no longer waiting to be accepted.")

        owners = list(current_owners(school).select_for_update())
        if locked.started_by_id is not None and locked.started_by_id not in {o.pk for o in owners}:
            raise TransferError(
                "The person who started this transfer no longer owns the school, so it "
                "cannot go ahead."
            )

        existing = recipient_for(school, locked.to_email)
        if existing is not None:
            if user is None or user.pk != existing.pk:
                raise TransferError(
                    f"Sign in as {locked.to_email} to accept this transfer."
                )
            new_owner = existing
        else:
            if user is not None and user.is_authenticated:
                raise TransferError(
                    "You are signed in to another account. Sign out, then open the link again."
                )
            if new_account is None:
                raise TransferError("Choose your name and a password to accept.")
            new_owner = User(
                username=unique_username(locked.to_email),
                email=locked.to_email,
                first_name=new_account.first_name[:150],
                last_name=new_account.last_name[:150],
                school=school,
            )
            new_owner.set_password(new_account.password)

        new_owner.role = Role.SCHOOL_OWNER
        # An owner sees every campus.
        new_owner.branch = None
        new_owner.save()

        stay_branch = None
        if locked.afterwards == Afterwards.STAY_PRINCIPAL:
            stay_branch = (
                Branch.all_objects.filter(pk=locked.stay_branch_id, school=school, is_active=True).first()
                if locked.stay_branch_id
                else None
            )
            if stay_branch is None:
                raise TransferError(
                    "The campus the current owner was going to run has been closed. Ask "
                    "them to start the transfer again."
                )
        for owner in owners:
            if owner.pk == new_owner.pk:
                continue
            if stay_branch is not None:
                owner.role = Role.PRINCIPAL
                owner.branch = stay_branch
                owner.save(update_fields=["role", "branch"])
            else:
                owner.is_active = False
                owner.save(update_fields=["is_active"])

        locked.status = Status.ACCEPTED
        locked.to_user = new_owner
        locked.decided_at = timezone.now()
        locked.save(update_fields=["status", "to_user", "decided_at"])
        _audit(
            locked,
            "ownership.transferred",
            {"owner": [", ".join(o.get_username() for o in owners), new_owner.get_username()]},
        )

    for owner in owners:
        if owner.pk != new_owner.pk:
            _send(
                "ownership_outcome",
                subject=f"{school.name} now belongs to {new_owner.get_full_name() or new_owner.email}",
                to=owner.email,
                transfer=locked,
                origin=origin,
                outcome="handed_over",
                person=owner,
            )
    _send(
        "ownership_outcome",
        subject=f"You now own {school.name} on {_product()}",
        to=new_owner.email,
        transfer=locked,
        origin=origin,
        outcome="received",
        person=new_owner,
    )
    return new_owner


# --- records and mail -----------------------------------------------------------------


def _audit(transfer: OwnershipTransfer, action: str, changes: dict) -> None:
    record(
        action=action,
        category=Category.ACCESS,
        target_type="ownershiptransfer",
        target_id=transfer.pk,
        target_label=f"{transfer.school.name} to {transfer.to_email}",
        school_id=transfer.school_id,
        changes=changes,
    )


def _product() -> str:
    return branding()["product_name"]


def _origin(origin: str | None) -> str | None:
    url = (origin or getattr(settings, "PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return None
    return url


def _send(template: str, *, subject: str, to: str, transfer, origin=None,
          accept_path: str | None = None, **extra) -> bool:
    """Send one of the transfer emails. Best effort, like every platform email.

    Returns ``True`` only if the message reached the mail backend; the caller
    decides whether a failure is worth telling anyone about.
    """
    if not to:
        return False
    site = _origin(origin)
    if site is None:
        logger.warning(
            "Ownership email %s for transfer %s not sent: no site address to link to.",
            template, transfer.pk,
        )
        return False
    context = {
        **branding(),
        "transfer": transfer,
        "school": transfer.school,
        "site_url": site,
        "settings_url": site + reverse("accounts:settings") + "#ownership",
        "accept_url": site + accept_path if accept_path else None,
        "expires_at": timezone.localtime(transfer.expires_at),
        "protocol": urlsplit(site).scheme,
        "domain": urlsplit(site).netloc,
        **extra,
    }
    try:
        message = EmailMultiAlternatives(
            subject=subject,
            body=render_to_string(f"email/{template}.txt", context),
            to=[to],
        )
        message.attach_alternative(render_to_string(f"email/{template}.html", context), "text/html")
        message.send()
    except Exception:
        logger.exception("Ownership email %s for transfer %s could not be sent", template, transfer.pk)
        return False
    return True
