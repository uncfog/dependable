"""Recording, confirming and voiding money.

The student picker is a text input backed by a native ``<datalist>`` rather
than a long ``<select>``, because it is how the job is actually done: the bursar
has a receipt in front of them with an admission number on it, and types it.

The list is filled from a search as they type, instead of being sent with the
form. A school's whole roster on every payment form was a heavy page on a phone
and a slow one on a poor connection. Without scripting, typing the admission number
still works, which is the common case anyway.

The typed value is resolved back to a ``Student`` through the tenant-scoped
manager, so a copied-and-pasted admission number from another school finds
nothing.

Admission numbers are unique per campus, not per school, so an account that can
see two campuses can see two children with one number. Their options name the
campus, and the whole option decides which child is meant. A bare number that
fits both is refused rather than guessed: this is where money meets a child, and
crediting the wrong one looks like a perfectly good payment.
"""

from __future__ import annotations

import posixpath
import re
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal

from django import forms
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils import timezone

from apps.core.forms import StyledFormMixin
from apps.fees.models import Term
from apps.students.models import Student, StudentStatus

from .models import Payment, PaymentLabel, PaymentMethod, PaymentStatus

#: What an uploaded receipt may be, and how big. A parent's bank slip is a
#: phone photo or a PDF; anything else is a mistake worth catching at the form
#: rather than at the storage layer.
RECEIPT_TYPES = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".pdf"}
RECEIPT_MAX_BYTES = 8 * 1024 * 1024

#: "FA/2025/001 — Chinaza Okonkwo (JSS 1A)" → the admission number.
_PICKER = re.compile(r"^\s*([^—]+?)\s*(?:—.*)?$")


def _admission_number_in(value: str) -> str:
    match = _PICKER.match(value)
    return (match.group(1) if match else value).strip()


def _students_numbered(admission_number: str):
    """Everyone the caller can see with this number. Tenant-scoped."""
    return Student.objects.filter(
        admission_number__iexact=admission_number
    ).select_related("school_class", "branch__school")


def student_from_picker(value) -> Student | None:
    """The student a picker entry names, or None if it does not name exactly one.

    Accepts the whole datalist option or just the admission number. Tenant-scoped
    through ``Student.objects``, so another school's admission number names
    nobody. When the caller can see several children with that number, only their
    full option decides. The field's ``clean`` and the live balance lookup both
    read through here, so the panel can never show a student the form would then
    refuse.
    """
    if isinstance(value, Student):
        return value
    value = str(value or "").strip()
    if not value:
        return None
    matches = list(_students_numbered(_admission_number_in(value)))
    if len(matches) == 1:
        return matches[0]
    named = [
        student for student in matches
        if picker_label(student, with_campus=True) == value
    ]
    return named[0] if len(named) == 1 else None


def picker_label(student: Student, *, with_campus: bool | None = None) -> str:
    """One datalist option. The admission number leads, because that is what is
    printed on the receipt the bursar is holding.

    The campus is added when the caller can see another child with this number.
    Left as ``None``, that is looked up; :func:`picker_options` already knows for
    its whole list and passes it in, so a roster stays one query.
    """
    label = (
        f"{student.admission_number} — {student.full_name} "
        f"({student.school_class.display_name})"
    )
    if with_campus is None:
        with_campus = (
            _students_numbered(student.admission_number)
            .exclude(pk=student.pk)
            .exists()
        )
    # ``str(branch)`` includes the school, which a platform account needs: two
    # schools can both have a campus called North.
    return f"{label} — {student.branch}" if with_campus else label


