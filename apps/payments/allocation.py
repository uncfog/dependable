"""Sharing one sum of money across children. The only way to change shared money.

Every change takes a row lock on the transaction first, so two people sharing out the
same money at the same moment queue up, and the second sees what the first left. That
lock is what makes the rule true: **allocations can never exceed the amount received.**

Three decisions the school owner made, all enforced here:

* paying a child more than they owe needs explicit confirmation, and otherwise the
  money stays unallocated;
* credit moves between siblings only by a logged void and a new allocation, which is
  :func:`reallocate`, and never by itself;
* staff at a campus allocate only to children there, and a school owner across them.

Money that belongs to one child is corrected as ever, through its ``Payment``.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.core.roles import Scope, scope_for
from apps.core.tenancy import TenantContext
from apps.families.models import FamilyMembership
from apps.fees.models import Term
from apps.students import fees

from .models import (
    Payment,
    PaymentLabel,
    PaymentSource,
    PaymentStatus,
    PaymentTransaction,
    TransactionKind,
)

ZERO = Decimal("0")


class AllocationError(Exception):
    """Refused. The message is written for the person at the screen."""


class ExceedsReceived(AllocationError):
    def __init__(self, unallocated: Decimal):
        self.unallocated = unallocated
        super().__init__(f"Only {unallocated:,.2f} of this payment is left to allocate.")


class WouldOverpay(AllocationError):
    """Asked for more than the child owes, with no confirmation that it is meant."""

    def __init__(self, owed: Decimal, amount: Decimal):
        self.owed = owed
        self.amount = amount
        super().__init__(
            f"That is more than this child owes ({owed:,.2f}). Confirm it to keep the "
            f"difference as their credit, or leave it unallocated."
        )


class NotInFamily(AllocationError):
    def __init__(self):
        super().__init__("That child is not in the family this payment came from.")


class CampusNotAllowed(AllocationError):
    def __init__(self):
        super().__init__("That child is at another campus. Ask the school owner to do this.")


def may_allocate_to(actor, student) -> bool:
    """The campus rule. School-wide and platform accounts may reach any campus."""
    scope = scope_for(TenantContext.from_user(actor).role)
    if scope is not Scope.BRANCH:
        return True
    return actor.branch_id == student.branch_id


def receive(*, amount, date_paid, method, actor=None, branch, family=None, reference="",
            payer_name="", note="", status=PaymentStatus.CONFIRMED,
            source=PaymentSource.MANUAL, receipt=None) -> PaymentTransaction:
    """Record money that came in, ready to be shared out."""
    if amount <= ZERO:
        raise AllocationError("A payment has to be more than nothing.")
    now = timezone.now()
    confirmed = status == PaymentStatus.CONFIRMED
    return PaymentTransaction.all_objects.create(
        school_id=branch.school_id,
        branch=branch,
        kind=TransactionKind.FAMILY,
        family=family,
        amount=amount,
        date_paid=date_paid,
        method=method,
        reference=reference,
        payer_name=payer_name,
        note=note,
        receipt=receipt or "",
        source=source,
        status=status,
        recorded_by=actor,
        confirmed_by=actor if confirmed else None,
        confirmed_at=now if confirmed else None,
    )


def _locked(txn: PaymentTransaction) -> PaymentTransaction:
    return PaymentTransaction.all_objects.select_for_update().get(pk=txn.pk)


@transaction.atomic
def allocate(txn: PaymentTransaction, student, amount, *, actor=None,
             label=PaymentLabel.SCHOOL_FEES, note="", allow_credit=False) -> Payment:
    """Give ``student`` a share. It is credited to their campus's current term."""
    txn = _locked(txn)
    amount = Decimal(amount)

    if not txn.is_shared:
        raise AllocationError("This payment is for one child, so it can't be shared.")
    if txn.status == PaymentStatus.VOID:
        raise AllocationError("This payment has been voided.")
    if amount <= ZERO:
        raise AllocationError("An allocation has to be more than nothing.")
    if student.school_id != txn.school_id:
        raise AllocationError("That child is at a different school.")
    if actor is not None and not may_allocate_to(actor, student):
        raise CampusNotAllowed()
    if txn.family_id and not FamilyMembership.all_objects.filter(
        family_id=txn.family_id, student=student, ended_at__isnull=True
    ).exists():
        raise NotInFamily()

    left = txn.unallocated
    if amount > left:
        raise ExceedsReceived(left)

    term = Term.all_objects.filter(branch_id=student.branch_id, is_current=True).first()
    if term is None:
        raise AllocationError(
            f"{student.branch.name} has no current term to credit this payment to."
        )

    if not allow_credit:
        # Money on its way to this child counts, or a second share could overpay them.
        pending = sum(
            Payment.all_objects.filter(
                student=student, status=PaymentStatus.PENDING
            ).values_list("amount", flat=True),
            ZERO,
        )
        owed = max(fees.position_for(student).total_owed - pending, ZERO)
        if amount > owed:
            raise WouldOverpay(owed, amount)

    payment = Payment(
        school_id=student.school_id,
        branch_id=student.branch_id,
        transaction=txn,
        student=student,
        term=term,
        amount=amount,
        date_paid=txn.date_paid,
        label=label,
        method=txn.method,
        reference=txn.reference,
        source=txn.source,
        status=txn.status,
        note=note,
        recorded_by=actor,
    )
    if txn.status == PaymentStatus.CONFIRMED:
        payment.confirmed_by = actor
    payment.save()
    return payment


