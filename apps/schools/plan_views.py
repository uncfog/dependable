"""Paying for a plan by bank transfer.

A school owner picks a paid plan and gets an order with a reference and the
school's bank details. They pay from their own bank with the reference as the
narration, then report the transfer. The platform owner confirms it in the
admin, and only then does the school's plan change.

Nothing here moves money. The transfer happens at the school's bank, outside
the app.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from django.conf import settings
from django.contrib import messages
from django.db.models import Count
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView

from apps.core.branding import SUPPORT_EMAIL
from apps.core.permissions import Capability, CapabilityRequiredMixin
from apps.core.views import RoleDashboardMixin

from .models import Plan, PlanOrder, School
from .plans import OFFERS, offer_for

from .billing import OPEN_STATUSES, confirm_order


def bank_details() -> dict[str, str]:
    return {
        "bank": settings.BILLING_BANK_NAME,
        "account_name": settings.BILLING_ACCOUNT_NAME,
        "account_number": settings.BILLING_ACCOUNT_NUMBER,
    }


def bank_details_ready() -> bool:
    """Blank settings mean nobody can be told where to pay, so no order is taken."""
    return all(bank_details().values())


def _new_reference() -> str:
    return f"PLN-{uuid.uuid4().hex[:10].upper()}"


class ManagePlanMixin(CapabilityRequiredMixin):
    """Owner level only. The plan is the proprietor's decision, like the profile."""

    capability = Capability.MANAGE_SCHOOL_PROFILE

    def _school(self):
        school = getattr(self.request.user, "school", None)
        if school is None:
            raise Http404("This account is not attached to a school.")
        return school


class PlanCheckoutView(ManagePlanMixin, TemplateView):
    template_name = "schools/plan_checkout.html"

    def get_context_data(self, **kwargs):
        open_order = (
            PlanOrder.objects.filter(status__in=OPEN_STATUSES)
            .order_by("-created_at")
            .first()
        )
        return super().get_context_data(
            school=self._school(),
            offers=OFFERS,
            open_order=open_order,
            bank_ready=bank_details_ready(),
            support_email=SUPPORT_EMAIL,
            page_title="Plan",
            **kwargs,
        )

    def post(self, request, *args, **kwargs):
        school = self._school()
        offer = offer_for(request.POST.get("plan", ""))
        if offer is None or not offer.orderable or offer.plan == school.plan:
            messages.error(request, "That plan can't be bought here.")
            return redirect("billing:checkout")
        if not bank_details_ready():
            messages.error(
                request,
                "Bank details aren't set up yet, so plans can't be paid for here. "
                "Contact support.",
            )
            return redirect("billing:checkout")

        # Reuse an open order for the same plan rather than stacking up duplicates.
        order = (
            PlanOrder.objects.filter(plan=offer.plan, status__in=OPEN_STATUSES)
            .order_by("-created_at")
            .first()
        )
        if order is None:
            order = PlanOrder.objects.create(
                school=school,
                plan=offer.plan,
                amount_naira=offer.price_naira,
                reference=_new_reference(),
            )
        return redirect("billing:order", reference=order.reference)


class PlanOrderView(ManagePlanMixin, TemplateView):
    template_name = "schools/plan_order.html"

    def get_context_data(self, **kwargs):
        order = get_object_or_404(PlanOrder.objects, reference=self.kwargs["reference"])
        return super().get_context_data(
            order=order,
            offer=offer_for(order.plan),
            bank=bank_details(),
            bank_ready=bank_details_ready(),
            page_title="Pay for your plan",
            **kwargs,
        )


class PlanOrderReportView(ManagePlanMixin, View):
    """The school says it has sent the transfer. Only the platform owner can confirm it."""

    def post(self, request, reference):
        order = get_object_or_404(PlanOrder.objects, reference=reference)
        if order.status == PlanOrder.Status.AWAITING_TRANSFER:
            order.status = PlanOrder.Status.TRANSFER_REPORTED
            order.transfer_reported_at = timezone.now()
            order.save(update_fields=["status", "transfer_reported_at", "updated_at"])
            messages.success(
                request,
                "Thanks. We'll switch your plan on once the payment arrives.",
            )
        return redirect("billing:order", reference=order.reference)


def plan_cards(counts: dict[str, int] | None = None) -> list[SimpleNamespace]:
    """The offers as the pricing cards draw them, with how many schools are on each."""
    return [
        SimpleNamespace(
            plan=offer.plan, name=offer.name, students=offer.students,
            price_naira=offer.price_naira, price_display=offer.price_display,
            school_count=(counts or {}).get(offer.plan, 0),
        )
        for offer in OFFERS
    ]


class PlatformBillingView(RoleDashboardMixin, TemplateView):
    """The platform owner's Plans & billing: the price list the landing page and the
    checkout share, the transfers waiting to be confirmed, and every school's plan."""

    template_name = "schools/platform_billing.html"
    home_url_name = "core:platform_overview"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        counts = dict(
            School.objects.values_list("plan").annotate(n=Count("id")).values_list("plan", "n")
        )
        orders = PlanOrder.all_objects.select_related("school")
        context.update(
            plan_offers=plan_cards(counts),
            waiting=list(
                orders.filter(status__in=OPEN_STATUSES).order_by("-status", "created_at")
            ),
            recently_paid=list(orders.filter(status=PlanOrder.Status.PAID).order_by("-paid_at")[:10]),
            schools=list(School.objects.order_by("name")),
            unlisted_plans=[
                (Plan(plan).label, n) for plan, n in counts.items() if offer_for(plan) is None
            ],
            bank_ready=bank_details_ready(),
            page_title="Plans & billing",
        )
        return context


class PlatformConfirmOrderView(RoleDashboardMixin, View):
    """Confirm one transfer from the Plans & billing screen."""

    home_url_name = "core:platform_overview"

    def post(self, request, pk):
        order = get_object_or_404(PlanOrder.all_objects.select_related("school"), pk=pk)
        if confirm_order(order):
            messages.success(
                request,
                f"{order.school.name} is now on the {offer_for(order.plan).name} plan.",
            )
        else:
            messages.info(request, "That order was already paid or cancelled.")
        return redirect("core:platform_billing")
