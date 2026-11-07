"""Family payments: receive the money, share it out, correct it, print it.

The rules live in :mod:`apps.payments.allocation`. These screens ask it and report
what it says. Nothing here filters by school or campus: the scoped managers have.
"""

from __future__ import annotations

from decimal import Decimal

from django.contrib import messages as flash
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.views.generic import FormView, ListView, View

from apps.audit.recording import history_for
from apps.core.files import DOCUMENT_TYPES, stream_stored_file
from apps.core.permissions import Capability, CapabilityRequiredMixin, has_capability
from apps.families.balances import summary_for
from apps.families.models import Family
from apps.students import fees

from . import allocation, balances, shared
from .forms import VoidForm
from .models import Payment, PaymentStatus, PaymentTransaction, TransactionKind
from .shared_forms import MoveForm, ReceiveForm, ShareForm

ZERO = Decimal("0")


def _naira(amount) -> str:
    return f"₦{amount:,.0f}" if amount == amount.to_integral() else f"₦{amount:,.2f}"


def _transaction(pk) -> PaymentTransaction:
    """A family payment the caller's campus can see. One-child ones live elsewhere."""
    return get_object_or_404(
        PaymentTransaction.objects.filter(kind=TransactionKind.FAMILY).select_related(
            "family", "branch__school", "recorded_by", "confirmed_by", "voided_by"
        ),
        pk=pk,
    )


def _visible_shares(txn):
    return (
        Payment.objects.filter(transaction=txn)
        .select_related("student", "student__school_class", "term")
        .order_by("id")
    )


def _shared_elsewhere(txn, shares) -> Decimal:
    """Money gone to children this account can't see. An amount, and never a name."""
    here = sum((s.amount for s in shares if s.status != PaymentStatus.VOID), ZERO)
    return txn.allocated - here


class SharedListView(CapabilityRequiredMixin, ListView):
    """Oldest first, because a queue is worked from the front."""

    capability = Capability.VIEW_PAYMENTS
    template_name = "payments/shared_list.html"
    context_object_name = "transactions"
    paginate_by = 25

    def get_queryset(self):
        rows = shared.transactions()
        family = self.request.GET.get("family", "")
        if family.isdigit():
            rows = rows.filter(family_id=family)
        if self.request.GET.get("show") == "all":
            return rows.order_by("-date_paid", "-id")
        return shared.waiting(rows).order_by("date_paid", "id")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["showing_all"] = self.request.GET.get("show") == "all"
        context["unallocated_total"] = shared.unallocated_total()
        context["page_title"] = "Family payments"
        return context


class SharedRecordView(CapabilityRequiredMixin, FormView):
    capability = Capability.RECORD_PAYMENTS
    form_class = ReceiveForm
    template_name = "payments/shared_form.html"
    extra_context = {"page_title": "Record a family payment"}

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def get_initial(self):
        initial = super().get_initial()
        requested = self.request.GET.get("family", "")
        if requested.isdigit():
            family = Family.objects.filter(pk=requested).first()
            if family is not None:
                initial["family"] = family
        return initial

    def form_valid(self, form):
        txn, repeated = form.save(actor=self.request.user)
        if repeated:
            flash.info(
                self.request,
                f"{_naira(txn.amount)} from {txn.family.name} was already recorded a "
                f"moment ago, so it was not added again.",
            )
        else:
            flash.success(
                self.request,
                f"{_naira(txn.amount)} recorded for {txn.family.name}. Now share it "
                f"between the children.",
            )
        return HttpResponseRedirect(txn.get_absolute_url())


class SharedDetailView(CapabilityRequiredMixin, View):
    """The payment, what is left of it, and the place to share it out."""

    capability = Capability.VIEW_PAYMENTS

    def get(self, request, pk):
        txn = _transaction(pk)
        return self._page(request, txn, self._form(request, txn))

    def post(self, request, pk):
        if not has_capability(request.user, Capability.RECORD_PAYMENTS):
            raise PermissionDenied
        txn = _transaction(pk)
        if txn.status == PaymentStatus.VOID:
            flash.error(request, "This payment has been voided.")
            return HttpResponseRedirect(txn.get_absolute_url())

        form = self._form(request, txn, data=request.POST)
        if form.is_out_of_date():
            # A second press lands here too. Empty boxes, so pressing again can't
            # repeat it, and the page below shows what is true now.
            return self._page(
                request, txn, self._form(request, txn),
                notice="This payment changed since you opened the page, so nothing was "
                       "saved. Check what is left below and share it again.",
            )
        offer_credit = False
        if form.is_valid():
            try:
                total = form.save(actor=request.user)
            except allocation.AllocationError as refused:
                form.add_error(None, f"{refused.student.full_name}: {refused}")
                offer_credit = isinstance(refused, allocation.WouldOverpay)
            else:
                flash.success(
                    request,
                    f"{_naira(total)} shared. {_naira(txn.unallocated)} is still not allocated.",
                )
                return HttpResponseRedirect(txn.get_absolute_url())
        return self._page(request, txn, form, offer_credit=offer_credit)

    def _form(self, request, txn, data=None):
        if txn.status == PaymentStatus.VOID:
            return None
        if not has_capability(request.user, Capability.RECORD_PAYMENTS):
            return None
        return ShareForm(data, txn=txn, children=summary_for(txn.family).children)

    def _page(self, request, txn, form, *, offer_credit=False, notice=""):
        shares = list(_visible_shares(txn))
        can_void = has_capability(request.user, Capability.VOID_PAYMENTS)
        return render(request, "payments/shared_detail.html", {
            "txn": txn,
            "share_form": form,
            "shares": shares,
            "elsewhere": _shared_elsewhere(txn, shares),
            "summary": summary_for(txn.family),
            "offer_credit": offer_credit,
            "notice": notice,
            "can_record": has_capability(request.user, Capability.RECORD_PAYMENTS),
            "can_void": can_void,
            "history": history_for("payments.PaymentTransaction", txn.pk),
            "page_title": f"Family payment {txn.number}",
        })


