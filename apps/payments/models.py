"""Money actually received: one row per payment, recorded against a student.

For the pilot this is manual. The school keeps banking into its own account and
the bursar records each payment here -- no gateway, no card, no settlement. The
model is nonetheless shaped so that an online gateway and an automatic bank
import can be added later without a rewrite: ``source`` already distinguishes
where a row came from, ``status`` already carries the pending → confirmed
reconciliation the client described, and the gateway reference fields already
exist and are simply null.

Three decisions worth stating, because they are the ones a later change is most
likely to get wrong:

**Balance is never stored.** Not on the student, not on the term, not here.
It is expected minus confirmed, derived in :mod:`apps.payments.balances` every
time it is asked. A stored balance is a second source of truth that goes stale
the first time a payment is voided or a class is repriced.

**``label`` is descriptive only.** The client asked for labelling, not for
per-component accounting: a payment is marked "Uniform" or "School Fees" from
the receipt or from what the parent said, and the balance stays one combined
figure. Splitting the balance per label would mean deciding what happens when a
parent pays 50,000 against a 30,000 uniform charge, and nobody has asked for
that answer.

**A voided payment is not deleted.** It keeps its row, stops counting toward
the balance, and records who voided it and why. Money that was recorded and
then reversed is a fact about the account; erasing it is how a ledger stops
being one.
"""

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models, transaction as db_transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.urls import reverse
from django.utils import timezone

from apps.core.models import BranchScopedModel

ZERO = Decimal("0")


class PaymentLabel(models.TextChoices):
    """What the money was for, as the bursar understood it.

    Descriptive. The balance does not track these separately -- see the module
    docstring.
    """

    SCHOOL_FEES = "school_fees", "School Fees"
    UNIFORM = "uniform", "Uniform"
    BOOKS = "books", "Books"
    DEVELOPMENT = "development", "Development"
    OTHER = "other", "Other"


class PaymentMethod(models.TextChoices):
    CASH = "cash", "Cash"
    TRANSFER = "transfer", "Bank transfer"
    POS = "pos", "POS"
    OTHER = "other", "Other"


class PaymentSource(models.TextChoices):
    """How the row got here.

    Every row is ``MANUAL`` today. The field exists now so that when a gateway
    or a bank statement import starts creating rows, the ones a human typed
    remain distinguishable from the ones a machine reconciled -- which is the
    first question anyone will ask when the two disagree.
    """

    MANUAL = "manual", "Recorded by staff"
    GATEWAY = "gateway", "Online payment"
    BANK_IMPORT = "bank_import", "Bank import"


class PaymentStatus(models.TextChoices):
    """Where a payment stands.

    ``PENDING`` is the receipt that has been handed in but not yet checked --
    the client's "someone uploads a receipt, the bursar confirms who and what
    it is for". Only ``CONFIRMED`` counts toward a balance.
    """

    PENDING = "pending", "Pending"
    CONFIRMED = "confirmed", "Confirmed"
    VOID = "void", "Voided"


#: Design-system pill class per status, reusing the four payment-status colours.
STATUS_PILLS = {
    PaymentStatus.PENDING: "status-partial",
    PaymentStatus.CONFIRMED: "status-paid",
    PaymentStatus.VOID: "status-unpaid",
}


def receipt_upload_to(instance: "Payment", filename: str) -> str:
    """Group uploads by tenant, so a school's receipts stay in one place."""
    return f"receipts/{instance.school_id or 'unassigned'}/{filename}"


class TransactionKind(models.TextChoices):
    #: One child's payment, in step with its single ``Payment`` row.
    INDIVIDUAL = "individual", "One child"
    #: Money shared across children by :mod:`apps.payments.allocation`.
    FAMILY = "family", "Shared"


