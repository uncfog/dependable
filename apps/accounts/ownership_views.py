"""The three screens of an ownership transfer: start, cancel, accept.

Start and cancel are the owner's, from Account settings. Accept is public -- it is
opened from an email by someone who may have no account yet -- and the token in
its URL is the only thing that lets anyone in.
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseRedirect
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from django.views.generic import FormView, View

from apps.core.navigation import home_url_for
from apps.core.roles import Role

from .models import OwnershipTransfer
from .ownership import (
    NewAccount,
    TransferError,
    accept_transfer,
    cancel_transfer,
    decline_transfer,
    hash_token,
    open_transfer,
    recipient_for,
    start_transfer,
)
from .ownership_forms import AcceptAsNewForm, AcceptAsStaffForm, StartTransferForm

Status = OwnershipTransfer.Status

#: How Django names the one authentication backend, for logging in a new owner.
BACKEND = "apps.accounts.backends.EmailOrUsernameBackend"


def site_origin(request) -> str:
    """Links in mail point back at the site the owner is using."""
    return f"{request.scheme}://{request.get_host()}"


def is_school_owner(user) -> bool:
    return bool(
        user.is_authenticated
        and user.is_active
        and user.role == Role.SCHOOL_OWNER
        and user.school_id
    )


class SchoolOwnerRequiredMixin(LoginRequiredMixin):
    """The school's owner, and nobody else -- not even the platform, which has a command."""

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not is_school_owner(request.user):
            raise PermissionDenied("Only the school's owner can hand the school over.")
        return super().dispatch(request, *args, **kwargs)


def settings_url() -> str:
    return reverse("accounts:settings") + "#ownership"


class StartTransferView(SchoolOwnerRequiredMixin, FormView):
    template_name = "accounts/ownership_transfer.html"
    form_class = StartTransferForm

    def dispatch(self, request, *args, **kwargs):
        if is_school_owner(request.user) and open_transfer(request.user.school) is not None:
            messages.info(
                request,
                "A transfer is already waiting to be accepted. Cancel it to start another.",
            )
            return HttpResponseRedirect(settings_url())
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["owner"] = self.request.user
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "Transfer ownership"
        context["school"] = self.request.user.school
        return context

    def form_valid(self, form):
        data = form.cleaned_data
        try:
            started = start_transfer(
                school=self.request.user.school,
                email=data["email"],
                name=data["full_name"],
                afterwards=data["afterwards"],
                stay_branch=data.get("stay_branch"),
                started_by=self.request.user,
                origin=site_origin(self.request),
            )
        except TransferError as error:
            form.add_error(None, str(error))
            return self.form_invalid(form)
        transfer = started.transfer
        if started.emailed:
            messages.success(
                self.request,
                f"We have emailed {transfer.to_email}. Nothing changes until "
                f"{transfer.to_name} accepts, and you can cancel until then.",
            )
        else:
            messages.warning(
                self.request,
                f"The transfer is open, but the email to {transfer.to_email} could "
                f"not be sent. Cancel it and try again, or ask the platform for help.",
            )
        return HttpResponseRedirect(settings_url())


class CancelTransferView(SchoolOwnerRequiredMixin, View):
    def post(self, request, *args, **kwargs):
        transfer = open_transfer(request.user.school)
        if transfer is None:
            messages.info(request, "There was no transfer waiting, so nothing was cancelled.")
            return HttpResponseRedirect(settings_url())
        try:
            cancel_transfer(transfer, by=request.user)
        except TransferError as error:
            messages.error(request, str(error))
        else:
            messages.success(
                request,
                f"Cancelled. The link sent to {transfer.to_email} no longer works.",
            )
        return HttpResponseRedirect(settings_url())


@method_decorator(never_cache, name="dispatch")
class AcceptTransferView(View):
    """What the emailed link opens. Every state the link can be in, on one page.

    Rendered in the sign-in page's shell whether or not anyone is signed in
    (``public_preview``), so the person reads the same page in every case.
    """

    template_name = "accounts/ownership_accept.html"

    def get_transfer(self, token):
        return (
            OwnershipTransfer.all_objects.select_related("school", "started_by")
            .filter(token_hash=hash_token(token))
            .first()
        )

    def state(self, request, transfer):
        """``(state, existing account or None)``. See the template for each state."""
        if transfer is None:
            return "invalid", None
        if not transfer.is_open:
            return "closed", None
        try:
            existing = recipient_for(transfer.school, transfer.to_email)
        except TransferError:
            return "unavailable", None
        user = request.user
        if existing is not None:
            if not user.is_authenticated:
                return "sign_in", existing
            if user.pk != existing.pk:
                return "wrong_account", existing
            return "staff", existing
        if user.is_authenticated:
            return "wrong_account", None
        return "new", None

    def render_page(self, request, transfer, state, form=None, status=200, error=None):
        context = {
            "transfer": transfer,
            "state": state,
            "form": form,
            "error": error,
            "public_preview": True,
            "page_title": "Take over a school",
            "login_url": f"{reverse('login')}?next={request.path}",
        }
        response = render(request, self.template_name, context, status=status)
        # The token is in this URL: no other site gets it in a Referer header.
        # Not "no-referrer": that makes a browser send `Origin: null` with this
        # page's own forms, and Django's CSRF check refuses every one of them.
        response["Referrer-Policy"] = "same-origin"
        return response

    def get(self, request, token):
        transfer = self.get_transfer(token)
        state, _ = self.state(request, transfer)
        form = None
        if state == "new":
            form = AcceptAsNewForm(transfer)
        elif state == "staff":
            form = AcceptAsStaffForm()
        return self.render_page(request, transfer, state, form,
                                status=404 if state == "invalid" else 200)

    def post(self, request, token):
        transfer = self.get_transfer(token)
        state, existing = self.state(request, transfer)
        if state not in {"new", "staff"}:
            return self.render_page(request, transfer, state,
                                    status=404 if state == "invalid" else 200)

        if request.POST.get("action") == "decline":
            try:
                decline_transfer(transfer, origin=site_origin(request))
            except TransferError as error:
                return self.render_page(request, transfer, "closed", error=str(error))
            transfer.refresh_from_db()
            return self.render_page(request, transfer, "declined")

        if state == "new":
            form = AcceptAsNewForm(transfer, request.POST)
            if not form.is_valid():
                return self.render_page(request, transfer, state, form)
            account = NewAccount(
                first_name=form.cleaned_data["first_name"],
                last_name=form.cleaned_data["last_name"],
                password=form.cleaned_data["password"],
            )
            kwargs = {"new_account": account}
        else:
            form = AcceptAsStaffForm(request.POST)
            if not form.is_valid():
                return self.render_page(request, transfer, state, form)
            kwargs = {"user": request.user}

        try:
            owner = accept_transfer(transfer, origin=site_origin(request), **kwargs)
        except TransferError as error:
            transfer.refresh_from_db()
            return self.render_page(request, transfer, self.state(request, transfer)[0],
                                    form, error=str(error))

        if state == "new":
            login(request, owner, backend=BACKEND)
        messages.success(
            request,
            f"You now own {transfer.school.name}. Everything the school had is here, "
            f"unchanged.",
        )
        return redirect(home_url_for(owner))