class SharedConfirmView(CapabilityRequiredMixin, View):
    """Accept money that was held. Its shares start counting in the same moment."""

    capability = Capability.RECORD_PAYMENTS

    def post(self, request, pk):
        txn = _transaction(pk)
        if txn.receipt and not request.POST.get("receipt_checked"):
            flash.error(request, "Check the slip against the family and the amount first.")
            return HttpResponseRedirect(txn.get_absolute_url())
        try:
            allocation.confirm(txn, actor=request.user)
        except allocation.AllocationError as refused:
            flash.error(request, str(refused))
        else:
            flash.success(request, f"{txn.number} confirmed.")
        return HttpResponseRedirect(txn.get_absolute_url())


class SharedVoidView(CapabilityRequiredMixin, FormView):
    """The whole sum was wrong, such as a bounced cheque. Every share goes with it."""

    capability = Capability.VOID_PAYMENTS
    form_class = VoidForm
    template_name = "payments/shared_confirm_void.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        txn = _transaction(self.kwargs["pk"])
        shares = [s for s in _visible_shares(txn) if s.status != PaymentStatus.VOID]
        context.update(
            txn=txn, shares=shares, elsewhere=_shared_elsewhere(txn, shares),
            page_title=f"Void {txn.number}",
        )
        return context

    def form_valid(self, form):
        txn = _transaction(self.kwargs["pk"])
        allocation.void_transaction(
            txn, actor=self.request.user, reason=form.cleaned_data["reason"]
        )
        flash.warning(
            self.request, f"{txn.number} voided, with every child's share of it."
        )
        return HttpResponseRedirect(txn.get_absolute_url())


class AllocationMoveView(CapabilityRequiredMixin, FormView):
    """Credit moves between siblings only like this: a logged void and a new share."""

    capability = Capability.VOID_PAYMENTS
    form_class = MoveForm
    template_name = "payments/allocation_move.html"

    def _payment(self) -> Payment:
        if not hasattr(self, "_found"):
            payment = get_object_or_404(
                Payment.objects.select_related("student", "transaction__family"),
                pk=self.kwargs["pk"],
            )
            if not (payment.transaction.is_shared and payment.transaction.family_id):
                raise Http404("That payment is for one child, so there is nothing to move.")
            self._found = payment
        return self._found

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        payment = self._payment()
        kwargs["children"] = [
            row for row in summary_for(payment.transaction.family).children
            if row.student.pk != payment.student_id
        ]
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        payment = self._payment()
        context["payment"] = payment
        context["position"] = fees.position_for(payment.student)
        context.setdefault("offer_credit", False)
        context["page_title"] = f"Move {payment.receipt_number}"
        return context

    def form_valid(self, form):
        payment = self._payment()
        if payment.status == PaymentStatus.VOID:
            flash.info(self.request, "That share has already been voided.")
            return HttpResponseRedirect(payment.get_absolute_url())
        try:
            moved = allocation.reallocate(
                payment, form.student(), actor=self.request.user,
                reason=form.cleaned_data["reason"],
                allow_credit=form.cleaned_data["allow_credit"],
            )
        except allocation.AllocationError as refused:
            form.add_error(None, str(refused))
            return self.render_to_response(
                self.get_context_data(
                    form=form, offer_credit=isinstance(refused, allocation.WouldOverpay)
                )
            )
        flash.success(
            self.request,
            f"{_naira(moved.amount)} moved from {payment.student.full_name} to "
            f"{moved.student.full_name}. The first receipt is voided.",
        )
        return HttpResponseRedirect(moved.get_absolute_url())


class SharedReceiptView(CapabilityRequiredMixin, View):
    """One printable sheet for the sum the parent handed over."""

    capability = Capability.VIEW_PAYMENTS

    def get(self, request, pk):
        txn = _transaction(pk)
        shares = [s for s in _visible_shares(txn) if s.status != PaymentStatus.VOID]
        return render(request, "payments/shared_receipt.html", {
            "txn": txn,
            "school": txn.branch.school,
            "received_from": txn.payer_name or txn.family.contact_name or txn.family.name,
            "lines": [(share, balances.running_balance(share)) for share in shares],
            "elsewhere": _shared_elsewhere(txn, shares),
            "page_title": f"Receipt {txn.number}",
        })


class SharedReceiptFileView(CapabilityRequiredMixin, View):
    """The attached slip, behind the same gate as the payment."""

    capability = Capability.VIEW_PAYMENTS

    def get(self, request, pk):
        txn = _transaction(pk)
        if not txn.receipt:
            raise Http404("No slip is attached to that payment.")
        # A parent's bank slip is personal financial data.
        return stream_stored_file(
            txn.receipt, inline_types=DOCUMENT_TYPES, cache_control="private, no-store"
        )