class StudentPickerField(forms.CharField):
    """A student, chosen by typing. Cleans to the ``Student`` instance."""

    widget = forms.TextInput

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("label", "Student")
        kwargs.setdefault(
            "help_text",
            "Type an admission number or a name and pick from the list.",
        )
        super().__init__(*args, **kwargs)
        self.widget.attrs.setdefault("list", "student-options")
        self.widget.attrs.setdefault("autocomplete", "off")
        self.widget.attrs.setdefault("placeholder", "FA/2025/001")

    def clean(self, value):
        value = super().clean(value)
        if not value:
            if self.required:
                raise ValidationError("Choose the student this payment is for.")
            return None

        student = student_from_picker(value)
        if student is None:
            admission_number = _admission_number_in(value)
            if _students_numbered(admission_number).exists():
                raise ValidationError(
                    f'More than one student here share the admission number '
                    f'"{admission_number}". Pick the right child and campus from '
                    f"the list as you type."
                )
            raise ValidationError(
                f'No student here has the admission number "{admission_number}". '
                f"Pick one from the list as you type."
            )
        return student

    def prepare_value(self, value):
        """Render an existing choice the same way the datalist offers it."""
        if isinstance(value, Student):
            return picker_label(value)
        # Only a real integer is an id -- the instance's own ``student_id``. Text
        # came from the form, and "014" is an admission number some school uses:
        # reading it as a primary key swapped the child on a refused form.
        if isinstance(value, int):
            student = Student.objects.filter(pk=value).first()
            if student is not None:
                return picker_label(student)
        return value


def validate_receipt_upload(upload):
    # An unchanged existing file comes back as the FieldFile, which has nothing
    # to re-validate.
    if not upload or not hasattr(upload, "content_type"):
        return upload
    name = (upload.name or "").lower()
    if not any(name.endswith(ext) for ext in RECEIPT_TYPES):
        raise ValidationError("Upload the receipt as an image or a PDF.")
    if upload.size > RECEIPT_MAX_BYTES:
        raise ValidationError(
            f"That file is {upload.size // 1024 // 1024} MB. Keep receipts "
            f"under {RECEIPT_MAX_BYTES // 1024 // 1024} MB."
        )
    return upload


@dataclass(frozen=True)
class _GatedFile:
    """What the "Currently:" line links to: the name, but the gated URL."""

    name: str
    url: str

    def __str__(self) -> str:
        return self.name


class GatedReceiptInput(forms.ClearableFileInput):
    """The receipt upload, whose "Currently:" link opens the gated page.

    Django's widget links straight to the storage URL, which skips the
    payments permission. ``gated_url`` is set by the form from its instance.
    """

    gated_url = ""

    def get_context(self, name, value, attrs):
        context = super().get_context(name, value, attrs)
        if context["widget"]["is_initial"] and self.gated_url:
            context["widget"]["value"] = _GatedFile(
                name=posixpath.basename(value.name), url=self.gated_url
            )
        return context


#: How many matches one search returns. Enough to pick from on a phone.
SEARCH_LIMIT = 20
SEARCH_MIN_LENGTH = 2


def picker_options(query: str | None = None) -> list[str]:
    """The students the caller may record a payment against, as picker options.

    Active only: money is taken against a child who is here. Tenant-scoped, so
    the list is the caller's own campus. Given a query, the matches for it, capped:
    every word has to appear in the name or the admission number. The campus is
    worked out against everyone the caller can see, not just these matches, so an
    option always names it when the number is shared.
    """
    students = Student.objects.filter(status=StudentStatus.ACTIVE).select_related(
        "school_class", "branch__school"
    )
    numbers = Student.objects.values_list("admission_number", flat=True)
    uses = Counter(number.upper() for number in numbers)
    if query is not None:
        for word in query.split():
            students = students.filter(
                Q(admission_number__icontains=word)
                | Q(first_name__icontains=word)
                | Q(last_name__icontains=word)
                | Q(other_names__icontains=word)
            )
        students = students[:SEARCH_LIMIT]
    return [
        picker_label(student, with_campus=uses[student.admission_number.upper()] > 1)
        for student in students
    ]


