"""Family forms."""

from __future__ import annotations

from django import forms
from django.core.exceptions import ValidationError

from apps.core.forms import StyledFormMixin
from apps.payments.forms import StudentPickerField

from .models import Family


class FamilyForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Family
        fields = ["name", "family_code", "contact_name", "contact_phone", "contact_email", "note"]

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)
        if school is not None:
            self.instance.school = school

    def clean_family_code(self):
        code = self.cleaned_data["family_code"].strip()
        # Against the whole school rather than through a scoped lookup, and by hand
        # because the school is not on this form, which leaves the constraint unchecked.
        if code:
            clash = Family.all_objects.filter(
                school_id=self.instance.school_id, family_code__iexact=code
            ).exclude(pk=self.instance.pk)
            if clash.exists():
                raise ValidationError("Another family already has that family ID.")
        return code


class AddChildForm(StyledFormMixin, forms.Form):
    """Which child. Only children the account can see can be named, through the picker."""

    student = StudentPickerField(label="Child")
