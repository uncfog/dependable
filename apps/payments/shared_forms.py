"""Forms for family payments. The rules stay in :mod:`apps.payments.allocation`."""

from __future__ import annotations

from decimal import Decimal

from django import forms
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.core.forms import StyledFormMixin
from apps.families.models import Family
from apps.schools.models import Branch

from . import allocation, duplicates
from .forms import validate_receipt_upload
from .models import (
    PaymentLabel,
    PaymentMethod,
    PaymentStatus,
    PaymentTransaction,
    TransactionKind,
)

MINIMUM = Decimal("0.01")


def _money_input() -> forms.NumberInput:
    # The class by hand: the mixin styles fields that exist when the form is built,
    # and the per-child boxes are added after that.
    return forms.NumberInput(attrs={
        "class": "field-input", "step": "0.01", "min": "0.01", "inputmode": "decimal",
        "placeholder": "0",
    })


class ReceiveForm(StyledFormMixin, forms.Form):
    """The money that came in, before anyone decides whose it is."""

    family = forms.ModelChoiceField(queryset=Family.objects.none(), empty_label="Choose a family")
    branch = forms.ModelChoiceField(
        queryset=Branch.objects.none(), label="Campus that took it", empty_label="Choose a campus"
    )
    amount = forms.DecimalField(
        label="Amount (₦)", max_digits=12, decimal_places=2, min_value=MINIMUM, widget=_money_input()
    )
    date_paid = forms.DateField(
        label="Date paid",
        help_text="Day / month / year",
        # Typed as dd/mm/yyyy, like a single child's payment; ISO is still accepted.
        input_formats=["%d/%m/%Y", "%Y-%m-%d"],
        widget=forms.DateInput(
            attrs={"placeholder": "dd/mm/yyyy", "inputmode": "numeric", "autocomplete": "off"},
            format="%d/%m/%Y",
        ),
    )
    method = forms.ChoiceField(
        label="How was it paid", choices=PaymentMethod.choices,
        initial=PaymentMethod.TRANSFER, widget=forms.RadioSelect,
    )
    reference = forms.CharField(
        label="Reference (optional)", max_length=120, required=False,
        widget=forms.TextInput(attrs={"placeholder": "Teller or transfer ref", "autocomplete": "off"}),
    )
    payer_name = forms.CharField(
        label="Paid by (optional)", max_length=160, required=False,
        help_text="Printed on the receipt. Leave blank to use the family's contact.",
    )
    receipt = forms.FileField(
        label="Bank slip or POS receipt", required=False,
        help_text="Optional. Image or PDF.",
        widget=forms.ClearableFileInput(attrs={"accept": "image/*,application/pdf"}),
    )
    note = forms.CharField(
        label="Note (optional)", max_length=250, required=False,
        widget=forms.TextInput(attrs={"placeholder": "Anything the receipt does not say"}),
    )
    confirm_now = forms.BooleanField(
        required=False, initial=False, label="Confirm this payment now",
        help_text="Leave unticked to hold it until someone has checked it. Nothing counts "
                  "toward a balance until then.",
    )
    receipt_checked = forms.BooleanField(
        required=False, initial=False,
        label="I have checked the slip against the family and the amount",
    )

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["date_paid"].initial = timezone.localdate()
        self.fields["family"].queryset = Family.objects.all()
        # Campus staff take money at their own campus, whatever a request says.
        self.fixed_branch = user.branch if user.branch_id else None
        if self.fixed_branch is None:
            branches = Branch.objects.all()
            if len(branches) == 1:
                self.fixed_branch = branches[0]
            else:
                self.fields["branch"].queryset = branches
        if self.fixed_branch is not None:
            del self.fields["branch"]

    def clean_receipt(self):
        return validate_receipt_upload(self.cleaned_data.get("receipt"))

    def clean(self):
        cleaned = super().clean()
        family, branch = cleaned.get("family"), self.campus(cleaned)
        if family and branch and family.school_id != branch.school_id:
            self.add_error("family", "That family is at a different school.")
        if cleaned.get("confirm_now") and cleaned.get("receipt") and not cleaned.get("receipt_checked"):
            self.add_error(
                "receipt_checked",
                "Check the slip against the family and the amount, then tick this box. "
                "Or untick “confirm” to hold it.",
            )
        return cleaned

    def campus(self, cleaned):
        return self.fixed_branch or cleaned.get("branch")

    def save(self, *, actor) -> tuple[PaymentTransaction, bool]:
        """The payment, and whether this was a repeat of one just recorded."""
        cleaned = self.cleaned_data
        family = cleaned["family"]
        same_money = {
            "family": family,
            "amount": cleaned["amount"],
            "date_paid": cleaned["date_paid"],
            "method": cleaned["method"],
            "reference": cleaned["reference"],
        }
        with transaction.atomic():
            duplicates.lock_row(Family, family.pk)
            twin = duplicates.recent_twin(
                PaymentTransaction.all_objects.filter(kind=TransactionKind.FAMILY, **same_money),
                recorded_by=actor,
            )
            if twin is not None:
                return twin, True
            return (
                allocation.receive(
                    **same_money,
                    actor=actor,
                    branch=self.campus(cleaned),
                    payer_name=cleaned["payer_name"],
                    note=cleaned["note"],
                    receipt=cleaned.get("receipt"),
                    status=(
                        PaymentStatus.CONFIRMED if cleaned["confirm_now"] else PaymentStatus.PENDING
                    ),
                ),
                False,
            )