class PaymentForm(StyledFormMixin, forms.ModelForm):
    """Record a payment, or correct a pending one before confirming it."""

    student = StudentPickerField()

    class Meta:
        model = Payment
        fields = [
            "student",
            "amount",
            "date_paid",
            "label",
            "method",
            "reference",
            "receipt",
            "note",
        ]
        widgets = {
            "amount": forms.NumberInput(
                attrs={"step": "0.01", "min": "0.01", "inputmode": "decimal",
                       "placeholder": "50000"}
            ),
            # A typed day, dd/mm/yyyy, the way Nigerian receipts print it. The
            # calendar button beside it (payment_form.html) fills this same text.
            "date_paid": forms.DateInput(
                attrs={"placeholder": "dd/mm/yyyy", "inputmode": "numeric",
                       "autocomplete": "off"},
                format="%d/%m/%Y",
            ),
            "reference": forms.TextInput(
                attrs={"placeholder": "Teller or transfer ref", "autocomplete": "off"}
            ),
            "method": forms.RadioSelect,
            "note": forms.TextInput(
                attrs={"placeholder": "Anything the receipt does not say"}
            ),
            "receipt": GatedReceiptInput(
                attrs={"accept": "image/*,application/pdf"}
            ),
        }

    #: Rendered as two buttons rather than a status dropdown -- "Save as
    #: pending" and "Record as confirmed" are two different intentions, and a
    #: select makes the bursar read three words to express one of them.
    confirm_now = forms.BooleanField(
        required=False,
        initial=False,
        label="Confirm this payment now",
        help_text="Leave unticked to hold it in the pending queue for checking.",
    )

    #: The human check on an attached slip. Required -- in clean(), not here --
    #: only when a receipt is attached and the payment is being confirmed: that
    #: is the moment money starts counting toward a balance on the strength of
    #: a picture, and nothing in the system reads the picture. The person
    #: confirming does, and this is them saying so. Unticked by default, always:
    #: a box that arrives ticked is a blind confirm with an extra step.
    receipt_checked = forms.BooleanField(
        required=False,
        initial=False,
        label="I have checked the receipt against the student and amount",
        help_text="The name or admission number on the slip is this student, "
                  "and the amount on it matches the amount above.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["date_paid"].initial = timezone.localdate()
        # Typed as dd/mm/yyyy; ISO is still accepted so a pasted or old-style value
        # is not refused.
        self.fields["date_paid"].input_formats = ["%d/%m/%Y", "%Y-%m-%d"]
        self.fields["amount"].label = "Amount (₦)"
        self.fields["amount"].help_text = "Amount on the receipt"
        self.fields["date_paid"].help_text = "Day / month / year"
        self.fields["label"].label = "What it is for"
        self.fields["label"].help_text = ""
        self.fields["reference"].help_text = ""
        self.fields["method"].label = "How was it paid"
        # A radio per method, so a blank "-------" row is not one of the choices.
        self.fields["method"].choices = PaymentMethod.choices
        self.fields["reference"].label = "Reference (optional)"
        self.fields["note"].label = "Note (optional)"
        self.fields["note"].widget.attrs.setdefault(
            "placeholder", "Anything the receipt does not say"
        )
        self.fields["student"].widget.attrs["placeholder"] = "Type an admission number or a name"
        self.fields["student"].help_text = "Pick the student to show their balance."

        if "receipt" in self.fields:
            self.fields["receipt"].widget.gated_url = self.instance.receipt_file_url

        if self.instance.pk:
            self.fields["confirm_now"].initial = (
                self.instance.status == PaymentStatus.CONFIRMED
            )

    def clean_amount(self) -> Decimal:
        amount = self.cleaned_data["amount"]
        if amount is not None and amount <= 0:
            raise ValidationError("A payment has to be more than nothing.")
        return amount

    def clean_receipt(self):
        return validate_receipt_upload(self.cleaned_data.get("receipt"))

    def has_receipt(self) -> bool:
        """Whether the payment will have a slip attached once this form saves.

        A new upload counts; so does the one already on the row, unless the
        form's own "clear" box is removing it (which cleans to ``False``).
        """
        upload = self.cleaned_data.get("receipt")
        if upload is False:
            return False
        if upload:
            return True
        return bool(self.instance.pk and self.instance.receipt)

    def needs_receipt_check(self) -> bool:
        """Confirming money on the strength of an attached slip.

        Only on the way *into* confirmed: re-saving a payment that is already
        confirmed is a correction, and was checked when it was confirmed.
        """
        confirming = bool(self.cleaned_data.get("confirm_now"))
        already_confirmed = bool(
            self.instance.pk and self.instance.status == PaymentStatus.CONFIRMED
        )
        return confirming and not already_confirmed and self.has_receipt()

    def clean(self):
        cleaned = super().clean()
        if self.needs_receipt_check() and not cleaned.get("receipt_checked"):
            self.add_error(
                "receipt_checked",
                "Check the attached receipt against the student and the amount, "
                "then tick this box. Or untick “confirm” to leave it pending.",
            )
        student = cleaned.get("student")
        if student is None:
            return cleaned

        # Set before the model's own clean() runs, so its branch check has
        # something to compare against.
        self.instance.student = student
        self.instance.branch = student.branch
        self.instance.school_id = student.school_id

        term = self.resolve_term(student)
        if term is None:
            self.add_error(
                None,
                f"{student.branch.name} has no current term, so there is nothing "
                f"to credit this payment to. Mark a term as current first.",
            )
        else:
            self.instance.term = term
        return cleaned

    def resolve_term(self, student) -> Term | None:
        """The term a payment lands in: the student's branch's current one.

        Kept on the row rather than worked out from the date every time a
        balance is read. A payment made in the holidays for next term is still
        that term's money, and only the person recording it knows which.
        """
        if self.instance.pk and self.instance.term_id:
            return self.instance.term
        return Term.objects.filter(
            branch_id=student.branch_id, is_current=True
        ).first()

    def save(self, commit=True, *, user=None):
        payment = super().save(commit=False)
        confirm = self.cleaned_data.get("confirm_now")

        if payment.pk is None:
            payment.recorded_by = user
        if confirm:
            payment.confirm(by=user, save=False)
        else:
            payment.status = PaymentStatus.PENDING
            payment.confirmed_by = None
            payment.confirmed_at = None

        if commit:
            payment.save()
        return payment


class ConfirmedPaymentNotesForm(PaymentForm):
    """What somebody who may not void can still correct on confirmed money.

    The description of the payment, and nothing that moves a balance. Changing
    the student or the amount, or taking it back to pending, reverses money just
    as voiding does, so it needs the same authority. The fields are removed
    rather than disabled, so a hand-made POST has nothing to write to.
    """

    LOCKED = ("student", "amount", "date_paid", "receipt", "confirm_now", "receipt_checked")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in self.LOCKED:
            del self.fields[name]

    def save(self, commit=True, *, user=None):
        # Past PaymentForm.save, whose handling of ``confirm_now`` would read the
        # absent box as "take it back to pending".
        payment = super(PaymentForm, self).save(commit=False)
        if commit:
            payment.save(
                update_fields=["label", "method", "reference", "note", "updated_at"]
            )
        return payment


class PendingConfirmForm(PaymentForm):
    """The pending queue's confirm step.

    Deliberately the same form: the client's flow is "someone uploads a
    receipt, the bursar confirms who and what it is for", which means the
    bursar must be able to *correct* the student, label and amount at the
    moment of confirming. A separate confirm-only form would force them to
    edit, go back, and confirm.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["confirm_now"].initial = True
        self.fields["confirm_now"].label = "Confirm this payment"
        self.fields["confirm_now"].help_text = (
            "Untick to leave it pending and come back to it."
        )


class VoidForm(StyledFormMixin, forms.Form):
    """Reversing a confirmed payment.

    A reason is required, not optional. Voiding moves a real balance, and "why
    is this 50,000 gone?" is asked months later by someone who was not there.
    """

    reason = forms.CharField(
        label="Why is this being voided?",
        max_length=250,
        widget=forms.TextInput(
            attrs={"placeholder": "Duplicate entry / wrong student / cheque bounced"}
        ),
    )
    confirm = forms.BooleanField(
        label="I understand this changes the student's balance",
        error_messages={"required": "Tick the box to confirm the void."},
    )


class PaymentFilterForm(forms.Form):
    """The branch-wide list's filter bar. Nothing here is saved."""

    q = forms.CharField(
        required=False,
        label="Search",
        widget=forms.TextInput(
            attrs={
                "placeholder": "Name, admission number or reference",
                "type": "search",
                "autocomplete": "off",
            }
        ),
    )
    status = forms.ChoiceField(
        required=False,
        label="Status",
        choices=[("", "All statuses")] + list(PaymentStatus.choices),
    )
    label = forms.ChoiceField(
        required=False,
        label="For",
        choices=[("", "All labels")] + list(PaymentLabel.choices),
    )
    method = forms.ChoiceField(
        required=False,
        label="Method",
        choices=[("", "All methods")] + list(PaymentMethod.choices),
    )
    since = forms.DateField(
        required=False,
        label="From",
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
    )
    until = forms.DateField(
        required=False,
        label="To",
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "field-input")

    def clean(self):
        cleaned = super().clean()
        since, until = cleaned.get("since"), cleaned.get("until")
        if since and until and since > until:
            self.add_error("until", "The end date is before the start date.")
        return cleaned