class PaymentTransaction(BranchScopedModel):
    """Money actually received, once, however it is then shared out.

    **Balances never read this.** They read ``Payment`` rows, one per child, as they
    always have, so no figure can be counted twice and a family payment changes
    nothing about how a balance is worked out. This records the sum handed over, and
    what is left of it is simply what its allocations have not used.

    ``branch`` is the campus that took the money. Shared money is only changed through
    :mod:`apps.payments.allocation`, which holds the rule that allocations can never
    exceed the amount received.
    """

    branch = models.ForeignKey(
        "schools.Branch", on_delete=models.CASCADE, related_name="payment_transactions"
    )
    kind = models.CharField(
        max_length=20, choices=TransactionKind.choices, default=TransactionKind.INDIVIDUAL
    )
    family = models.ForeignKey(
        "families.Family", on_delete=models.PROTECT, null=True, blank=True,
        related_name="transactions",
    )
    amount = models.DecimalField(
        max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))]
    )
    date_paid = models.DateField(default=timezone.localdate)
    method = models.CharField(
        max_length=20, choices=PaymentMethod.choices, default=PaymentMethod.TRANSFER
    )
    reference = models.CharField(max_length=120, blank=True)
    payer_name = models.CharField(max_length=160, blank=True)
    source = models.CharField(
        max_length=20, choices=PaymentSource.choices, default=PaymentSource.MANUAL
    )
    status = models.CharField(
        max_length=20, choices=PaymentStatus.choices, default=PaymentStatus.CONFIRMED
    )
    note = models.CharField(max_length=250, blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="payment_transactions_recorded",
    )
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="payment_transactions_confirmed",
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="payment_transactions_voided",
    )
    voided_at = models.DateTimeField(null=True, blank=True)
    void_reason = models.CharField(max_length=250, blank=True)

    class Meta(BranchScopedModel.Meta):
        abstract = False
        ordering = ["-date_paid", "-id"]
        indexes = [
            models.Index(fields=["school", "branch"]),
            models.Index(fields=["family", "status"]),
            models.Index(fields=["kind", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.number} {self.amount}"

    @property
    def number(self) -> str:
        """Not a receipt number: those stay on each child's ``Payment``."""
        return f"TXN-{self.pk:06d}" if self.pk else "TXN-------"

    @property
    def is_shared(self) -> bool:
        return self.kind == TransactionKind.FAMILY

    def shares(self):
        """Every child's row, at every campus.

        Not ``self.allocations``, which only shows what the caller's campus can see.
        One campus can take money that a school owner shares with another, and the
        ceiling has to count all of it whoever is asking.
        """
        return Payment.all_objects.filter(transaction=self)

    @property
    def allocated(self) -> Decimal:
        """What its children's rows are using. A voided share gives its money back."""
        total = self.shares().exclude(status=PaymentStatus.VOID).aggregate(
            total=models.Sum("amount")
        )["total"]
        return total or ZERO

    @property
    def unallocated(self) -> Decimal:
        """What is left to share out. Worked out every time, stored never."""
        if self.status == PaymentStatus.VOID:
            return ZERO
        return self.amount - self.allocated


#: The fields a one-child transaction takes from its payment.
_MIRRORED = (
    "amount", "date_paid", "method", "reference", "source", "status", "note",
    "recorded_by_id", "confirmed_by_id", "confirmed_at", "voided_by_id", "voided_at",
    "void_reason",
)


class Payment(BranchScopedModel):
    """One payment received from (or on behalf of) one student.

    On a shared transaction, this is one child's portion of it.
    """

    branch = models.ForeignKey(
        "schools.Branch", on_delete=models.CASCADE, related_name="payments"
    )
    transaction = models.ForeignKey(
        PaymentTransaction,
        # A transaction with money shared out must not vanish from under it.
        on_delete=models.PROTECT,
        related_name="allocations",
        null=True,
        blank=True,
    )
    student = models.ForeignKey(
        "students.Student",
        # A student with money against their name must not vanish underneath
        # it: the roster would lose the person the payments were made for, and
        # the balance would silently stop being anybody's. The roster screens
        # offer "withdraw instead", which is what removal almost always means.
        on_delete=models.PROTECT,
        related_name="payments",
    )
    term = models.ForeignKey(
        "fees.Term",
        # A term with payments against it is history; deleting it would strand
        # them outside every balance.
        on_delete=models.PROTECT,
        related_name="payments",
        help_text="The term this payment is credited to.",
    )

    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    date_paid = models.DateField(
        default=timezone.localdate,
        help_text="When the money was received, not when it was typed in.",
    )
    label = models.CharField(
        max_length=20,
        choices=PaymentLabel.choices,
        default=PaymentLabel.SCHOOL_FEES,
        help_text="What the payment was for. Descriptive only — the balance is "
        "one combined figure.",
    )
    method = models.CharField(
        max_length=20, choices=PaymentMethod.choices, default=PaymentMethod.TRANSFER
    )
    reference = models.CharField(
        max_length=120,
        blank=True,
        help_text="Teller number, transfer reference, POS stub — whatever the "
        "receipt shows.",
    )
    source = models.CharField(
        max_length=20, choices=PaymentSource.choices, default=PaymentSource.MANUAL
    )
    status = models.CharField(
        max_length=20, choices=PaymentStatus.choices, default=PaymentStatus.CONFIRMED
    )
    receipt = models.FileField(
        "receipt attachment",
        upload_to=receipt_upload_to,
        null=True,
        blank=True,
        help_text="The parent's bank slip or POS receipt. Image or PDF.",
    )
    note = models.CharField(max_length=250, blank=True)

    # --- Future online payments ------------------------------------------
    # Null on every row today. They exist now so a gateway integration is a new
    # code path rather than a migration of the table every balance reads from.
    gateway_provider = models.CharField(max_length=40, blank=True)
    gateway_reference = models.CharField(max_length=160, blank=True)

    # --- Audit ------------------------------------------------------------
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="payments_recorded",
        null=True,
        blank=True,
    )
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="payments_confirmed",
        null=True,
        blank=True,
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="payments_voided",
        null=True,
        blank=True,
    )
    voided_at = models.DateTimeField(null=True, blank=True)
    void_reason = models.CharField(max_length=250, blank=True)

    class Meta(BranchScopedModel.Meta):
        abstract = False
        # Most recent money first: what a bursar wants on every one of these
        # screens is what just came in.
        ordering = ["-date_paid", "-id"]
        indexes = [
            models.Index(fields=["school", "branch"]),
            models.Index(fields=["student", "term"]),
            # The balance query: confirmed rows for a term.
            models.Index(fields=["term", "status"]),
            models.Index(fields=["branch", "status", "-date_paid"]),
        ]

    def __str__(self) -> str:
        return f"{self.amount} — {self.get_label_display()} ({self.student_id})"

    def get_absolute_url(self) -> str:
        return reverse("payments:payment_detail", args=[self.pk])

    # -- derived facts -------------------------------------------------------

    @property
    def counts_toward_balance(self) -> bool:
        """The one rule the whole balance rests on: confirmed money only."""
        return self.status == PaymentStatus.CONFIRMED

    @property
    def pill_class(self) -> str:
        return STATUS_PILLS.get(self.status, "status-unpaid")

    @property
    def receipt_number(self) -> str:
        """A stable, human-quotable number derived from the row's id.

        Not stored: the primary key already is the unique number, and a second
        counter would be a second thing to keep unique and in step.
        """
        return f"RCP-{self.pk:06d}" if self.pk else "RCP-------"

    @property
    def has_attachment(self) -> bool:
        return bool(self.receipt)

    @property
    def attachment_is_pdf(self) -> bool:
        return bool(self.receipt) and self.receipt.name.lower().endswith(".pdf")

    @property
    def receipt_file_url(self) -> str:
        """Where the attached slip is opened: behind the payments permission,
        not the storage URL. Every "View receipt" link points here."""
        # No pk yet means a form re-rendering an upload it has not saved:
        # there is nothing stored to open.
        if not (self.pk and self.receipt):
            return ""
        return reverse("payments:receipt_file", args=[self.pk])

    # -- transitions ---------------------------------------------------------

    def confirm(self, *, by=None, save: bool = True) -> "Payment":
        """Accept a payment. From here it counts toward the balance.

        Idempotent on ``confirmed_at`` rather than on ``status``: the field
        defaults to CONFIRMED, so an unsaved row is already "confirmed" before
        anybody has confirmed it, and guarding on the status alone would leave
        every over-the-counter payment with no record of who took it.
        """
        if self.status == PaymentStatus.CONFIRMED and self.confirmed_at is not None:
            return self
        self.status = PaymentStatus.CONFIRMED
        self.confirmed_by = by
        self.confirmed_at = timezone.now()
        if save:
            self.save(
                update_fields=["status", "confirmed_by", "confirmed_at", "updated_at"]
            )
        return self

    def void(self, *, by=None, reason: str = "", save: bool = True) -> "Payment":
        """Reverse a payment without erasing it.

        The row stays, stops counting, and records who did it and why. Balances
        move the moment this lands, because they are derived.
        """
        self.status = PaymentStatus.VOID
        self.voided_by = by
        self.voided_at = timezone.now()
        self.void_reason = reason.strip()[:250]
        if save:
            self.save(
                update_fields=[
                    "status", "voided_by", "voided_at", "void_reason", "updated_at"
                ]
            )
        return self

    # -- persistence ---------------------------------------------------------

    def save(self, *args, **kwargs):
        # A platform owner has no school of their own, so the student -- who
        # always knows their branch -- is what places the payment.
        if self.student_id and self.branch_id is None:
            self.branch_id = self.student.branch_id
        if self.status == PaymentStatus.CONFIRMED and self.confirmed_at is None:
            # Recorded straight as confirmed, which is the common case at the
            # bursary window: there is no pending step to stamp, so stamp now.
            self.confirmed_at = timezone.now()
        # In the model rather than in each writer, so the screens, walk-in enrolment,
        # seeds and the admin all stay correct, as will whatever writes one next. Both
        # rows or neither.
        with db_transaction.atomic():
            if self._state.adding and self.transaction_id is None:
                self.transaction = PaymentTransaction.all_objects.create(
                    school_id=self.school_id or self.branch.school_id,
                    branch_id=self.branch_id,
                    **self._mirrored_values(),
                )
            super().save(*args, **kwargs)
            self._keep_single_transaction_in_step()

    def _mirrored_values(self) -> dict:
        return {name: getattr(self, name) for name in _MIRRORED}

    def _keep_single_transaction_in_step(self) -> None:
        """A one-child transaction always says what its payment says."""
        txn = self.transaction
        if txn is None or txn.is_shared:
            return
        changed = [
            name for name, value in self._mirrored_values().items()
            if getattr(txn, name) != value
        ]
        if changed:
            for name in changed:
                setattr(txn, name, getattr(self, name))
            txn.save()

    def clean(self):
        super().clean()
        if self.student_id and self.term_id:
            if self.student.branch_id != self.term.branch_id:
                raise ValidationError(
                    {"term": "That term belongs to a different branch than the "
                             "student."}
                )
        if self.date_paid and self.date_paid > timezone.localdate():
            raise ValidationError({"date_paid": "That date is in the future."})


@receiver(post_delete, sender=Payment, dispatch_uid="payments_single_transaction_goes_too")
def _delete_single_transaction(sender, instance, **kwargs):
    """A one-child transaction means nothing without its payment."""
    if instance.transaction_id:
        PaymentTransaction.all_objects.filter(
            pk=instance.transaction_id, kind=TransactionKind.INDIVIDUAL
        ).delete()
