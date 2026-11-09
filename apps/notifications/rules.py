"""Activity notifications, read off the audit log.

Every change to money and access is already written as an ``AuditEvent`` (see
apps/audit/recording.py), from the web, the admin or a command. Hanging the
notifications off that one record means a new screen that records a payment
notifies people without anyone remembering to wire it in.

A recorded payment writes two events: its ``PaymentTransaction`` and its
``Payment``. One-child payments are told from the ``Payment`` (it names the
child); shared family payments from the transaction (it is one sum, however
many children it covers). Each payment is told about once.

A failure here must never undo the payment that caused it, so :func:`handle`
runs each rule in its own savepoint and swallows its errors.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from django.urls import reverse

from apps.core.roles import Role

from .delivery import campus_staff, notify, owners
from .models import Kind

log = logging.getLogger(__name__)


def naira(amount) -> str:
    amount = Decimal(amount or 0)
    if amount == amount.to_integral_value():
        return f"₦{amount:,.0f}"
    return f"₦{amount:,.2f}"


def _who(event) -> str:
    if event.actor is not None:
        return event.actor.get_full_name() or event.actor.get_username()
    return event.actor_name or "SCHOOLCORD"


def _payment(event):
    from apps.payments.models import Payment, TransactionKind

    payment = (
        Payment.all_objects.select_related("student", "transaction", "recorded_by")
        .filter(pk=event.target_id).first()
    )
    if payment is None:
        return None
    if payment.transaction_id and payment.transaction.kind == TransactionKind.FAMILY:
        return None  # told once, from the shared transaction
    return payment


def _transaction(event):
    from apps.payments.models import PaymentTransaction, TransactionKind

    txn = (
        PaymentTransaction.all_objects.select_related("family", "recorded_by")
        .filter(pk=event.target_id).first()
    )
    if txn is None or txn.kind != TransactionKind.FAMILY:
        return None  # a one-child transaction is told from its payment
    return txn


def _payment_created(event):
    from apps.payments.models import PaymentStatus

    payment = _payment(event)
    if payment is None:
        return
    url = reverse("payments:payment_detail", args=[payment.pk])
    what = f"{naira(payment.amount)} for {payment.student.full_name}"
    if payment.status == PaymentStatus.PENDING:
        notify(
            campus_staff(payment.school_id, payment.branch_id) + owners(payment.school_id),
            kind=Kind.PAYMENT_WAITING, title=f"{what} is waiting to be checked",
            body=f"Recorded by {_who(event)}. Check it against the bank before it counts.",
            url=url, school_id=payment.school_id, branch_id=payment.branch_id,
            event=event, exclude=event.actor,
        )
    elif payment.status == PaymentStatus.CONFIRMED:
        notify(
            owners(payment.school_id),
            kind=Kind.PAYMENT_RECORDED, title=f"{what} recorded",
            body=f"Recorded and checked by {_who(event)}.",
            url=url, school_id=payment.school_id, branch_id=payment.branch_id,
            event=event, exclude=event.actor,
        )


def _payment_status(event, *, voided: bool):
    payment = _payment(event)
    if payment is None:
        return
    what = f"{naira(payment.amount)} for {payment.student.full_name}"
    if voided:
        reason = (payment.void_reason or "").strip()
        title, kind = f"{what} was voided", Kind.PAYMENT_VOIDED
        body = f"Voided by {_who(event)}." + (f" Reason: {reason}" if reason else "")
    else:
        title, kind = f"{what} was checked", Kind.PAYMENT_CHECKED
        body = f"Checked against the bank by {_who(event)}. It now counts toward the balance."
    notify(
        [payment.recorded_by] + owners(payment.school_id),
        kind=kind, title=title, body=body,
        url=reverse("payments:payment_detail", args=[payment.pk]),
        school_id=payment.school_id, branch_id=payment.branch_id,
        event=event, exclude=event.actor,
    )


def _shared_label(txn) -> str:
    payer = txn.payer_name or (txn.family.name if txn.family_id else "")
    return f"{naira(txn.amount)} shared payment" + (f" from {payer}" if payer else "")


def _transaction_created(event):
    from apps.payments.models import PaymentStatus

    txn = _transaction(event)
    if txn is None:
        return
    url = reverse("payments:shared_detail", args=[txn.pk])
    if txn.status == PaymentStatus.PENDING:
        notify(
            campus_staff(txn.school_id, txn.branch_id) + owners(txn.school_id),
            kind=Kind.PAYMENT_WAITING, title=f"{_shared_label(txn)} is waiting to be checked",
            body=f"Recorded by {_who(event)}. Check it against the bank before it counts.",
            url=url, school_id=txn.school_id, branch_id=txn.branch_id,
            event=event, exclude=event.actor,
        )
    elif txn.status == PaymentStatus.CONFIRMED:
        notify(
            owners(txn.school_id),
            kind=Kind.PAYMENT_RECORDED, title=f"{_shared_label(txn)} recorded",
            body=f"Recorded and checked by {_who(event)}.",
            url=url, school_id=txn.school_id, branch_id=txn.branch_id,
            event=event, exclude=event.actor,
        )


def _transaction_status(event, *, voided: bool):
    txn = _transaction(event)
    if txn is None:
        return
    if voided:
        reason = (txn.void_reason or "").strip()
        title, kind = f"{_shared_label(txn)} was voided", Kind.PAYMENT_VOIDED
        body = f"Voided by {_who(event)}." + (f" Reason: {reason}" if reason else "")
    else:
        title, kind = f"{_shared_label(txn)} was checked", Kind.PAYMENT_CHECKED
        body = f"Checked against the bank by {_who(event)}."
    notify(
        [txn.recorded_by] + owners(txn.school_id),
        kind=kind, title=title, body=body,
        url=reverse("payments:shared_detail", args=[txn.pk]),
        school_id=txn.school_id, branch_id=txn.branch_id,
        event=event, exclude=event.actor,
    )


def _import(event, *, undone: bool):
    title = (f"An import was undone: {event.target_label}" if undone
             else f"Records imported from {event.target_label}")
    body = f"By {_who(event)}."
    if undone:
        reason = (event.changes.get("reason") or [None, ""])[1]
        if reason:
            body += f" Reason: {reason}"
    notify(
        owners(event.school_id), kind=Kind.IMPORT, title=title, body=body,
        url=reverse("onboarding:detail", args=[event.target_id]),
        school_id=event.school_id, event=event, exclude=event.actor,
    )


def _role_label(value) -> str:
    try:
        return Role(value).label
    except ValueError:
        return str(value or "")


def _staff(event, *, created: bool):
    from django.contrib.auth import get_user_model

    person = get_user_model().objects.filter(pk=event.target_id).first()
    if person is None or person.school_id is None or person.role == Role.PLATFORM_OWNER:
        return
    name = person.get_full_name() or person.get_username()
    url = reverse("staff:list")
    if created:
        # A new owner is not told about their own account.
        notify(
            [o for o in owners(person.school_id) if o.pk != person.pk], kind=Kind.STAFF,
            title=f"New staff account: {name}",
            body=f"{_role_label(person.role)}, added by {_who(event)}.",
            url=url, school_id=person.school_id, branch_id=person.branch_id,
            event=event, exclude=event.actor,
        )
        return
    changes = event.changes or {}
    if "role" in changes:
        new = _role_label(changes["role"][1])
        notify(owners(person.school_id), kind=Kind.STAFF,
               title=f"{name} is now {new}", body=f"Changed by {_who(event)}.",
               url=url, school_id=person.school_id, event=event, exclude=event.actor)
        notify([person], kind=Kind.STAFF, title=f"Your role is now {new}",
               body=f"Changed by {_who(event)}.", school_id=person.school_id,
               event=event, exclude=event.actor)
    if "is_active" in changes:
        on = bool(changes["is_active"][1])
        notify(owners(person.school_id), kind=Kind.STAFF,
               title=f"{name}'s account was {'switched back on' if on else 'switched off'}",
               body=f"By {_who(event)}.", url=url, school_id=person.school_id,
               event=event, exclude=event.actor)


def _security(event):
    from django.contrib.auth import get_user_model

    person = get_user_model().objects.filter(pk=event.target_id).first()
    if person is None or person.school_id is None:
        return
    name = person.get_full_name() or person.get_username()
    settings_url = reverse("accounts:settings")
    if event.action == "mfa.reset":
        notify([person], kind=Kind.SECURITY, title="Your two-step sign-in was reset",
               body=f"By {_who(event)}. Set it up again from Account settings.",
               url=settings_url, school_id=person.school_id, event=event,
               exclude=event.actor)
        notify(owners(person.school_id), kind=Kind.SECURITY,
               title=f"{name}'s two-step sign-in was reset", body=f"By {_who(event)}.",
               url=reverse("staff:list"), school_id=person.school_id, event=event,
               exclude=event.actor)
    elif event.action == "mfa.recovery_code_used":
        # The person is told even though they did it: if it was not them, they need to know.
        notify([person], kind=Kind.SECURITY, title="A recovery code was used to sign in",
               body="If this was not you, change your password and tell the school owner.",
               url=settings_url, school_id=person.school_id, event=event)
        notify(owners(person.school_id), kind=Kind.SECURITY,
               title=f"{name} signed in with a recovery code",
               url=reverse("staff:list"), school_id=person.school_id, event=event,
               exclude=person)


_OWNERSHIP_TITLES = {
    "ownership.transfer_started": "An ownership transfer was started",
    "ownership.transfer_cancelled": "An ownership transfer was cancelled",
    "ownership.transfer_declined": "An ownership transfer was declined",
    "ownership.transferred": "Ownership of the school was handed over",
}


def _ownership(event):
    notify(
        owners(event.school_id), kind=Kind.OWNERSHIP,
        title=_OWNERSHIP_TITLES[event.action], body=event.target_label,
        url=reverse("accounts:settings") + "#ownership",
        school_id=event.school_id, event=event, exclude=event.actor,
    )


RULES = {
    "payment.created": _payment_created,
    "payment.confirmed": lambda e: _payment_status(e, voided=False),
    "payment.voided": lambda e: _payment_status(e, voided=True),
    "paymenttransaction.created": _transaction_created,
    "paymenttransaction.confirmed": lambda e: _transaction_status(e, voided=False),
    "paymenttransaction.voided": lambda e: _transaction_status(e, voided=True),
    "importbatch.committed": lambda e: _import(e, undone=False),
    "importbatch.undone": lambda e: _import(e, undone=True),
    "user.created": lambda e: _staff(e, created=True),
    "user.changed": lambda e: _staff(e, created=False),
    "mfa.reset": _security,
    "mfa.recovery_code_used": _security,
    **{action: _ownership for action in _OWNERSHIP_TITLES},
}


def handle(sender, instance, created=False, raw=False, **kwargs):
    """``post_save`` on ``AuditEvent``: tell whoever needs to know."""
    from django.db import transaction

    if raw or not created or not instance.school_id:
        return
    rule = RULES.get(instance.action)
    if rule is None:
        return
    try:
        with transaction.atomic():
            rule(instance)
    except Exception:  # noqa: BLE001 -- a notice must never cost the school its payment
        log.exception("notifications: %s %s was not told", instance.action, instance.target_id)
