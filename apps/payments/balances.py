"""What a student has actually paid, and therefore what they still owe.

Everything here is derived. Nothing is stored, nothing is cached on a student,
and no screen is allowed to add up its own version:

    expected  = the class's FeeStructure total for the term  (apps.fees)
    paid      = the sum of that student's CONFIRMED payments for the term
    balance   = expected - paid
    status    = paid / partial / unpaid, or overdue past the term's due date

:func:`paid_by_student` is the seam ``apps.students.fees`` reaches for through
the app registry. Because it is the single definition of "paid", a pending
receipt cannot leak into a balance from one screen and not another, and voiding
a payment moves every figure on the platform at once.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from django.db.models import Q, QuerySet, Sum

from .models import Payment, PaymentStatus

ZERO = Decimal("0")


def confirmed() -> QuerySet[Payment]:
    """Payments that count. The one place this rule is written down.

    Tenant-scoped via ``Payment.objects``, so a balance can never be assembled
    out of another school's money.
    """
    return Payment.objects.filter(status=PaymentStatus.CONFIRMED)


def paid_by_student(students, terms: dict) -> dict[int, Decimal]:
    """``{student_id: amount confirmed}`` for each student's own current term.

    One query for any number of students. ``terms`` maps ``branch_id`` to the
    term that branch is currently in -- passed in rather than looked up again,
    because the caller has already resolved it and the two must agree.

    A student is only credited with payments made against *their* branch's
    current term: a school owner listing two campuses mid-transition can have
    two different terms in flight, and crediting one campus's payments against
    the other's term would quietly overstate collections.
    """
    students = list(students)
    if not students or not terms:
        return {}

    wanted = {
        student.pk: terms[student.branch_id].pk
        for student in students
        if student.branch_id in terms
    }
    if not wanted:
        return {}

    rows = (
        confirmed()
        .filter(student_id__in=wanted, term_id__in=set(wanted.values()))
        .values("student_id", "term_id")
        .annotate(amount=Sum("amount"))
    )
    return {
        row["student_id"]: row["amount"] or ZERO
        for row in rows
        if wanted.get(row["student_id"]) == row["term_id"]
    }


def paid_for(student, term) -> Decimal:
    """Confirmed total for one student in one term -- the detail screen's ask."""
    if student is None or term is None:
        return ZERO
    total = (
        confirmed()
        .filter(student=student, term=term)
        .aggregate(amount=Sum("amount"))["amount"]
    )
    return total or ZERO


@dataclass(frozen=True)
class OutstandingRow:
    """One line of the outstanding-balances screen."""

    student: object
    position: object

    @property
    def owed(self) -> Decimal:
        """Everything this child owes, earlier terms included."""
        return self.position.total_owed


def outstanding(students=None) -> list[OutstandingRow]:
    """Students who still owe something, most owed first.

    The list the bursar chases and the list messaging's "parents who owe"
    resolves to are the same derivation, so the screen and the reminder can
    never name different families.
    """
    from apps.students import fees
    from apps.students.models import Student, StudentStatus

    if students is None:
        students = Student.objects.filter(
            status=StudentStatus.ACTIVE
        ).select_related("school_class", "branch")
    students = list(students)
    schedule = fees.load(students)

    rows = [
        OutstandingRow(student=student, position=schedule.position_for(student))
        for student in students
    ]
    rows = [row for row in rows if row.owed > ZERO]
    rows.sort(key=lambda row: row.owed, reverse=True)
    return rows


def running_balance(payment: Payment) -> Decimal:
    """What was still owed immediately after ``payment`` was taken.

    Printed on the receipt, so a parent walking away from the bursary window
    knows where they stand. Counts confirmed payments for the same student and
    term up to and including this one, ordered the way a ledger reads: by date,
    then by the order they were recorded.
    """
    from apps.students import fees

    position = fees.position_for(payment.student)
    if position.term is None or not (position.is_priced or position.arrears):
        return ZERO

    taken = (
        confirmed()
        .filter(student_id=payment.student_id, term_id=payment.term_id)
        .filter(_up_to(payment))
        .aggregate(amount=Sum("amount"))["amount"]
        or ZERO
    )
    return max(position.arrears + position.expected - taken, ZERO)


def _up_to(payment: Payment) -> Q:
    """Payments at or before ``payment`` in ledger order.

    Same-day payments are broken by id, which is the order they were entered --
    two transfers received on one Monday still have a first and a second, and
    the receipt for each has to show a different running balance.
    """
    return Q(date_paid__lt=payment.date_paid) | Q(
        date_paid=payment.date_paid, id__lte=payment.pk
    )
