"""Account screens: Account settings, and the password generator behind Generate.

Generation lives on the server so there is exactly one implementation of the
policy. A JavaScript generator would be a second definition of "strong enough"
that no Django test could reach, and the two would drift the first time the
policy changed. Here the button and the form agree because they call the same
:func:`~apps.accounts.passwords.generate_password`, which in turn calls the same
validators the form does.

The endpoint has to be reachable signed-out -- signup is where it is needed
most -- so it is throttled. It is cheap in itself, but in production each call
runs the policy, and the policy includes a request to Have I Been Pwned; an
open, unthrottled endpoint would let anyone use this deployment to generate
traffic against theirs.
"""

from __future__ import annotations

import logging

from django.contrib import messages
from django.contrib.auth import get_user_model, update_session_auth_hash
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.cache import cache
from django.db import transaction
from django.http import HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST
from django.views.generic import FormView, ListView, TemplateView, View

from django.urls import reverse, reverse_lazy

from apps.core.emails import send_password_changed_email
from apps.core.navigation import home_url_for
from apps.core.ratelimit import client_ip
from apps.core.permissions import (
    Capability,
    CapabilityRequiredMixin,
    capabilities_of,
    is_protected_account,
    may_deactivate,
)
from apps.core.roles import Role, is_platform_role

from .forms import (
    AccountPasswordForm,
    EmailChangeForm,
    ProfileForm,
    StaffAccountForm,
    StaffRowFormSet,
)
from .invites import send_set_password_email
from .passwords import generate_password

logger = logging.getLogger(__name__)

#: Generous next to how often a human presses a button, low enough to be
#: useless for amplification.
RATE_LIMIT = 20
RATE_WINDOW_SECONDS = 60


def plan_summary(user) -> dict | None:
    """The school's plan, for a school owner's account settings; ``None`` for anyone else.

    The plan is the proprietor's decision (MANAGE_SCHOOL_PROFILE, as the checkout
    requires), so principals and bursars do not see this section.
    """
    from apps.core.permissions import Capability, has_capability
    from apps.schools.billing import OPEN_STATUSES
    from apps.schools.models import PlanOrder
    from apps.schools.plans import offer_for
    from apps.students.models import Student, StudentStatus

    school = getattr(user, "school", None)
    if school is None or not has_capability(user, Capability.MANAGE_SCHOOL_PROFILE):
        return None
    offer = offer_for(school.plan)
    students = Student.all_objects.filter(
        branch__school=school, status=StudentStatus.ACTIVE
    ).count()
    ceiling = offer.max_students if offer else None
    return {
        "name": offer.name if offer else school.get_plan_display(),
        "price": offer.price_display if offer else None,
        "per_term": bool(offer and offer.price_naira),
        "students": students,
        "max_students": ceiling,
        "percent": min(int(students * 100 / ceiling), 100) if ceiling else None,
        "over": bool(ceiling and students > ceiling),
        "status": school.get_status_display(),
        "open_order": PlanOrder.all_objects.filter(school=school, status__in=OPEN_STATUSES)
        .order_by("-created_at")
        .first(),
    }