def fingerprint(txn: PaymentTransaction) -> str:
    """Changes whenever the shares do, so a page can tell it has gone out of date."""
    rows = txn.shares()
    return f"{rows.count()}:{txn.allocated}:{rows.filter(status=PaymentStatus.VOID).count()}"


class ShareForm(StyledFormMixin, forms.Form):
    """An amount per child. All of them are saved, or none."""

    seen = forms.CharField(widget=forms.HiddenInput)
    label = forms.ChoiceField(
        label="What is it for?", choices=PaymentLabel.choices, initial=PaymentLabel.SCHOOL_FEES
    )
    allow_credit = forms.BooleanField(
        required=False, label="Yes, keep anything above what they owe as their credit"
    )

    def __init__(self, *args, txn, children, **kwargs):
        super().__init__(*args, **kwargs)
        self.txn = txn
        self.fields["seen"].initial = fingerprint(txn)
        self.children = children
        for row in children:
            self.fields[self._name(row)] = forms.DecimalField(
                label=row.student.full_name, required=False, max_digits=12, decimal_places=2,
                min_value=MINIMUM, widget=_money_input(),
            )

    @staticmethod
    def _name(row) -> str:
        return f"share_{row.student.pk}"

    def rows(self):
        return [(row, self[self._name(row)]) for row in self.children]

    def is_out_of_date(self) -> bool:
        return self.data.get("seen") != fingerprint(self.txn)

    def clean(self):
        cleaned = super().clean()
        if not self.errors and not self._amounts():
            raise ValidationError("Enter an amount for at least one child.")
        return cleaned

    def _amounts(self):
        pairs = ((row, self.cleaned_data.get(self._name(row))) for row in self.children)
        return [(row, amount) for row, amount in pairs if amount]

    def save(self, *, actor) -> Decimal:
        """Raises :class:`~apps.payments.allocation.AllocationError` with ``.student`` set."""
        total = Decimal("0")
        with transaction.atomic():
            for row, amount in self._amounts():
                try:
                    allocation.allocate(
                        self.txn, row.student, amount, actor=actor,
                        label=self.cleaned_data["label"],
                        allow_credit=self.cleaned_data["allow_credit"],
                    )
                except allocation.AllocationError as refused:
                    refused.student = row.student
                    raise
                total += amount
        return total


class MoveForm(StyledFormMixin, forms.Form):
    to_student = forms.ChoiceField(label="Move it to")
    reason = forms.CharField(
        label="Why?", max_length=250,
        help_text="Kept on the record, so it explains itself later.",
    )
    allow_credit = forms.BooleanField(
        required=False, label="Yes, keep anything above what they owe as their credit"
    )

    def __init__(self, *args, children, **kwargs):
        super().__init__(*args, **kwargs)
        self.children = {row.student.pk: row.student for row in children}
        self.fields["to_student"].choices = [("", "Choose a child")] + [
            (row.student.pk, row.student.full_name) for row in children
        ]

    def student(self):
        return self.children[int(self.cleaned_data["to_student"])]
