"""Payment screens: record, the pending queue, history, outstanding, receipt.

No view here filters by school or branch -- ``Payment.objects`` is tenant-scoped
like every other feature model, so a list is already narrowed and another
branch's payment 404s.

Nothing on these screens stores a balance. Every figure is derived through
:mod:`apps.payments.balances`, which is why confirming a pending receipt or
voiding a payment moves the student's balance, the outstanding list, the bursar's
dashboard and messaging's "parents who owe" in the same instant, with no
recalculation step anywhere.
"""

from __future__ import annotations

from decimal import Decimal

from django.apps import apps as django_apps
from django.contrib import messages as flash
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.http import Http404, HttpResponseRedirect
from django.urls import reverse
from django.views.generic import (
    CreateView,
    DetailView,
    FormView,
    ListView,
    TemplateView,
    UpdateView,
    View,
)

from apps.core.files import DOCUMENT_TYPES, stream_stored_file
from apps.core.permissions import Capability, CapabilityRequiredMixin, has_capability
from apps.fees.models import Term
from apps.students import fees
from apps.students.models import Student

from . import balances, duplicates
from .forms import (
    ConfirmedPaymentNotesForm,
    PaymentFilterForm,
    PaymentForm,
    PendingConfirmForm,
    VoidForm,
    student_from_picker,
)
from .models import Payment, PaymentStatus

ZERO = Decimal("0")


class ViewPaymentsMixin(CapabilityRequiredMixin):
    capability = Capability.VIEW_PAYMENTS


class RecordPaymentsMixin(CapabilityRequiredMixin):
    """The bursar's core job, and the leadership roles above them."""

    capability = Capability.RECORD_PAYMENTS


class VoidPaymentsMixin(CapabilityRequiredMixin):
    """Reversing confirmed money is supervisory -- principal and owner only."""

    capability = Capability.VOID_PAYMENTS


class TermMixin:
    """The term these screens are about: the branch's current one."""

    def current_term(self) -> Term | None:
        if not hasattr(self, "_term"):
            self._term = Term.objects.filter(is_current=True).first()
        return self._term


# ===========================================================================
# Branch-wide list
# ===========================================================================


class PaymentListView(ViewPaymentsMixin, TermMixin, ListView):
    """Every payment this branch has taken, filterable by label, status and date."""

    model = Payment
    template_name = "payments/payment_list.html"
    context_object_name = "payments"
    paginate_by = 25

    def get_filter_form(self) -> PaymentFilterForm:
        if not hasattr(self, "_filter_form"):
            self._filter_form = PaymentFilterForm(data=self.request.GET or None)
            self._filter_form.is_valid()
        return self._filter_form

    def get_queryset(self):
        queryset = super().get_queryset().select_related(
            "student", "student__school_class", "term", "recorded_by"
        )
        form = self.get_filter_form()
        filters = form.cleaned_data if form.is_bound and form.is_valid() else {}

        term = filters.get("q")
        if term:
            queryset = queryset.filter(
                Q(student__first_name__icontains=term)
                | Q(student__last_name__icontains=term)
                | Q(student__admission_number__icontains=term)
                | Q(reference__icontains=term)
            )
        for field in ("status", "label", "method"):
            if filters.get(field):
                queryset = queryset.filter(**{field: filters[field]})
        if filters.get("since"):
            queryset = queryset.filter(date_paid__gte=filters["since"])
        if filters.get("until"):
            queryset = queryset.filter(date_paid__lte=filters["until"])
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["filter_form"] = self.get_filter_form()
        context["is_filtered"] = any(
            self.request.GET.get(name)
            for name in ("q", "status", "label", "method", "since", "until")
        )
        # Totals for what the filters selected, not just the page on screen --
        # "how much did we take in October?" is the question this bar answers.
        totals = self.get_queryset().aggregate(
            confirmed=Sum("amount", filter=Q(status=PaymentStatus.CONFIRMED)),
            pending=Sum("amount", filter=Q(status=PaymentStatus.PENDING)),
            pending_count=Count("id", filter=Q(status=PaymentStatus.PENDING)),
        )
        context["confirmed_total"] = totals["confirmed"] or ZERO
        context["pending_total"] = totals["pending"] or ZERO
        context["pending_count"] = totals["pending_count"] or 0
        context["term"] = self.current_term()
        context["page_title"] = "Payments"
        return context


