"""Forms for fee structures and their line items."""

from __future__ import annotations

from decimal import Decimal

from django import forms
from django.forms import BaseInlineFormSet, inlineformset_factory

from apps.academics.models import Class
from apps.core.forms import BranchScopedForm, StyledFormMixin

from .models import FeeComponent, FeeStructure, StudentFeeAdjustment, Term, not_past


class StudentFeeAdjustmentForm(StyledFormMixin, forms.ModelForm):
    """A change to one child's bill. Its student comes from the URL, never the form."""

    class Meta:
        model = StudentFeeAdjustment
        fields = ["term", "kind", "amount", "description"]
        widgets = {
            "amount": forms.NumberInput(attrs={"step": "0.01", "inputmode": "decimal"}),
        }

    def __init__(self, *args, student, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.student = student
        self.fields["amount"].label = "Amount (₦)"
        # Their own campus's terms. Earlier ones too, which is how a balance from
        # before the school joined gets recorded.
        self.fields["term"].queryset = Term.objects.filter(branch_id=student.branch_id)
        current = self.fields["term"].queryset.filter(is_current=True).first()
        if current is not None:
            self.fields["term"].initial = current


class VoidAdjustmentForm(StyledFormMixin, forms.Form):
    reason = forms.CharField(
        label="Why is this being voided?",
        max_length=250,
        widget=forms.TextInput(attrs={"placeholder": "Entered against the wrong child"}),
    )


class FeeStructureForm(BranchScopedForm):
    """Pick a class and a term. Branch and school are derived from the class.

    Both dropdowns run through tenant-scoped managers, so the options are
    already limited to what this user may see.
    """

    class Meta:
        model = FeeStructure
        fields = ["school_class", "term"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        classes = Class.objects.filter(is_active=True).select_related("branch")
        # Not a term a campus has moved on from: see Term.is_past.
        terms = not_past(Term.objects.select_related("branch"))
        self.fields["school_class"].queryset = classes
        self.fields["term"].queryset = terms
        self.fields["school_class"].label = "Class"

        # A school owner sees several branches at once, so the bare class name
        # is ambiguous. A principal sees one and does not need the noise.
        if self.visible_branch_count > 1:
            self.fields["school_class"].label_from_instance = (
                lambda obj: f"{obj.branch.name} — {obj.display_name}"
            )
            self.fields["term"].label_from_instance = (
                lambda obj: f"{obj.branch.name} — {obj.name}"
            )
        else:
            self.fields["school_class"].label_from_instance = (
                lambda obj: obj.display_name
            )

    def clean(self):
        cleaned = super().clean()
        school_class = cleaned.get("school_class")
        term = cleaned.get("term")
        if school_class and term and school_class.branch_id != term.branch_id:
            raise forms.ValidationError(
                {"term": "That term belongs to a different branch than the class."}
            )
        if school_class and term:
            clash = FeeStructure.objects.filter(school_class=school_class, term=term)
            if self.instance.pk:
                clash = clash.exclude(pk=self.instance.pk)
            if clash.exists():
                raise forms.ValidationError(
                    {
                        "school_class": f"{school_class.display_name} already has a "
                        f"fee structure for {term.name}. Edit that one instead."
                    }
                )
        return cleaned


class TermForm(BranchScopedForm):
    """A term at one campus.

    ``is_current`` is a plain checkbox rather than anything cleverer because
    ``Term.save()`` already stands down the branch's other current term -- the
    model holds the invariant, so the form does not have to police it.
    """

    class Meta:
        model = Term
        fields = ["branch", "name", "academic_year", "sequence", "is_current", "due_date"]
        widgets = {"due_date": forms.DateInput(attrs={"type": "date"})}
        labels = {"is_current": "This is the term the school is in now"}
        help_texts = {
            "is_current": (
                "Starting a new term ends the last one: its fees are locked, and "
                "anything unpaid carries over as arrears. Move students to their new "
                "classes after this, not before."
            ),
        }


class FeeComponentForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = FeeComponent
        fields = ["name", "amount"]
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "e.g. Textbooks"}),
            "amount": forms.NumberInput(
                attrs={"step": "0.01", "min": "0", "placeholder": "0",
                       "inputmode": "decimal"}
            ),
        }

    def clean_name(self):
        return self.cleaned_data["name"].strip()


class BaseFeeComponentFormSet(BaseInlineFormSet):
    """Validates the line items as a set, not just one at a time."""

    def clean(self):
        super().clean()
        if any(self.errors):
            return

        names: dict[str, int] = {}
        kept = 0
        for form in self.forms:
            if not form.cleaned_data or form.cleaned_data.get("DELETE"):
                continue
            kept += 1
            key = form.cleaned_data["name"].casefold()
            if key in names:
                form.add_error(
                    "name", "This line item is already listed on this structure."
                )
            names[key] = 1

        if kept == 0:
            raise forms.ValidationError(
                "A fee structure needs at least one line item."
            )

    @property
    def total(self) -> Decimal:
        """Server-side total, used to re-render the header after a failed post."""
        running = Decimal("0")
        for form in self.forms:
            if not form.cleaned_data or form.cleaned_data.get("DELETE"):
                continue
            running += form.cleaned_data.get("amount") or Decimal("0")
        return running


def _component_formset(extra: int):
    return inlineformset_factory(
        FeeStructure,
        FeeComponent,
        form=FeeComponentForm,
        formset=BaseFeeComponentFormSet,
        fields=["name", "amount"],
        extra=extra,
        can_delete=True,
    )


#: Editing an existing structure: show exactly the line items that exist.
FeeComponentFormSet = _component_formset(extra=0)

#: Creating one: start with a few blank rows so there is something to type into
#: before any JavaScript runs. `extra` is ignored once the formset is bound, so
#: a failed submit does not accumulate more rows.
NewFeeComponentFormSet = _component_formset(extra=4)