class AccountSettingsView(LoginRequiredMixin, TemplateView):
    """Anyone's own account: how they are named, their email, their password.

    Every role has one, because every account has all three. Three forms on one
    page, told apart by a hidden ``form`` field, so a failed password change does
    not wipe a half-typed email or a half-corrected surname.

    Also where an account on a temporary password is held (see
    RequirePasswordChangeMiddleware); choosing a new one sends them on to their
    dashboard.
    """

    template_name = "accounts/settings.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        context.setdefault("profile_form", ProfileForm(instance=user))
        context.setdefault("email_form", EmailChangeForm(user))
        context.setdefault("password_form", AccountPasswordForm(user))
        context["temporary_password"] = user.must_change_password
        context["plan"] = plan_summary(user)
        context["page_title"] = "Account settings"
        return context

    def post(self, request, *args, **kwargs):
        user = request.user
        submitted = request.POST.get("form")

        if submitted == "profile":
            form = ProfileForm(request.POST, instance=user)
            if form.is_valid():
                form.save()
                messages.success(
                    request,
                    f"Saved. You appear as {user.get_full_name()} across the app.",
                )
                return redirect("accounts:settings")
            return self.render_to_response(self.get_context_data(profile_form=form))

        if submitted == "email":
            form = EmailChangeForm(user, request.POST)
            if form.is_valid():
                form.save()
                messages.success(request, f"Your email address is now {user.email}.")
                return redirect("accounts:settings")
            return self.render_to_response(self.get_context_data(email_form=form))

        form = AccountPasswordForm(user, request.POST)
        if form.is_valid():
            was_temporary = user.must_change_password
            form.save()
            # A password change rotates the session hash; without this the
            # person who just changed it would be signed out.
            update_session_auth_hash(request, form.user)
            send_password_changed_email(form.user)
            messages.success(
                request,
                f"Password changed. We have emailed {user.email} to let you know."
                if user.email
                else "Password changed.",
            )
            if was_temporary:
                return HttpResponseRedirect(home_url_for(form.user))
            return redirect("accounts:settings")
        return self.render_to_response(self.get_context_data(password_form=form))


@require_POST
def generate_password_view(request):
    """Return one generated password as JSON.

    POST rather than GET so it is CSRF-checked and never cached by anything in
    front of it -- a cached response here would hand the same password to
    everyone who asked.
    """
    key = f"pwgen:{client_ip(request)}"
    # add() only succeeds on the first call in the window, which is what starts
    # the clock; incr() after that. The pair is not atomic across processes, so
    # a multi-worker deployment enforces this per worker -- fine for a limit
    # whose job is to bound amplification, not to be exact.
    cache.add(key, 0, RATE_WINDOW_SECONDS)
    try:
        used = cache.incr(key)
    except ValueError:
        # The key expired between add() and incr(). Treat as the first call.
        used = 1

    if used > RATE_LIMIT:
        logger.warning("Password generation rate limit hit for %s", key)
        return JsonResponse(
            {"error": "Too many requests. Wait a moment and try again."},
            status=429,
        )

    try:
        password = generate_password()
    except RuntimeError:
        # The policy has become unsatisfiable by the generator's shape. Log it
        # with the traceback and let the user type their own rather than
        # showing them a 500.
        logger.exception("Password generator could not satisfy the policy")
        return JsonResponse(
            {"error": "Could not generate a password. Please type one."},
            status=503,
        )

    return JsonResponse({"password": password})


class StaffListView(CapabilityRequiredMixin, ListView):
    """Who can sign in -- at this school, or across the platform.

    One screen answering two questions, because they are the same list at two
    scopes. A school sees its own staff. The platform sees every account on the
    product, which is why it gets a School column and the deactivate control and
    why its nav entry says "Users" rather than "Staff" -- see
    apps/core/navigation.py.

    Like Branches, this used to be a link to the Django admin -- unreachable
    for the proprietor, whose account is not ``is_staff``, and not tenant-scoped
    if it had been. ``User`` has no scoped manager of its own (see
    apps/core/tenancy.py on why ``User.objects`` is deliberately unfiltered), so
    this is one of the few querysets that has to narrow itself, and it does so
    from the signed-in account's own school rather than from anything in the URL.
    """

    capability = Capability.VIEW_STAFF
    template_name = "accounts/staff_list.html"
    context_object_name = "staff"

    def is_platform(self) -> bool:
        """Whether this caller is looking at the whole platform.

        Read from the effective role rather than from ``user.role``, so a Django
        superuser -- promoted to platform scope everywhere else -- is treated the
        same here.
        """
        context = getattr(self.request, "tenant", None)
        role = context.role if context is not None else self.request.user.role
        return is_platform_role(role)

    def get_queryset(self):
        user = self.request.user
        people = get_user_model().objects.select_related("school", "branch")
        # A platform owner has no school of their own and sees everybody;
        # anyone else sees exactly their own school's accounts.
        if user.school_id is not None:
            people = people.filter(school_id=user.school_id)
        elif not self.is_platform():
            people = people.none()
        if self.is_platform():
            # Grouped by school first: at platform scope the list is read one
            # school at a time, and role order across thirty schools is noise.
            return people.order_by("school__name", "role", "username")
        return people.order_by("role", "username")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        platform = self.is_platform()
        context["is_platform"] = platform
        context["page_title"] = "Users" if platform else "Staff"
        context["noun"] = "account" if platform else "staff account"
        context["can_deactivate"] = "deactivate_accounts" in {
            c.value for c in capabilities_of(self.request.user)
        }
        # Decided per row rather than in the template, so the button is drawn in
        # exactly the places the view would allow it -- one answer, not two that
        # can drift. `object_list` is the page of accounts ListView resolved.
        context["rows"] = [
            {
                "person": person,
                "may_deactivate": may_deactivate(self.request.user, person),
                "is_protected": is_protected_account(person),
            }
            for person in context["staff"]
        ]
        context["role_labels"] = dict(Role.choices)
        return context


