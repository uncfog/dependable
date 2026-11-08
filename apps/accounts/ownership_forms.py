"""Forms for handing a school over. The rules are in ``apps.accounts.ownership``."""

from __future__ import annotations

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

from .models import OwnershipTransfer
from .ownership import TransferError, recipient_for

Afterwards = OwnershipTransfer.Afterwards


class StartTransferForm(forms.Form):
    """The owner names the new owner, says what happens to them, and re-enters a password.

    The password is asked for because this is the one action that gives the
    whole school away: a phone left signed in on a desk must not be enough.
    """

    full_name = forms.CharField(
        label="New owner's full name",
        max_length=150,
        widget=forms.TextInput(attrs={"autocomplete": "off"}),
    )
    email = forms.EmailField(
        label="New owner's email address",
        help_text="The link to accept goes here, and they sign in with it afterwards.",
        widget=forms.EmailInput(attrs={"autocomplete": "off"}),
    )
    afterwards = forms.ChoiceField(
        label="After the handover, you",
        choices=Afterwards.choices,
        initial=Afterwards.STAY_PRINCIPAL,
        widget=forms.RadioSelect,
    )
    stay_branch = forms.ModelChoiceField(
        label="Campus you will run",
        queryset=None,
        required=False,
        empty_label="Choose a campus",
    )
    password = forms.CharField(
        label="Your password",
        help_text="To confirm it is you.",
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}),
    )

    def __init__(self, owner, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.schools.models import Branch

        self.owner = owner
        branches = Branch.all_objects.filter(school_id=owner.school_id, is_active=True).order_by("name")
        self.fields["stay_branch"].queryset = branches
        # The school is obvious here; "Riverbank Academy - Main Campus" is noise.
        self.fields["stay_branch"].label_from_instance = lambda branch: branch.name
        if branches.count() == 1:
            # One campus: nothing to choose, and nothing to get wrong.
            self.fields["stay_branch"].initial = branches.first().pk

    def clean_full_name(self):
        return " ".join(self.cleaned_data["full_name"].split())

    def clean_email(self):
        email = self.cleaned_data["email"].strip()
        try:
            recipient_for(self.owner.school, email, current_owner=self.owner)
        except TransferError as error:
            raise ValidationError(str(error))
        return email

    def clean_password(self):
        password = self.cleaned_data["password"]
        if not self.owner.check_password(password):
            raise ValidationError("That is not your password.")
        return password

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("afterwards") == Afterwards.STAY_PRINCIPAL and not cleaned.get("stay_branch"):
            self.add_error("stay_branch", "Choose the campus you will run.")
        return cleaned


class AcceptAsNewForm(forms.Form):
    """Someone with no account yet: their name and a password, then yes."""

    first_name = forms.CharField(label="First name", max_length=150,
                                 widget=forms.TextInput(attrs={"autocomplete": "given-name"}))
    last_name = forms.CharField(label="Surname", max_length=150,
                                widget=forms.TextInput(attrs={"autocomplete": "family-name"}))
    password = forms.CharField(
        label="Choose a password",
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )
    password_confirm = forms.CharField(
        label="Confirm password",
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )
    understood = forms.BooleanField(
        label="I understand I will own this school's account, its records and its billing.",
    )

    def __init__(self, transfer, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.transfer = transfer
        if not self.is_bound:
            first, _, last = transfer.to_name.partition(" ")
            self.initial.update({"first_name": first, "last_name": last})

    def clean(self):
        cleaned = super().clean()
        password = cleaned.get("password")
        if password:
            # Compared against the account it will be, so the similarity
            # validator can see the name and email.
            candidate = get_user_model()(
                email=self.transfer.to_email,
                first_name=cleaned.get("first_name", ""),
                last_name=cleaned.get("last_name", ""),
            )
            try:
                validate_password(password, candidate)
            except ValidationError as error:
                self.add_error("password", error)
            if cleaned.get("password_confirm") and password != cleaned["password_confirm"]:
                self.add_error("password_confirm", "The two passwords do not match.")
        return cleaned


class AcceptAsStaffForm(forms.Form):
    """Someone already on the staff, signed in: just yes."""

    understood = forms.BooleanField(
        label="I understand I will own this school's account, its records and its billing.",
    )