@transaction.atomic
def confirm(txn: PaymentTransaction, *, actor=None) -> PaymentTransaction:
    """Accept pending money. Its shares start counting in the same moment."""
    txn = _locked(txn)
    if txn.status == PaymentStatus.VOID:
        raise AllocationError("This payment has been voided.")
    if txn.status == PaymentStatus.CONFIRMED:
        return txn
    for payment in txn.shares().filter(status=PaymentStatus.PENDING):
        payment.confirm(by=actor)
    txn.status = PaymentStatus.CONFIRMED
    txn.confirmed_by = actor
    txn.confirmed_at = timezone.now()
    txn.save()
    return txn


def _reason(reason: str) -> str:
    reason = (reason or "").strip()
    if not reason:
        raise AllocationError("Say why, so the record explains itself later.")
    return reason


@transaction.atomic
def void_allocation(payment: Payment, *, actor=None, reason: str) -> Payment:
    """Take back one child's share. The money returns to unallocated."""
    reason = _reason(reason)
    if payment.transaction_id:
        _locked(payment.transaction)
    payment = Payment.all_objects.get(pk=payment.pk)
    if payment.status == PaymentStatus.VOID:
        return payment
    return payment.void(by=actor, reason=reason)


@transaction.atomic
def reallocate(payment: Payment, to_student, *, actor=None, reason: str,
               allow_credit=False) -> Payment:
    """Move a share to another child: a logged void and a new allocation, or neither."""
    amount = payment.amount
    txn = payment.transaction
    label = payment.label
    void_allocation(payment, actor=actor, reason=reason)
    return allocate(
        txn, to_student, amount, actor=actor, label=label,
        note=f"Moved from {payment.receipt_number}.", allow_credit=allow_credit,
    )


@transaction.atomic
def void_transaction(txn: PaymentTransaction, *, actor=None, reason: str) -> PaymentTransaction:
    """The whole sum was wrong, such as a bounced cheque. Every share goes with it."""
    reason = _reason(reason)
    txn = _locked(txn)
    if txn.status == PaymentStatus.VOID:
        return txn
    for payment in txn.shares().exclude(status=PaymentStatus.VOID):
        payment.void(by=actor, reason=reason)
    txn.status = PaymentStatus.VOID
    txn.voided_by = actor
    txn.voided_at = timezone.now()
    txn.void_reason = reason[:250]
    txn.save()
    return txn