class DeactivateAccountsMixin(CapabilityRequiredMixin):
    """The platform, and nobody at a school. See Capability.DEACTIVATE_ACCOUNTS."""

    capability = Capability.DEACTIVATE_ACCOUNTS


class StaffActivationView(DeactivateAccountsMixin, View):
    """Switch an account off, or back on. Never deletes it.

    The same shape as the module switches: POST only, and it names the state it
    wants rather than flipping whatever it finds -- a stale tab must not be able
    to reactivate somebody by pressing what it thinks is "deactivate".

    A deactivated account keeps every row it is attached to. ``is_active=False``
    is Django's own "may not sign in", so the person is locked out at the
    authentication backend rather than by anything this codebase has to remember
    to check, and the payments they recorded still name them.
    """

    def post(self, request, pk, *args, **kwargs):
        person = get_object_or_404(get_user_model().objects.all(), pk=pk)
        active = request.POST.get("active") == "on"

        if person.pk == request.user.pk:
            # Not a permission question, a footgun one: the platform owner
            # switching off their own account would be locked out of the screen
            # that could switch it back on.
            messages.error(request, "You cannot deactivate your own account.")
            return HttpResponseRedirect(reverse("staff:list"))

        if is_protected_account(person):
            # The one account out of reach of this, so the platform cannot be
            # locked out of itself -- see permissions.is_protected_account.
            messages.error(
                request,
                "That account cannot be deactivated. It is the platform's own "
                "owner account, and locking it out would leave nobody able to "
                "undo it.",
            )
            return HttpResponseRedirect(reverse("staff:list"))

        if person.is_active == active:
            messages.info(
                request,
                f"{person.get_full_name() or person.username} was already "
                f"{'active' if active else 'deactivated'}.",
            )
            return HttpResponseRedirect(reverse("staff:list"))

        person.is_active = active
        person.save(update_fields=["is_active"])
        who = person.get_full_name() or person.username
        if active:
            messages.success(
                request, f"{who} can sign in again. Their password is unchanged."
            )
        else:
            messages.warning(
                request,
                f"{who} can no longer sign in. Nothing of theirs has been "
                f"deleted, and switching them back on restores their access.",
            )
        return HttpResponseRedirect(reverse("staff:list"))

class ManageStaffMixin(CapabilityRequiredMixin):
    """The proprietor, and the platform on their behalf. Not a bursar."""

    capability = Capability.MANAGE_STAFF


