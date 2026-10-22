"""One lump at enrolment, divided between the two ledgers it pays.

At a walk-in intake the parent pays everything at once: the admission fee, the
one-off items (uniform, sportswear, track suit, exam/dossier) and the first
term's fees. Those are two different kinds of money and the platform keeps them
in two different tables, for a reason worth keeping:

* intake money is an :class:`~apps.admissions.models.AdmissionPayment`, so a
  term's collection figure never quietly includes uniforms and admission fees;
* term money is a :class:`~apps.payments.models.Payment` against the student,
  because that is the only table a balance, a payment history or the
  dashboard's recent payments read. "Paid" keeps meaning one thing.

So the lump is *split*, not double-counted and not stored in one place and read
from two. The two rows are linked (``AdmissionPayment.term_payment``) and share
the term payment's receipt number, so the parent still holds one receipt for
the one sum they handed over.

**The allocation order is the client's**: admission fee first, then the term's
fees, then the one-off intake items. The admission fee is first because
enrolment is gated on it; the term is next because an unpaid term is what the
school chases. Anything paid beyond the whole bill stays on the intake side as
a credit, so an overpayment can never inflate a term's collections.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.fees.models import FeeComponent, FeeStructure, Term

from .models import (
    AdmissionFeeKind,
    AdmissionFeeSchedule,
    AdmissionPayment,
    AdmissionPaymentStatus,
)

ZERO = Decimal("0")


# ---------------------------------------------------------------------------
# What a new child owes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BillLine:
    name: str
    amount: Decimal


@dataclass(frozen=True)
class Bill:
    """A new child's bill: the intake one-offs plus the current term.

    Built from the two price lists, never stored. ``term`` is ``None`` when the
    campus has no current term, and ``term_lines`` is empty when the class has
    no fee structure for it -- in both cases the term part of the bill is
    unknown rather than zero, and :attr:`term_is_priced` says so.
    """

    schedule: AdmissionFeeSchedule | None
    term: Term | None
    admission_lines: tuple[BillLine, ...] = ()
    one_off_lines: tuple[BillLine, ...] = ()
    term_lines: tuple[BillLine, ...] = ()

    @property
    def admission_fee(self) -> Decimal:
        return sum((line.amount for line in self.admission_lines), ZERO)

    @property
    def one_offs(self) -> Decimal:
        return sum((line.amount for line in self.one_off_lines), ZERO)

    @property
    def intake_total(self) -> Decimal:
        return self.admission_fee + self.one_offs

    @property
    def term_fees(self) -> Decimal:
        return sum((line.amount for line in self.term_lines), ZERO)

    @property
    def term_is_priced(self) -> bool:
        return bool(self.term_lines)

    @property
    def total(self) -> Decimal:
        return self.intake_total + self.term_fees


def schedule_for(school_class) -> AdmissionFeeSchedule | None:
    """The class's intake sheet: the active one for the latest intake year."""
    if school_class is None:
        return None
    return (
        AdmissionFeeSchedule.all_objects.filter(school_class=school_class)
        .order_by("-is_active", "-academic_year")
        .prefetch_related("items")
        .first()
    )


def current_term_for(branch_id) -> Term | None:
    return Term.all_objects.filter(branch_id=branch_id, is_current=True).first()


def bill_for(school_class) -> Bill:
    """What a child joining ``school_class`` today owes, line by line."""
    schedule = schedule_for(school_class)
    admission_lines: list[BillLine] = []
    one_off_lines: list[BillLine] = []
    if schedule is not None:
        for item in schedule.items.all():
            target = (
                admission_lines
                if item.kind == AdmissionFeeKind.ADMISSION
                else one_off_lines
            )
            target.append(BillLine(item.name, item.amount))

    term = current_term_for(school_class.branch_id) if school_class else None
    term_lines: list[BillLine] = []
    if term is not None:
        structure = FeeStructure.all_objects.filter(
            school_class=school_class, term=term
        ).first()
        if structure is not None:
            term_lines = [
                BillLine(c.name, c.amount)
                for c in FeeComponent.all_objects.filter(fee_structure=structure)
            ]

    return Bill(
        schedule=schedule,
        term=term,
        admission_lines=tuple(admission_lines),
        one_off_lines=tuple(one_off_lines),
        term_lines=tuple(term_lines),
    )


# ---------------------------------------------------------------------------
# Dividing the money
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Allocation:
    """How one sum is divided. ``intake`` and ``term`` always add up to it."""

    admission_fee: Decimal = ZERO
    term_fees: Decimal = ZERO
    one_offs: Decimal = ZERO
    #: Paid beyond the whole bill. Kept on the intake side as a credit.
    excess: Decimal = ZERO

    @property
    def intake(self) -> Decimal:
        return self.admission_fee + self.one_offs + self.excess

    @property
    def term(self) -> Decimal:
        return self.term_fees

    @property
    def total(self) -> Decimal:
        return self.intake + self.term


def allocate(amount: Decimal, bill: Bill) -> Allocation:
    """Admission fee, then term fees, then one-off items, then credit.

    Pure arithmetic, no database: every caller's rounding and every edge case
    (a part payment, an overpayment, an unpriced class) is answered here once.
    """
    remaining = max(Decimal(amount), ZERO)

    def take(due: Decimal) -> Decimal:
        nonlocal remaining
        portion = min(remaining, max(due, ZERO))
        remaining -= portion
        return portion

    admission_fee = take(bill.admission_fee)
    term_fees = take(bill.term_fees if bill.term is not None else ZERO)
    one_offs = take(bill.one_offs)
    return Allocation(
        admission_fee=admission_fee,
        term_fees=term_fees,
        one_offs=one_offs,
        excess=remaining,
    )