class PendingQueueView(ViewPaymentsMixin, ListView):
    """Receipts handed in and waiting to be checked.

    The client's flow in one screen: a payment arrives with an attachment and
    a best guess at who it belongs to, and the bursar opens it, corrects the
    student, label and amount against the slip, and confirms.
    """

    model = Payment
    template_name = "payments/pending_queue.html"
    context_object_name = "payments"

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .filter(status=PaymentStatus.PENDING)
            .select_related("student", "student__school_class", "term")
            # Oldest first: a queue is worked from the front.
            .order_by("date_paid", "id")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        rows = list(context["payments"])
        context["pending_total"] = sum((p.amount for p in rows), ZERO)
        context["can_record"] = "record_payments" in _capabilities(self.request)
        context["page_title"] = "Pending receipts"
        return context


# ===========================================================================
# Recording and confirming
# ===========================================================================


class PaymentCreateView(RecordPaymentsMixin, TermMixin, CreateView):
    """Take a payment at the bursary window."""

    model = Payment
    form_class = PaymentForm
    template_name = "payments/payment_form.html"

    def get_initial(self):
        initial = super().get_initial()
        # Arrive from a student's page and they are already filled in.
        requested = self.request.GET.get("student")
        if requested:
            from apps.students.models import Student

            student = Student.objects.filter(pk=requested).first()
            if student is not None:
                initial["student"] = student
        return initial

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "Record a payment"
        context["verb"] = "Record payment"
        context["term"] = self.current_term()
        context["position"] = _position_for_form(context["form"])
        return context

    def form_valid(self, form):
        cleaned = form.cleaned_data
        with transaction.atomic():
            duplicates.lock_row(Student, cleaned["student"].pk)
            twin = duplicates.recent_twin(
                Payment.all_objects.filter(
                    student=cleaned["student"],
                    amount=cleaned["amount"],
                    date_paid=cleaned["date_paid"],
                    label=cleaned["label"],
                    method=cleaned["method"],
                    reference=cleaned["reference"],
                ),
                recorded_by=self.request.user,
            )
            if twin is None:
                self.object = form.save(user=self.request.user)
        if twin is not None:
            self.object = twin
            flash.info(
                self.request,
                f"{_naira(twin.amount)} for {twin.student.full_name} was already "
                f"recorded a moment ago, so it was not added again.",
            )
        else:
            _announce(self.request, self.object, recorded=True)
        return HttpResponseRedirect(self.get_success_url())

    def get_success_url(self):
        if self.object.status == PaymentStatus.CONFIRMED:
            # Straight to the receipt: the parent is still standing there.
            return reverse("payments:receipt", args=[self.object.pk])
        return reverse("payments:pending")


class StudentPositionView(RecordPaymentsMixin, TemplateView):
    """The "Where they stand" panel for whoever the picker now names.

    Fetched by the record form as the bursar picks a student, and swapped into
    the panel in place -- so the balance is on screen before the amount is
    typed, not after the form is submitted. The fragment is the same partial
    the form renders on load, so the two can never disagree.

    Tenant-scoped through the picker's own lookup: an admission number from
    another school names nobody here, and the panel falls back to its prompt.
    """

    template_name = "payments/_position_panel.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        student = student_from_picker(self.request.GET.get("student"))
        context["position"] = fees.position_for(student) if student else None
        return context


class PaymentUpdateView(RecordPaymentsMixin, TermMixin, UpdateView):
    """Correct a payment, and confirm it if it was pending.

    Reached both from the pending queue and from a payment's own page. A
    voided payment is not editable -- it is a closed record, and the way to
    change it is to record the correct payment afresh.

    Once confirmed, changing the money reverses it as surely as voiding does, so
    it takes the same capability. Without it, only the description can change.
    """

    model = Payment
    template_name = "payments/payment_form.html"

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .exclude(status=PaymentStatus.VOID)
            .select_related("student", "term")
        )

    def _money_is_locked(self) -> bool:
        return self.object.status == PaymentStatus.CONFIRMED and not has_capability(
            self.request.user, Capability.VOID_PAYMENTS
        )

    def get_form_class(self):
        if self.object.status == PaymentStatus.PENDING:
            return PendingConfirmForm
        if self._money_is_locked():
            return ConfirmedPaymentNotesForm
        return PaymentForm

    def get_template_names(self):
        if self._money_is_locked():
            return ["payments/payment_notes_form.html"]
        return [self.template_name]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        pending = self.object.status == PaymentStatus.PENDING
        context["page_title"] = (
            "Check this receipt" if pending else "Edit payment"
        )
        context["verb"] = "Confirm payment" if pending else "Save changes"
        context["reviewing"] = pending
        context["term"] = self.object.term
        context["position"] = _position_for_form(context["form"])
        return context

    def form_valid(self, form):
        was_pending = self.object.status == PaymentStatus.PENDING
        self.object = form.save(user=self.request.user)
        _announce(self.request, self.object, recorded=False, confirmed=was_pending)
        return HttpResponseRedirect(self.get_success_url())

    def get_success_url(self):
        if self.object.status == PaymentStatus.CONFIRMED:
            return reverse("payments:receipt", args=[self.object.pk])
        return reverse("payments:pending")