class StaffCreateView(ManageStaffMixin, FormView):
    """Create a login for a member of staff and email them a link to set it.

    The invitation is the same machinery the invite_school_owner command uses,
    and therefore the same email a "forgot password" produces -- one message to
    keep working rather than two that drift apart.
    """

    template_name = "accounts/staff_form.html"
    form_class = StaffAccountForm
    success_url = reverse_lazy("staff:list")
    extra_context = {"page_title": "New staff account"}

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["creator"] = self.request.user
        return kwargs

    def get_success_url(self):
        """Back to a blank form when asked, rather than to the list.

        The second and third account of a batch are the ones this saves; the
        bulk table is the better answer for more than that.
        """
        if "save_and_add_another" in self.request.POST:
            return reverse("staff:create")
        return super().get_success_url()

    def form_valid(self, form):
        user = form.save()
        # Outside the transaction that made the account on purpose: a mail
        # failure must not roll back a user who now exists as far as the
        # proprietor is concerned. If it fails they are told, and the Resend
        # button on the staff list is the fix.
        try:
            send_set_password_email(user)
        except Exception:
            logger.exception("Invite email failed for %s", user.pk)
            messages.warning(
                self.request,
                f"{user.get_full_name() or user.username} was created, but the "
                f"invitation email to {user.email} could not be sent. Use "
                f"Resend invite to try again.",
            )
        else:
            messages.success(
                self.request,
                f"{user.get_full_name() or user.username} can now sign in as "
                f"{user.get_role_display().lower()}. A link to set their own "
                f"password has been emailed to {user.email}.",
            )
        return HttpResponseRedirect(self.get_success_url())


class StaffBulkCreateView(ManageStaffMixin, TemplateView):
    """Issue several logins in one submission.

    A school opening for the term hands out a handful of accounts at once, and
    doing it one form at a time means finding the New button again between
    each. Same validation, same creation path, same invitation email as the
    single form -- see StaffRowForm.

    The accounts are created in one transaction and the emails are sent after
    it commits, which is the same order the single form uses and for the same
    reason: a mail failure must not roll back a colleague who now exists as far
    as the proprietor is concerned.
    """

    template_name = "accounts/staff_bulk_form.html"
    extra_context = {"page_title": "Invite staff"}

    def build_formset(self, data=None):
        return StaffRowFormSet(
            data=data, form_kwargs={"creator": self.request.user}
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.setdefault("formset", self.build_formset())
        return context

    def post(self, request, *args, **kwargs):
        formset = self.build_formset(data=request.POST)
        if not formset.is_valid():
            return self.render_to_response(self.get_context_data(formset=formset))

        with transaction.atomic():
            created = [form.save() for form in formset.filled_forms()]

        # Outside the transaction on purpose. Each failure is reported by name
        # so the proprietor knows exactly who to press Resend for, rather than
        # being told "some emails failed".
        failed = []
        for user in created:
            try:
                send_set_password_email(user)
            except Exception:
                logger.exception("Invite email failed for %s", user.pk)
                failed.append(user)

        sent = len(created) - len(failed)
        if sent:
            messages.success(
                request,
                f"{sent} account{'' if sent == 1 else 's'} created. A link to "
                f"set their own password has been emailed to "
                f"{'them' if sent > 1 else created[0].email}.",
            )
        if failed:
            names = ", ".join(u.get_full_name() or u.username for u in failed)
            messages.warning(
                request,
                f"Created, but the invitation email could not be sent to: "
                f"{names}. Use Resend invite on the staff list to try again.",
            )

        if "save_and_add_another" in request.POST:
            return HttpResponseRedirect(request.path)
        return HttpResponseRedirect(reverse("staff:list"))


class ResendStaffInviteView(ManageStaffMixin, View):
    """Send the set-a-password link again.

    POST only, and scoped to the caller's own school: an id from another
    tenant is a 404 here rather than an email sent to a stranger.
    """

    def post(self, request, *args, **kwargs):
        school = getattr(request.user, "school", None)
        people = get_user_model().objects.all()
        if school is not None:
            people = people.filter(school_id=school.pk)
        elif not request.user.is_superuser:
            people = people.none()
        user = get_object_or_404(people, pk=kwargs["pk"])
        try:
            send_set_password_email(user)
        except Exception:
            logger.exception("Invite resend failed for %s", user.pk)
            messages.error(
                request, f"The email to {user.email} could not be sent."
            )
        else:
            messages.success(request, f"A new link was emailed to {user.email}.")
        return HttpResponseRedirect(reverse("staff:list"))