# ---------------------------------------------------------------------------
# Writing the two rows
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Split:
    """What was written: either row may be absent when its portion is zero."""

    allocation: Allocation
    intake_payment: AdmissionPayment | None
    term_payment: "object | None"  # payments.Payment; typed loosely, see below

    @property
    def receipt_number(self) -> str:
        if self.term_payment is not None:
            return self.term_payment.receipt_number
        if self.intake_payment is not None:
            return self.intake_payment.receipt_number
        return ""


def _term_payment(*, student, term, amount, method, reference, date_paid, actor,
                  note):
    """The term portion, as an ordinary confirmed payment against the student.

    Imported here: payments is built on students and fees, admissions is an
    add-on over all three, and keeping the import local means admissions still
    loads in a project where payments is not installed.
    """
    from apps.payments.models import Payment, PaymentLabel, PaymentSource

    return Payment.all_objects.create(
        school_id=student.school_id,
        branch_id=student.branch_id,
        student=student,
        term=term,
        amount=amount,
        date_paid=date_paid,
        label=PaymentLabel.SCHOOL_FEES,
        method=method,
        reference=reference,
        source=PaymentSource.MANUAL,
        note=note[:250],
        recorded_by=actor,
        confirmed_by=actor,
    )


@transaction.atomic
def record(
    *,
    applicant,
    student,
    amount: Decimal,
    method: str,
    reference: str = "",
    received_on: date | None = None,
    actor=None,
    bill: Bill | None = None,
) -> Split:
    """Record one all-in payment for a child who is already on the roll.

    The walk-in path: the student exists (created in the same transaction by
    the caller), so the term portion can be credited straight away. Confirmed
    money only -- the desk has the cash or the slip in hand.
    """
    bill = bill or bill_for(student.school_class)
    allocation = allocate(amount, bill)
    received_on = received_on or timezone.localdate()

    term_payment = None
    if allocation.term > ZERO:
        term_payment = _term_payment(
            student=student,
            term=bill.term,
            amount=allocation.term,
            method=method,
            reference=reference,
            date_paid=received_on,
            actor=actor,
            note=f"Term portion of the enrolment payment ({applicant.reference}).",
        )

    intake_payment = None
    if allocation.intake > ZERO:
        intake_payment = AdmissionPayment.all_objects.create(
            school_id=applicant.school_id,
            branch_id=applicant.branch_id,
            applicant=applicant,
            amount=allocation.intake,
            method=method,
            status=AdmissionPaymentStatus.CONFIRMED,
            received_on=received_on,
            reference=reference,
            note=_intake_note(amount, term_payment),
            recorded_by=actor,
            term_payment=term_payment,
        )

    return Split(allocation, intake_payment, term_payment)


def _intake_note(total: Decimal, term_payment) -> str:
    if term_payment is None:
        return ""
    return (
        f"Enrolment payment of {total:,.0f}; {term_payment.amount:,.0f} "
        f"credited to term fees as {term_payment.receipt_number}."
    )


@transaction.atomic
def settle_at_enrolment(applicant, student, *, actor=None) -> Split | None:
    """Move the term share of money already paid at the door onto the student.

    The pipeline path: the family paid before there was a student to credit,
    so all of it was recorded as admission money. At conversion the same rule
    the walk-in screen uses decides how much of it was really this term's fees,
    and that much becomes a :class:`~apps.payments.models.Payment`.

    Only confirmed rows that have not been split before are considered. A
    pending receipt is not money yet; it stays admission money and can be
    recorded against the term by hand once it is checked.

    The rows the money is taken from are reduced, newest first. A row moved in
    full is voided rather than set to zero -- a payment that was recorded is a
    fact about the account, and its note says where the money went.
    """
    rows = list(
        AdmissionPayment.all_objects.filter(
            applicant=applicant,
            status=AdmissionPaymentStatus.CONFIRMED,
            term_payment__isnull=True,
        ).order_by("-received_on", "-id")
    )
    if not rows:
        return None

    total = sum((row.amount for row in rows), ZERO)
    bill = bill_for(student.school_class)
    allocation = allocate(total, bill)
    if allocation.term <= ZERO:
        return Split(allocation, None, None)

    latest = rows[0]
    term_payment = _term_payment(
        student=student,
        term=bill.term,
        amount=allocation.term,
        method=latest.method,
        reference=latest.reference,
        date_paid=latest.received_on,
        actor=actor,
        note=f"Term portion of admission payments on {applicant.reference}, "
             f"moved at enrolment.",
    )

    remaining = allocation.term
    for row in rows:
        if remaining <= ZERO:
            break
        moved = min(row.amount, remaining)
        remaining -= moved
        row.term_payment = term_payment
        moved_note = (
            f"{moved:,.0f} moved to term fees as {term_payment.receipt_number} "
            f"at enrolment."
        )
        if moved == row.amount:
            # Voided, not zeroed: the row's own amount still says what was
            # received on that day, and the status says it no longer counts here.
            row.status = AdmissionPaymentStatus.VOID
        else:
            row.amount -= moved
        row.note = f"{row.note} {moved_note}".strip()[:250]
        row.save(update_fields=["term_payment", "status", "amount", "note",
                                "updated_at"])

    intake_payment = next(
        (row for row in rows if row.status == AdmissionPaymentStatus.CONFIRMED
         and row.term_payment_id == term_payment.pk),
        None,
    )
    return Split(allocation, intake_payment, term_payment)


def intake_total_for(term_payment) -> Decimal:
    """Admission money paid in the same lump as ``term_payment``.

    What the receipt adds to the term portion to show the sum the parent
    actually handed over.
    """
    total = AdmissionPayment.all_objects.filter(
        term_payment=term_payment, status=AdmissionPaymentStatus.CONFIRMED
    ).aggregate(total=Sum("amount"))["total"]
    return total or ZERO
