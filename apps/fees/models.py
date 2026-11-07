"""Fee structures: what each class owes per term.

The academic-finance data that payments will later be recorded against. Three
models, all branch-owned so they inherit tenant scoping:

    Term  --<  FeeStructure  --<  FeeComponent

A structure's total is always the sum of its components. It is never stored --
a stored total is a second source of truth that drifts the first time someone
edits a line item, and reconciling a payment against a stale figure is the kind
of bug that costs a school real money.
"""

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models, transaction
from django.utils import timezone

from apps.core.models import BranchScopedModel


class TermSequence(models.IntegerChoices):
    FIRST = 1, "First Term"
    SECOND = 2, "Second Term"
    THIRD = 3, "Third Term"


class Term(BranchScopedModel):
    """One term of an academic year at a branch, e.g. "First Term 2025/2026"."""

    branch = models.ForeignKey(
        "schools.Branch", on_delete=models.CASCADE, related_name="terms"
    )
    name = models.CharField(max_length=120, help_text='e.g. "First Term 2025/2026".')
    academic_year = models.CharField(
        max_length=20, help_text='e.g. "2025/2026".'
    )
    sequence = models.PositiveSmallIntegerField(
        choices=TermSequence.choices, default=TermSequence.FIRST
    )
    is_current = models.BooleanField(
        default=False,
        help_text="The term the school is in now. Only one per branch; setting "
        "this clears it from any other term.",
    )
    #: The date fees for this term are expected by. Nullable, because a school
    #: that has not set one has not set one -- and inventing a deadline would
    #: turn every unpaid student on the platform red overnight. A balance only
    #: reads as overdue once the school says when it was due.
    due_date = models.DateField(
        "fees due by",
        null=True,
        blank=True,
        help_text="Optional. Unpaid balances are only marked overdue after "
        "this date.",
    )

    class Meta(BranchScopedModel.Meta):
        abstract = False
        # Newest year first, then in term order inside the year.
        ordering = ["-academic_year", "sequence"]
        constraints = [
            models.UniqueConstraint(
                fields=["branch", "academic_year", "sequence"],
                name="unique_term_per_branch_year",
            ),
            # Enforced in the database, not just in save(), so a stray script
            # cannot leave a branch with two "current" terms.
            models.UniqueConstraint(
                fields=["branch"],
                condition=models.Q(is_current=True),
                name="one_current_term_per_branch",
            ),
        ]
        indexes = [
            models.Index(fields=["school", "branch"]),
            models.Index(fields=["branch", "academic_year", "sequence"]),
        ]

    def __str__(self) -> str:
        return self.name

    @property
    def sort_key(self) -> tuple:
        """Where this term falls in time. Years like "2025/2026" sort as text."""
        return (self.academic_year, self.sequence)

    @property
    def is_past(self) -> bool:
        """Whether this campus has moved on to a later term.

        Derived rather than a stored "closed" flag, so moving the current term is
        the only thing a school has to do. A past term's fees are locked, because
        every child's balance for it is read from them: repricing last term would
        quietly change what families owe.
        """
        current = Term.all_objects.filter(branch_id=self.branch_id, is_current=True).first()
        return current is not None and self.sort_key < current.sort_key

    def save(self, *args, **kwargs):
        with transaction.atomic():
            if self.is_current and self.branch_id:
                # Stand the others down first, or the partial unique constraint
                # rejects this row.
                Term.all_objects.filter(
                    branch_id=self.branch_id, is_current=True
                ).exclude(pk=self.pk).update(is_current=False)
            super().save(*args, **kwargs)


def not_past(terms):
    """``terms`` without any whose campus has moved on. One query however many."""
    current = {
        term.branch_id: term.sort_key
        for term in Term.all_objects.filter(is_current=True)
    }
    past = [
        term.pk
        for term in terms
        if term.branch_id in current and term.sort_key < current[term.branch_id]
    ]
    return terms.exclude(pk__in=past)


class FeeStructure(BranchScopedModel):
    """The fee set for one class in one term."""

    branch = models.ForeignKey(
        "schools.Branch", on_delete=models.CASCADE, related_name="fee_structures"
    )
    # `class` is a Python keyword, so the field carries the longer name and the
    # verbose name keeps the domain language in the UI.
    school_class = models.ForeignKey(
        "academics.Class",
        on_delete=models.CASCADE,
        related_name="fee_structures",
        verbose_name="class",
    )
    term = models.ForeignKey(Term, on_delete=models.CASCADE, related_name="fee_structures")

    class Meta(BranchScopedModel.Meta):
        abstract = False
        ordering = ["school_class__level", "school_class__year_in_level",
                    "school_class__stream"]
        constraints = [
            models.UniqueConstraint(
                fields=["school_class", "term"],
                name="one_fee_structure_per_class_per_term",
            )
        ]
        indexes = [
            models.Index(fields=["school", "branch"]),
            models.Index(fields=["term", "school_class"]),
        ]

    def __str__(self) -> str:
        return f"{self.school_class} - {self.term}"

    @property
    def total(self) -> Decimal:
        """Sum of the line items. Computed every time, stored never.

        Cheap when the caller has ``prefetch_related("components")``, which the
        list views do.
        """
        return sum((c.amount for c in self.components.all()), Decimal("0"))

    def save(self, *args, **kwargs):
        if self.school_class_id and self.branch_id is None:
            self.branch_id = self.school_class.branch_id
        super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        if self.school_class_id and self.term_id:
            if self.school_class.branch_id != self.term.branch_id:
                raise ValidationError(
                    {"term": "That term belongs to a different branch than the class."}
                )


