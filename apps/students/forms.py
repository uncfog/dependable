"""Student entry, one at a time.

``StudentForm`` is the path for the child who walks in on a Tuesday. The bulk
import has a front door of its own, and it is not here: choosing the campus and
refusing anything that is obviously not a workbook is the same job for all four
imports, so it lives in :mod:`apps.core.import_flow`.

Every queryset here goes through a tenant-scoped manager, so the classes offered
are already limited to what the user may see -- the extra validation below
guards what scoping alone cannot: a school owner who *can* see two branches must
still not put a Main Campus child into an Annex class.
"""

from __future__ import annotations

from django import forms

from apps.academics.models import Class
from apps.core.forms import StyledFormMixin

from .models import Student, StudentStatus
from .validators import normalise_admission_number, normalise_phone


class StudentForm(StyledFormMixin, forms.ModelForm):
    """Add or edit one student.

    Branch is not a field: it comes from the chosen class, which always knows
    its own campus. Asking for both invites the two to disagree.
    """

    class Meta:
        model = Student
        fields = [
            "school_class",
            "admission_number",
            "first_name",
            "last_name",
            "other_names",
            "sex",
            "date_of_birth",
            "date_admitted",
            "status",
            "parent_name",
            "parent_phone",
            "parent_phone_alt",
            "parent_email",
            "address",
        ]
        widgets = {
            "admission_number": forms.TextInput(
                attrs={"placeholder": "FA/2025/001", "autocomplete": "off"}
            ),
            "first_name": forms.TextInput(attrs={"placeholder": "First name"}),
            "last_name": forms.TextInput(attrs={"placeholder": "Surname"}),
            "other_names": forms.TextInput(attrs={"placeholder": "Middle names"}),
            # `type=date` gets the native picker on mobile, which is where the
            # front desk will actually type this.
            "date_of_birth": forms.DateInput(
                attrs={"type": "date"}, format="%Y-%m-%d"
            ),
            "date_admitted": forms.DateInput(
                attrs={"type": "date"}, format="%Y-%m-%d"
            ),
            "parent_name": forms.TextInput(
                attrs={"placeholder": "Parent or guardian name"}
            ),
            "parent_phone": forms.TextInput(
                attrs={
                    "placeholder": "0803 123 4567",
                    "inputmode": "tel",
                    "autocomplete": "tel",
                }
            ),
            "parent_phone_alt": forms.TextInput(
                attrs={
                    "placeholder": "0805 987 6543",
                    "inputmode": "tel",
                    "autocomplete": "tel",
                }
            ),
            "parent_email": forms.EmailInput(
                attrs={"placeholder": "parent@example.com", "autocomplete": "email"}
            ),
            "address": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        classes = Class.objects.filter(is_active=True).select_related("branch")
        # An existing student's class may since have been deactivated; keep it
        # selectable so editing their phone number does not force a class change.
        if self.instance.pk and self.instance.school_class_id:
            classes = classes | Class.objects.filter(pk=self.instance.school_class_id)
        self.fields["school_class"].queryset = classes.distinct()
        self.fields["school_class"].label = "Class"
        self.fields["school_class"].empty_label = "Select a class"

        # A school owner sees several branches at once, so a bare "JSS 1A" is
        # ambiguous. A principal sees one campus and does not need the noise.
        if Class.objects.values("branch_id").distinct().count() > 1:
            self.fields["school_class"].label_from_instance = (
                lambda obj: f"{obj.branch.name} — {obj.display_name}"
            )
        else:
            self.fields["school_class"].label_from_instance = (
                lambda obj: obj.display_name
            )

        self.fields["sex"].empty_label = "Select"
        self.fields["other_names"].help_text = "Middle names, if any."

    # -- normalisation -------------------------------------------------------

    def clean_admission_number(self):
        return normalise_admission_number(self.cleaned_data["admission_number"])

    def clean_parent_phone(self):
        """Store one canonical form, whatever shape it was typed in."""
        return normalise_phone(self.cleaned_data["parent_phone"])

    def clean_parent_phone_alt(self):
        return normalise_phone(self.cleaned_data["parent_phone_alt"])

    def clean_first_name(self):
        return self.cleaned_data["first_name"].strip()

    def clean_last_name(self):
        return self.cleaned_data["last_name"].strip()

    def clean_other_names(self):
        return self.cleaned_data["other_names"].strip()

    def clean_parent_name(self):
        return self.cleaned_data["parent_name"].strip()

    # -- cross-field ---------------------------------------------------------

    def clean(self):
        cleaned = super().clean()
        school_class = cleaned.get("school_class")
        admission_number = cleaned.get("admission_number")

        if school_class:
            # Set before the model's own clean() runs, so the branch check it
            # does has something to compare against.
            self.instance.branch = school_class.branch
            self.instance.school_id = school_class.school_id

        if school_class and admission_number:
            # Caught here as well as by the database constraint, so the user
            # gets the message on the field rather than a 500.
            clash = Student.objects.filter(
                branch=school_class.branch, admission_number__iexact=admission_number
            )
            if self.instance.pk:
                clash = clash.exclude(pk=self.instance.pk)
            existing = clash.first()
            if existing is not None:
                self.add_error(
                    "admission_number",
                    f"{existing.full_name} at {school_class.branch.name} already "
                    f"has that admission number.",
                )
        return cleaned


class StudentFilterForm(forms.Form):
    """The list screen's search and filter bar.

    A plain ``Form``, not a ModelForm: nothing here is saved, and every field is
    optional so an empty submit means "show everything".
    """

    q = forms.CharField(
        required=False,
        label="Search",
        widget=forms.TextInput(
            attrs={
                "placeholder": "Name or admission number",
                "type": "search",
                "autocomplete": "off",
            }
        ),
    )
    school_class = forms.ModelChoiceField(
        queryset=Class.objects.none(),
        required=False,
        label="Class",
        empty_label="All classes",
    )
    status = forms.ChoiceField(
        required=False,
        label="Status",
        # "" is every status, including the ones that mean the student has left.
        # The view defaults the *absent* parameter to Active instead, because a
        # roster that has been running for years is mostly former students.
        choices=[("", "All statuses")] + list(StudentStatus.choices),
    )
    # The chips above the roster. Worked out from each child's ledger, not stored.
    balance = forms.ChoiceField(
        required=False,
        choices=[("", "All"), ("owes", "Owes"), ("paid", "Paid in full")],
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["school_class"].queryset = Class.objects.select_related("branch")
        self.fields["school_class"].label_from_instance = lambda obj: obj.display_name
        for field in self.fields.values():
            widget = field.widget
            widget.attrs.setdefault("class", "field-input")
            if isinstance(widget, forms.Select):
                # Submitting on change keeps the filters usable without pressing
                # anything; the noscript button on the form still works.
                widget.attrs.setdefault("onchange", "this.form.submit()")

    def clean_q(self):
        return self.cleaned_data["q"].strip()