def intake_portion(payment) -> Decimal:
    """Admission money paid in the same lump as ``payment``, or zero.

    An enrolment payment is divided between this ledger and the admissions one
    (see apps/admissions/split.py). The receipt and the detail screen show the
    whole sum the parent handed over, so they ask for the other half here.
    Resolved through the app registry: payments does not depend on admissions.
    """
    if not django_apps.is_installed("apps.admissions"):
        return Decimal("0")
    from apps.admissions.split import intake_total_for

    return intake_total_for(payment)


class PaymentDetailView(ViewPaymentsMixin, DetailView):
    """One payment, its attachment, and the balance it moved."""

    model = Payment
    template_name = "payments/payment_detail.html"
    context_object_name = "payment"

    def get_queryset(self):
        return super().get_queryset().select_related(
            "student", "student__school_class", "term", "recorded_by",
            "confirmed_by", "voided_by",
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        payment = self.object
        context["position"] = fees.position_for(payment.student)
        context["can_void"] = (
            "void_payments" in _capabilities(self.request)
            and payment.status == PaymentStatus.CONFIRMED
        )
        context["can_record"] = "record_payments" in _capabilities(self.request)
        context["intake_portion"] = intake_portion(payment)
        context["received_total"] = payment.amount + context["intake_portion"]
        context["page_title"] = f"Payment {payment.receipt_number}"
        return context


class PaymentVoidView(VoidPaymentsMixin, FormView):
    """Reverse a confirmed payment. Never deletes it."""

    form_class = VoidForm
    template_name = "payments/payment_confirm_void.html"

    def get_payment(self) -> Payment:
        """Resolved lazily, so the capability check runs first: a bursar gets
        the 403 they are owed rather than a 404 that hides why."""
        from django.shortcuts import get_object_or_404

        if not hasattr(self, "_payment"):
            self._payment = get_object_or_404(
                Payment.objects.select_related("student", "term"),
                pk=self.kwargs["pk"],
            )
        return self._payment

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        payment = self.get_payment()
        position = fees.position_for(payment.student)
        context["payment"] = payment
        context["position"] = position
        # Worked out here rather than with the `add` template filter, which
        # coerces through int() and would quietly drop the kobo.
        context["outstanding_after"] = (
            position.outstanding + payment.amount
            if payment.status == PaymentStatus.CONFIRMED
            else position.outstanding
        )
        context["already_void"] = payment.status == PaymentStatus.VOID
        context["page_title"] = f"Void {payment.receipt_number}"
        return context

    def form_valid(self, form):
        payment = self.get_payment()
        if payment.status == PaymentStatus.VOID:
            flash.info(self.request, "That payment was already voided.")
            return HttpResponseRedirect(payment.get_absolute_url())

        payment.void(by=self.request.user, reason=form.cleaned_data["reason"])
        position = fees.position_for(payment.student)
        flash.warning(
            self.request,
            f"{payment.receipt_number} voided. {payment.student.full_name}'s "
            f"balance is now {_naira(position.outstanding)}.",
        )
        return HttpResponseRedirect(payment.get_absolute_url())


# ===========================================================================
# Balances and receipts
# ===========================================================================


class OutstandingView(ViewPaymentsMixin, TermMixin, ListView):
    """Who still owes, most owed first -- and one click to message them.

    Deliberately the same derivation messaging's "parents who owe" resolves to,
    so the list a bursar chases and the parents a reminder reaches can never be
    different people.
    """

    template_name = "payments/outstanding.html"
    context_object_name = "rows"
    paginate_by = 50

    def get_queryset(self):
        return balances.outstanding()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        rows = context["rows"]
        context["owed_total"] = sum((row.owed for row in rows), ZERO)
        context["expected_total"] = sum(
            (row.position.expected for row in rows), ZERO
        )
        context["term"] = self.current_term()
        context["can_send"] = "send_messages" in _capabilities(self.request)
        context["can_record"] = "record_payments" in _capabilities(self.request)
        context["page_title"] = "Outstanding balances"
        return context


class ReceiptView(ViewPaymentsMixin, DetailView):
    """A printable receipt for a confirmed payment.

    Plain HTML with a print stylesheet rather than a generated PDF: the browser
    already knows how to print and how to save as PDF, and adding a PDF library
    to the pilot would be a dependency the school has to keep working for a
    page that is one page long.
    """

    model = Payment
    template_name = "payments/receipt.html"
    context_object_name = "payment"

    def get_queryset(self):
        return super().get_queryset().select_related(
            "student", "student__school_class", "student__branch", "term",
            "recorded_by", "confirmed_by",
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        payment = self.object
        position = fees.position_for(payment.student)
        context["position"] = position
        # What was still owed the moment this payment was taken -- not what is
        # owed now, which may have moved since the parent walked away.
        context["running_balance"] = balances.running_balance(payment)
        context["school"] = payment.student.branch.school
        context["intake_portion"] = intake_portion(payment)
        context["received_total"] = payment.amount + context["intake_portion"]
        context["page_title"] = f"Receipt {payment.receipt_number}"
        return context


class ReceiptFileView(ViewPaymentsMixin, View):
    """The attached slip itself, behind the same gate as the payment.

    The storage URL would open for anyone holding it; this one only opens for a
    signed-in account that can see payments, and only for a payment in its own
    school -- ``Payment.objects`` is tenant-scoped, so another school's id 404s.
    Streamed from whichever storage backend holds it, local disk or bucket.
    """

    def get(self, request, pk):
        payment = Payment.objects.filter(pk=pk).first()
        if payment is None or not payment.receipt:
            raise Http404("No receipt is attached to that payment.")
        # A parent's bank slip is personal financial data.
        return stream_stored_file(
            payment.receipt,
            inline_types=DOCUMENT_TYPES,
            cache_control="private, no-store",
        )


class StudentPaymentHistoryView(ViewPaymentsMixin, DetailView):
    """A student's full payment history, when the detail page's card is not enough."""

    template_name = "payments/student_history.html"
    context_object_name = "student"

    def get_queryset(self):
        from apps.students.models import Student

        return Student.objects.select_related("school_class", "branch")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        student = self.object
        context["payments"] = list(
            Payment.objects.filter(student=student).select_related(
                "term", "recorded_by"
            )
        )
        context["position"] = fees.position_for(student)
        context["can_record"] = "record_payments" in _capabilities(self.request)
        context["page_title"] = f"Payments — {student.full_name}"
        return context


# ===========================================================================
# Helpers
# ===========================================================================


def _capabilities(request) -> set[str]:
    from apps.core.permissions import capabilities_of

    return {c.value for c in capabilities_of(request.user)}


def _naira(amount: Decimal) -> str:
    from apps.core.templatetags.money import naira

    return naira(amount)


def _position_for_form(form):
    """The fee position of whoever the form is currently pointed at.

    Shown next to the amount field so the bursar can see what is owed while
    typing what was paid -- the number they are most likely to be checking
    against.
    """
    # Unbound, this is the initial Student; bound, it is whatever was typed --
    # the picker's text, which only the picker's own lookup can resolve. A form
    # with the money locked has no picker at all, only the payment's own student.
    student = (
        student_from_picker(form["student"].value())
        if "student" in form.fields
        else None
    )
    if student is not None:
        return fees.position_for(student)
    if getattr(form, "instance", None) is not None and form.instance.student_id:
        return fees.position_for(form.instance.student)
    return None


def _announce(request, payment: Payment, *, recorded: bool, confirmed: bool = False):
    """Say what happened, and what it did to the balance.

    The balance is the reason the bursar is here, so it goes in the message
    rather than being something they have to navigate to and check.
    """
    position = fees.position_for(payment.student)
    name = payment.student.full_name

    if payment.status == PaymentStatus.PENDING:
        flash.success(
            request,
            f"{_naira(payment.amount)} held as pending for {name}. It does not "
            f"count toward their balance until it is confirmed.",
        )
        return

    verb = "recorded" if recorded else ("confirmed" if confirmed else "updated")
    balance_note = (
        f"{name} now owes {_naira(position.outstanding)}."
        if position.is_priced
        else f"{name}'s class has no fees set this term, so there is no balance "
             f"to update."
    )
    flash.success(
        request,
        f"{_naira(payment.amount)} {verb} for {name}. {balance_note}",
    )