class FeeComponent(BranchScopedModel):
    """One line item of a fee structure, e.g. "Textbooks" at 50,000."""

    branch = models.ForeignKey(
        "schools.Branch", on_delete=models.CASCADE, related_name="fee_components"
    )
    fee_structure = models.ForeignKey(
        FeeStructure, on_delete=models.CASCADE, related_name="components"
    )
    name = models.CharField(max_length=120)
    amount = models.DecimalField(
        max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0"))]
    )
    position = models.PositiveSmallIntegerField(
        default=0, help_text="Display order within the structure."
    )

    class Meta(BranchScopedModel.Meta):
        abstract = False
        ordering = ["position", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["fee_structure", "name"],
                name="unique_component_name_per_structure",
            )
        ]
        indexes = [
            models.Index(fields=["school", "branch"]),
            models.Index(fields=["fee_structure", "position"]),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.amount})"

    def save(self, *args, **kwargs):
        if self.fee_structure_id and self.branch_id is None:
            self.branch_id = self.fee_structure.branch_id
        super().save(*args, **kwargs)


class StudentTermEnrolment(BranchScopedModel):
    """Which class a child was in for a term, and that the term counts for them.

    ``Student.school_class`` only says where a child is now, so without this a
    promotion would reprice every term they had ever attended. It is also what puts
    a term on a child's ledger: an earlier term with no row here adds nothing to
    their balance, because nobody can vouch for it. That is what stops any existing
    figure moving when this ships.

    Kept up to date by :mod:`apps.fees.enrolment`. A row is only ever changed while
    its term is the current one.
    """

    branch = models.ForeignKey(
        "schools.Branch", on_delete=models.CASCADE, related_name="term_enrolments"
    )
    student = models.ForeignKey(
        "students.Student", on_delete=models.CASCADE, related_name="term_enrolments"
    )
    term = models.ForeignKey(Term, on_delete=models.CASCADE, related_name="enrolments")
    #: Protected: a class a child was billed through is part of that bill's history.
    school_class = models.ForeignKey(
        "academics.Class",
        on_delete=models.PROTECT,
        related_name="term_enrolments",
        verbose_name="class",
    )

    class Meta(BranchScopedModel.Meta):
        abstract = False
        constraints = [
            models.UniqueConstraint(
                fields=["student", "term"], name="one_enrolment_per_student_per_term"
            )
        ]
        indexes = [
            models.Index(fields=["school", "branch"]),
            models.Index(fields=["term", "school_class"]),
        ]

    def __str__(self) -> str:
        return f"{self.student_id} in {self.school_class_id} for {self.term_id}"


class AdjustmentKind(models.TextChoices):
    """Why one child's bill differs from their class's."""

    OPENING_BALANCE = "opening_balance", "Opening balance"
    CHARGE = "charge", "Extra charge"
    DISCOUNT = "discount", "Discount"
    SCHOLARSHIP = "scholarship", "Scholarship"
    WAIVER = "waiver", "Waiver"


#: The kinds that reduce what a child owes. The amount itself is always positive.
REDUCING_KINDS = frozenset(
    {AdjustmentKind.DISCOUNT, AdjustmentKind.SCHOLARSHIP, AdjustmentKind.WAIVER}
)


class StudentFeeAdjustment(BranchScopedModel):
    """One change to one child's bill for one term.

    Money on a record: corrected by voiding it and entering the right one, never by
    editing, and it keeps the student and the term from being deleted underneath it.
    Every change is in the audit log.
    """

    branch = models.ForeignKey(
        "schools.Branch", on_delete=models.CASCADE, related_name="fee_adjustments"
    )
    student = models.ForeignKey(
        "students.Student", on_delete=models.PROTECT, related_name="fee_adjustments"
    )
    term = models.ForeignKey(Term, on_delete=models.PROTECT, related_name="adjustments")
    kind = models.CharField(max_length=20, choices=AdjustmentKind.choices)
    amount = models.DecimalField(
        max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))]
    )
    description = models.CharField(
        "reason", max_length=250, help_text="Why. Someone will ask in a year's time."
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="fee_adjustments_created",
    )
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="fee_adjustments_voided",
    )
    voided_at = models.DateTimeField(null=True, blank=True)
    void_reason = models.CharField(max_length=250, blank=True)

    class Meta(BranchScopedModel.Meta):
        abstract = False
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["school", "branch"]),
            models.Index(fields=["student", "term"]),
        ]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} {self.amount}"

    @property
    def is_void(self) -> bool:
        return self.voided_at is not None

    @property
    def signed_amount(self) -> Decimal:
        """What this does to the bill. A void one does nothing."""
        if self.is_void:
            return Decimal("0")
        return -self.amount if self.kind in REDUCING_KINDS else self.amount

    def save(self, *args, **kwargs):
        if self.student_id and self.branch_id is None:
            self.branch_id = self.student.branch_id
        super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        if self.student_id and self.term_id:
            if self.student.branch_id != self.term.branch_id:
                raise ValidationError(
                    {"term": "That term belongs to a different campus than the student."}
                )

    def void(self, *, by=None, reason: str = "") -> "StudentFeeAdjustment":
        self.voided_by = by
        self.voided_at = timezone.now()
        self.void_reason = reason.strip()[:250]
        self.save(update_fields=["voided_by", "voided_at", "void_reason", "updated_at"])
        return self
