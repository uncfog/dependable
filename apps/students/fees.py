"""A student's fee position, derived rather than stored.

What a student owes this term is the total of their class's ``FeeStructure``
for the term their branch is currently in, plus any adjustments to their own bill.
What they owe from before that is every earlier term on their ledger, added up.
That is all facts about classes, terms and payments, so it lives on those models;
the student just points at them. Nothing here writes anything.

The reason this module exists at all, rather than a property on ``Student``, is
the list screen: rendering fifty students must not mean fifty structure lookups.
:class:`FeeSchedule` loads the term and the class totals in two queries and then
answers for any number of students in memory.

``paid`` comes from the payments app when it is installed, and is zero when it
is not -- which is honest rather than broken: nothing has been recorded, so
every priced student reads as unpaid. The lookup goes through
:func:`_paid_amounts`, which resolves the payments app lazily. A hard import
would make the roster unrenderable until payments ships, and this module is
older than that app by several branches.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable

from django.apps import apps as django_apps
from django.db.models import Sum
from django.utils import timezone

from apps.fees.models import (
    FeeComponent,
    FeeStructure,
    StudentFeeAdjustment,
    StudentTermEnrolment,
    Term,
)

ZERO = Decimal("0")


class FeeState:
    """The states a student's fees can be in on screen.

    ``UNPRICED`` is not a payment state -- it means nobody has set fees for that
    class this term, which is a setup gap the school needs to see rather than a
    zero balance to be reassured by.

    ``OVERDUE`` is not a fifth position either: it is ``UNPAID`` or ``PARTIAL``
    after the term's due date has passed. A term with no due date set never
    produces it, because a deadline nobody stated is not one a parent can have
    missed.
    """

    UNPRICED = "unpriced"
    PAID = "paid"
    PARTIAL = "partial"
    UNPAID = "unpaid"
    OVERDUE = "overdue"


#: Design-system pill class and label for each state -- the four payment-status
#: colours from the token set, and nothing invented alongside them.
STATE_DISPLAY = {
    FeeState.UNPRICED: ("status-unpaid", "No fees set"),
    FeeState.PAID: ("status-paid", "Paid"),
    FeeState.PARTIAL: ("status-partial", "Part paid"),
    FeeState.UNPAID: ("status-unpaid", "Unpaid"),
    FeeState.OVERDUE: ("status-overdue", "Overdue"),
}


@dataclass(frozen=True)
class FeePosition:
    """Where one student stands against one term's fees."""

    term: Term | None
    structure: FeeStructure | None
    #: This term's class fees plus this child's adjustments. Never below nothing.
    expected: Decimal = ZERO
    #: Confirmed payments credited to this term.
    paid: Decimal = ZERO
    #: This child's adjustments for this term, signed. Included in ``expected``.
    adjustment: Decimal = ZERO
    #: The balance carried from earlier terms: positive if owed, negative if credit.
    arrears: Decimal = ZERO

    @property
    def is_priced(self) -> bool:
        return self.structure is not None or self.adjustment != ZERO

    @property
    def adjustment_size(self) -> Decimal:
        """For display beside its own sign: "−₦5,000" rather than "₦-5,000"."""
        return abs(self.adjustment)

    # Money goes to the oldest debt first, so arrears are settled before this term.
    # Everything below follows from that one rule, and with no arrears these are the
    # plain figures: `outstanding` is expected less paid.

    @property
    def _available(self) -> Decimal:
        """This term's money, plus any credit brought forward."""
        return self.paid + max(-self.arrears, ZERO)

    @property
    def arrears_outstanding(self) -> Decimal:
        """What is still owed from earlier terms, after this term's money."""
        return max(max(self.arrears, ZERO) - self._available, ZERO)

    @property
    def _toward_this_term(self) -> Decimal:
        return max(self._available - max(self.arrears, ZERO), ZERO)

    @property
    def outstanding(self) -> Decimal:
        """What is owed for this term. Never negative: see :attr:`credit`."""
        return max(self.expected - self._toward_this_term, ZERO)

    @property
    def credit(self) -> Decimal:
        """Money beyond everything owed so far."""
        return max(self._toward_this_term - self.expected, ZERO)

    @property
    def total_owed(self) -> Decimal:
        """Everything this child owes, earlier terms and this one."""
        return self.arrears_outstanding + self.outstanding

    @property
    def due_date(self):
        """When this term's fees were expected, or ``None`` if never stated."""
        return getattr(self.term, "due_date", None)

    @property
    def is_overdue(self) -> bool:
        """Something is owed from a finished term, or past the school's own deadline."""
        if self.arrears_outstanding > ZERO:
            return True
        if self.outstanding <= ZERO or self.due_date is None:
            return False
        return timezone.localdate() > self.due_date

    @property
    def state(self) -> str:
        if not self.is_priced and self.arrears_outstanding <= ZERO:
            return FeeState.UNPRICED
        if self.total_owed <= ZERO:
            return FeeState.PAID
        # Overdue outranks the amount-based states: a bursar looking at this
        # column needs the deadline to be the thing that stands out.
        if self.is_overdue:
            return FeeState.OVERDUE
        if self._toward_this_term > ZERO:
            return FeeState.PARTIAL
        return FeeState.UNPAID

    @property
    def pill_class(self) -> str:
        return STATE_DISPLAY[self.state][0]

    @property
    def pill_label(self) -> str:
        return STATE_DISPLAY[self.state][1]


