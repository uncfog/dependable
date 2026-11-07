"""A family's position: its children's positions added up, and nothing stored.

One child's credit is deliberately not set against a sibling's debt. That money was
given to that child, and moving it is a logged decision somebody makes, not arithmetic.
Money received but not yet shared out sits beside the total for the same reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from apps.payments.models import PaymentStatus, PaymentTransaction
from apps.students import fees
from apps.students.models import Student

from .models import FamilyMembership

ZERO = Decimal("0")


@dataclass(frozen=True)
class ChildRow:
    membership: FamilyMembership
    student: Student
    position: fees.FeePosition


@dataclass(frozen=True)
class FamilySummary:
    children: tuple[ChildRow, ...]
    unallocated: Decimal
    #: A child this account cannot see is here. Says that much and no more.
    has_children_elsewhere: bool

    @property
    def total_owed(self) -> Decimal:
        return sum((row.position.total_owed for row in self.children), ZERO)

    @property
    def total_credit(self) -> Decimal:
        return sum((row.position.credit for row in self.children), ZERO)


def summary_for(family) -> FamilySummary:
    """What the caller may see of this family. Scoped by the tenant managers."""
    memberships = list(
        FamilyMembership.objects.filter(family=family, ended_at__isnull=True)
    )
    students = list(
        Student.objects.filter(pk__in=[m.student_id for m in memberships]).select_related(
            "school_class", "branch"
        )
    )
    by_id = {student.pk: student for student in students}
    schedule = fees.load(students)
    rows = tuple(
        ChildRow(m, by_id[m.student_id], schedule.position_for(by_id[m.student_id]))
        for m in memberships
        if m.student_id in by_id
    )

    everyone = FamilyMembership.all_objects.filter(
        family=family, ended_at__isnull=True
    ).count()
    return FamilySummary(
        children=rows,
        unallocated=_unallocated(family),
        has_children_elsewhere=everyone > len(rows),
    )


def _unallocated(family) -> Decimal:
    received = PaymentTransaction.objects.filter(family=family, status=PaymentStatus.CONFIRMED)
    return sum((txn.unallocated for txn in received), ZERO)
