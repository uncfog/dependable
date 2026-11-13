"""The screens of a role handover. The rules are in ``apps.accounts.handover``."""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from django.views.generic import FormView, View

from apps.core.forms import StyledFormMixin
from apps.core.navigation import home_url_for

from . import handover as service
from .handover import HandoverError
from .models import RoleHandover
from .ownership import NewAccount, hash_token
from .ownership_views import BACKEND, is_school_owner, site_origin

Status = RoleHandover.Status


def _settings(anchor: str) -> str:
    return reverse("accounts:settings") + anchor


class StartForm(StyledFormMixin, forms.Form):
    full_name = forms.CharField(
        label="Their full name", max_length=150,
        widget=forms.TextInput(attrs={"autocomplete": "off"}),
    )
    email = forms.EmailField(
        label="Their email address",
        help_text="The link to accept goes here, and they sign in with it afterwards.",
        widget=forms.EmailInput(attrs={"autocomplete": "off"}),
    )
    password = forms.CharField(
        label="Your password", help_text="To confirm it is you.",
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}),
    )

    def __init__(self, user, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user

    def clean_full_name(self):
        return " ".join(self.cleaned_data["full_name"].split())

    def clean_email(self):
        try:
            return service.check_recipient(self.cleaned_data["email"], asker=self.user)
        except HandoverError as error:
            raise ValidationError(str(error))

    def clean_password(self):
        if not self.user.check_password(self.cleaned_data["password"]):
            raise ValidationError("That is not your password.")
        return self.cleaned_data["password"]


class StartHandoverView(LoginRequiredMixin, FormView):
    template_name = "accounts/handover_start.html"
    form_class = StartForm

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            if not service.may_hand_over(request.user):
                raise PermissionDenied("Only a principal or bursar can hand over their role here.")
            if service.open_for(request.user) is not None:
                messages.info(request, "You already have a handover waiting. Cancel it to start another.")
                return HttpResponseRedirect(_settings("#handover"))
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "user": self.request.user}

    def get_context_data(self, **kwargs):
        return super().get_context_data(page_title="Hand over your role", **kwargs)

    def form_valid(self, form):
        try:
            handover = service.request_handover(
                user=self.request.user, email=form.cleaned_data["email"],
                name=form.cleaned_data["full_name"], origin=site_origin(self.request),
            )
        except HandoverError as error:
            form.add_error(None, str(error))
            return self.form_invalid(form)
        messages.success(
            self.request,
            f"Sent to the school owner for approval. Nothing changes until they approve "
            f"and {handover.to_name} accepts.",
        )
        return HttpResponseRedirect(_settings("#handover"))


class CancelHandoverView(LoginRequiredMixin, View):
    def post(self, request):
        handover = service.open_for(request.user)
        if handover is None:
            messages.info(request, "There was no handover waiting, so nothing was cancelled.")
        else:
            try:
                service.cancel(handover, by=request.user)
                messages.success(request, "Handover cancelled. Any link that was sent no longer works.")
            except HandoverError as error:
                messages.error(request, str(error))
        return HttpResponseRedirect(_settings("#handover"))


class DecideHandoverView(LoginRequiredMixin, View):
    """The owner approves or turns down a request from their own school."""

    def post(self, request, pk):
        if not is_school_owner(request.user):
            raise PermissionDenied("Only the school's owner can decide a handover.")
        handover = get_object_or_404(RoleHandover, pk=pk, school_id=request.user.school_id)
        try:
            if request.POST.get("decision") == "approve":
                emailed = service.approve(handover, by=request.user, origin=site_origin(request))
                if emailed:
                    messages.success(request, f"Approved. We have emailed {handover.to_email} the link to accept.")
                else:
                    messages.warning(request, f"Approved, but the email to {handover.to_email} could not be sent.")
            else:
                service.reject(handover, by=request.user, origin=site_origin(request))
                messages.success(request, "Not approved. Nothing has changed, and they have been told.")
        except HandoverError as error:
            messages.error(request, str(error))
        return HttpResponseRedirect(_settings("#handovers"))


class AcceptForm(StyledFormMixin, forms.Form):
    first_name = forms.CharField(label="First name", max_length=150,
                                 widget=forms.TextInput(attrs={"autocomplete": "given-name"}))
    last_name = forms.CharField(label="Surname", max_length=150,
                                widget=forms.TextInput(attrs={"autocomplete": "family-name"}))
    password = forms.CharField(label="Choose a password",
                               widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}))
    password_confirm = forms.CharField(label="Confirm password",
                                       widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}))

    def __init__(self, handover, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.handover = handover
        if not self.is_bound:
            first, _, last = handover.to_name.partition(" ")
            self.initial.update({"first_name": first, "last_name": last})

    def clean(self):
        cleaned = super().clean()
        password = cleaned.get("password")
        if password:
            candidate = get_user_model()(
                email=self.handover.to_email, first_name=cleaned.get("first_name", ""),
                last_name=cleaned.get("last_name", ""),
            )
            try:
                validate_password(password, candidate)
            except ValidationError as error:
                self.add_error("password", error)
            if cleaned.get("password_confirm") and password != cleaned["password_confirm"]:
                self.add_error("password_confirm", "The two passwords do not match.")
        return cleaned


@method_decorator(never_cache, name="dispatch")
class AcceptHandoverView(View):
    """What the emailed link opens. States: invalid, closed, signed_in, new, declined."""

    template_name = "accounts/handover_accept.html"

    def find(self, token):
        service.expire_lapsed()
        return (
            RoleHandover.objects.select_related("school", "branch", "from_user")
            .filter(token_hash=hash_token(token)).exclude(token_hash="").first()
        )

    def state(self, request, handover) -> str:
        if handover is None:
            return "invalid"
        if not handover.is_acceptable:
            return "closed"
        return "signed_in" if request.user.is_authenticated else "new"

    def page(self, request, handover, state, form=None, error=None):
        response = render(request, self.template_name, {
            "handover": handover, "state": state, "form": form, "error": error,
            "public_preview": True, "page_title": "Take over a role",
        }, status=404 if state == "invalid" else 200)
        # The token is in this URL: no other site gets it in a Referer header.
        response["Referrer-Policy"] = "same-origin"
        return response

    def get(self, request, token):
        handover = self.find(token)
        state = self.state(request, handover)
        return self.page(request, handover, state, AcceptForm(handover) if state == "new" else None)

    def post(self, request, token):
        handover = self.find(token)
        state = self.state(request, handover)
        if state != "new":
            return self.page(request, handover, state)
        if request.POST.get("action") == "decline":
            try:
                service.decline(handover, origin=site_origin(request))
            except HandoverError as error:
                return self.page(request, handover, "closed", error=str(error))
            return self.page(request, handover, "declined")
        form = AcceptForm(handover, request.POST)
        if not form.is_valid():
            return self.page(request, handover, state, form)
        try:
            newcomer = service.accept(
                handover, origin=site_origin(request),
                new_account=NewAccount(
                    first_name=form.cleaned_data["first_name"],
                    last_name=form.cleaned_data["last_name"],
                    password=form.cleaned_data["password"],
                ),
            )
        except HandoverError as error:
            handover.refresh_from_db()
            return self.page(request, handover, self.state(request, handover), form, error=str(error))
        login(request, newcomer, backend=BACKEND)
        messages.success(
            request,
            f"You are now {handover.get_role_display()} at {handover.branch.name}. "
            f"Everything already recorded is here, unchanged.",
        )
        return redirect(home_url_for(newcomer))