@dataclass(frozen=True)
class TermLine:
    """One term on a child's statement."""

    term: Term
    school_class: object
    fees: Decimal
    adjustments: tuple = ()
    paid: Decimal = ZERO
    is_current: bool = False

    @property
    def adjustment_total(self) -> Decimal:
        return sum((a.signed_amount for a in self.adjustments), ZERO)

    @property
    def charged(self) -> Decimal:
        """Fees and adjustments together. A term never charges less than nothing."""
        return max(self.fees + self.adjustment_total, ZERO)

    @property
    def balance(self) -> Decimal:
        """Positive if owed, negative if overpaid."""
        return self.charged - self.paid

    @property
    def owing(self) -> Decimal:
        return max(self.balance, ZERO)

    @property
    def overpaid(self) -> Decimal:
        return max(-self.balance, ZERO)


@dataclass
class FeeSchedule:
    """Every current term and class total the caller needs, loaded once.

    Built by :func:`load`, then asked ``position_for(student)`` as many times as
    there are rows on the page.
    """

    #: branch_id -> the term that branch is currently in.
    terms: dict[int, Term] = field(default_factory=dict)
    #: class_id -> its structure for that term. Absent means "not priced".
    structures: dict[int, FeeStructure] = field(default_factory=dict)
    #: class_id -> sum of the structure's components.
    totals: dict[int, Decimal] = field(default_factory=dict)
    #: student_id -> amount paid. The seam the payments layer plugs into.
    payments: dict[int, Decimal] = field(default_factory=dict)
    #: student_id -> their earlier terms, newest first.
    past_lines: dict[int, list[TermLine]] = field(default_factory=dict)
    #: student_id -> their adjustments for the current term, void ones included.
    current_adjustments: dict[int, tuple] = field(default_factory=dict)

    def term_for(self, student) -> Term | None:
        return self.terms.get(student.branch_id)

    def _class_fees(self, student, term) -> tuple[FeeStructure | None, Decimal]:
        structure = self.structures.get(student.school_class_id)
        if structure is None or structure.term_id != term.pk:
            # A structure from a different term is not this student's position.
            return None, ZERO
        return structure, self.totals.get(student.school_class_id, ZERO)

    def position_for(self, student) -> FeePosition:
        term = self.term_for(student)
        if term is None:
            return FeePosition(term=None, structure=None)
        structure, class_fees = self._class_fees(student, term)
        adjustment = sum(
            (a.signed_amount for a in self.current_adjustments.get(student.pk, ())), ZERO
        )
        arrears = sum(
            (line.balance for line in self.past_lines.get(student.pk, ())), ZERO
        )
        # Money paid into a class with no structure stays out of this term's position,
        # as it always has, unless there is a bill for it to go against.
        billed = structure is not None or adjustment != ZERO or arrears != ZERO
        return FeePosition(
            term=term,
            structure=structure,
            expected=max(class_fees + adjustment, ZERO),
            paid=self.payments.get(student.pk, ZERO) if billed else ZERO,
            adjustment=adjustment,
            arrears=arrears,
        )

    def statement_for(self, student) -> list[TermLine]:
        """Every term on this child's ledger, newest first."""
        lines = list(self.past_lines.get(student.pk, ()))
        term = self.term_for(student)
        if term is not None:
            _, class_fees = self._class_fees(student, term)
            lines.insert(
                0,
                TermLine(
                    term=term,
                    school_class=student.school_class,
                    fees=class_fees,
                    adjustments=self.current_adjustments.get(student.pk, ()),
                    paid=self.payments.get(student.pk, ZERO),
                    is_current=True,
                ),
            )
        return lines


def load(
    students: Iterable, *, payments: dict[int, Decimal] | None = None
) -> FeeSchedule:
    """Build the schedule covering ``students``, in two queries.

    Both querysets go through tenant-scoped managers, so a caller can never
    assemble a position out of another tenant's pricing.
    """
    students = list(students)
    if not students:
        return FeeSchedule()

    branch_ids = {s.branch_id for s in students if s.branch_id}
    class_ids = {s.school_class_id for s in students if s.school_class_id}

    terms = {
        term.branch_id: term
        for term in Term.objects.filter(is_current=True, branch_id__in=branch_ids)
    }
    if not terms:
        return FeeSchedule()

    structures = {
        structure.school_class_id: structure
        for structure in FeeStructure.objects.filter(
            term__in=terms.values(), school_class_id__in=class_ids
        ).select_related("term")
    }

    # One aggregate over the line items rather than FeeStructure.total per row:
    # the property is cheap with a prefetch, but a page of fifty students
    # touching twelve classes should still be one query, not twelve.
    totals = {
        row["fee_structure__school_class_id"]: row["amount"] or ZERO
        for row in FeeComponent.objects.filter(
            fee_structure_id__in=[s.pk for s in structures.values()]
        )
        .values("fee_structure__school_class_id")
        .annotate(amount=Sum("amount"))
    }

    if payments is None:
        payments = _paid_amounts(students, terms)

    past_lines, current_adjustments = _ledger(students, terms)

    return FeeSchedule(
        terms=terms,
        structures=structures,
        totals=totals,
        payments=payments,
        past_lines=past_lines,
        current_adjustments=current_adjustments,
    )


def _ledger(students, terms: dict) -> tuple[dict, dict]:
    """Each child's earlier terms, and their adjustments for this one.

    A fixed handful of queries for any number of children. An earlier term counts
    for a child only if it has an enrolment or an adjustment on record, so history
    nobody can vouch for neither adds a debt nor turns old payments into a credit.

    Through ``all_objects``, filtered to these students: the caller has already been
    allowed to see them, and a child's balance should not read differently depending
    on which campus is looking if they have moved between campuses.
    """
    current = {
        student.pk: terms[student.branch_id]
        for student in students
        if student.branch_id in terms
    }
    if not current:
        return {}, {}
    ids = list(current)

    adjustments: dict[tuple, list] = {}
    for row in StudentFeeAdjustment.all_objects.filter(student_id__in=ids).select_related(
        "term"
    ):
        adjustments.setdefault((row.student_id, row.term_id), []).append(row)

    # student -> term_id -> (term, class it was billed through, or None)
    on_ledger: dict[int, dict] = {}
    for row in StudentTermEnrolment.all_objects.filter(student_id__in=ids).select_related(
        "term", "school_class"
    ):
        if row.term.sort_key < current[row.student_id].sort_key:
            on_ledger.setdefault(row.student_id, {})[row.term_id] = (row.term, row.school_class)
    for (student_id, term_id), rows in adjustments.items():
        term = rows[0].term
        if term.sort_key < current[student_id].sort_key:
            on_ledger.setdefault(student_id, {}).setdefault(term_id, (term, None))

    current_adjustments = {
        student_id: tuple(adjustments.get((student_id, term.pk), ()))
        for student_id, term in current.items()
    }
    if not on_ledger:
        return {}, current_adjustments

    term_ids = {term_id for per in on_ledger.values() for term_id in per}
    class_ids = {
        klass.pk for per in on_ledger.values() for _, klass in per.values() if klass
    }
    fees_due = {
        (row["fee_structure__term_id"], row["fee_structure__school_class_id"]): row["amount"]
        for row in FeeComponent.all_objects.filter(
            fee_structure__term_id__in=term_ids,
            fee_structure__school_class_id__in=class_ids,
        )
        .values("fee_structure__term_id", "fee_structure__school_class_id")
        .annotate(amount=Sum("amount"))
    }
    paid = _paid_in_terms(list(on_ledger), term_ids)

    past_lines = {}
    for student_id, per in on_ledger.items():
        lines = [
            TermLine(
                term=term,
                school_class=klass,
                fees=fees_due.get((term_id, klass.pk), ZERO) if klass else ZERO,
                adjustments=tuple(adjustments.get((student_id, term_id), ())),
                paid=paid.get((student_id, term_id), ZERO),
            )
            for term_id, (term, klass) in per.items()
        ]
        lines.sort(key=lambda line: line.term.sort_key, reverse=True)
        past_lines[student_id] = lines
    return past_lines, current_adjustments


def _paid_in_terms(student_ids, term_ids) -> dict[tuple, Decimal]:
    """Confirmed money per ``(student, term)``, for terms other than the current one."""
    if not django_apps.is_installed("apps.payments"):
        return {}
    from apps.payments.models import Payment, PaymentStatus

    return {
        (row["student_id"], row["term_id"]): row["amount"]
        for row in Payment.all_objects.filter(
            status=PaymentStatus.CONFIRMED,
            student_id__in=student_ids,
            term_id__in=term_ids,
        )
        .values("student_id", "term_id")
        .annotate(amount=Sum("amount"))
    }


def _paid_amounts(students, terms: dict) -> dict[int, Decimal]:
    """Confirmed payments per student, for the term their branch is in.

    Resolved through the app registry rather than an import, so this module has
    no dependency on the payments app and keeps working when it is absent. The
    payments layer owns the definition of what counts -- confirmed only, this
    term only -- and this file does not second-guess it.
    """
    if not django_apps.is_installed("apps.payments"):
        return {}
    from apps.payments.balances import paid_by_student

    return paid_by_student(students, terms)


def position_for(student) -> FeePosition:
    """The fee position of a single student -- the detail screen's entry point."""
    return load([student]).position_for(student)


def statement_for(student) -> list[TermLine]:
    """Every term on a child's ledger, newest first."""
    return load([student]).statement_for(student)
