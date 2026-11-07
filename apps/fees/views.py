"""Fee structure management screens.

As with academics, no view filters by school or branch: the tenant-scoped
managers do it, so a list is already narrowed and a cross-branch lookup 404s.
"""

from __future__ import annotations

from collections import OrderedDict
from decimal import Decimal

from django.contrib import messages
from django.db import transaction
from django.db.models import Count
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import reverse, reverse_lazy
from django.views.generic import CreateView, DeleteView, FormView, ListView, UpdateView

from apps.academics.lookup import ClassIndex
from apps.academics.models import Class, Level
from apps.core.import_flow import (
    ImportFlow,
    ImportReviewView,
    ImportTemplateView,
    ImportUploadView,
)
from apps.core.permissions import Capability, CapabilityRequiredMixin
from apps.core.spreadsheets import Reference
from apps.students.models import Student

from . import importers
from .forms import (
    FeeComponentFormSet,
    FeeStructureForm,
    NewFeeComponentFormSet,
    StudentFeeAdjustmentForm,
    TermForm,
    VoidAdjustmentForm,
)
from .models import FeeStructure, StudentFeeAdjustment, Term, not_past


class ReadFeesMixin(CapabilityRequiredMixin):
    capability = Capability.VIEW_FEES


class ManageFeesMixin(CapabilityRequiredMixin):
    """Owner- and principal-level only. A bursar reaches these and gets a 403."""

    capability = Capability.MANAGE_FEES


class TermContextMixin:
    """Resolves which term the screen is about, from ``?term=`` or the current one."""

    _term_cache = False

    def get_terms(self):
        return Term.objects.select_related("branch")

    def get_selected_term(self):
        if self._term_cache is not False:
            return self._term_cache
        terms = self.get_terms()
        requested = self.request.GET.get("term")
        term = None
        if requested:
            term = terms.filter(pk=requested).first()
        if term is None:
            term = terms.filter(is_current=True).first() or terms.first()
        self._term_cache = term
        return term


class FeeStructureListView(ReadFeesMixin, TermContextMixin, ListView):
    model = FeeStructure
    template_name = "fees/structure_list.html"
    context_object_name = "structures"

    def get_queryset(self):
        term = self.get_selected_term()
        if term is None:
            return FeeStructure.objects.none()
        return (
            FeeStructure.objects.filter(term=term)
            .select_related("school_class", "term", "branch")
            .prefetch_related("components")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        term = self.get_selected_term()
        structures = list(context["structures"])

        groups = OrderedDict()
        for structure in structures:
            groups.setdefault(structure.school_class.level, []).append(structure)
        context["level_groups"] = [
            {
                "label": Level(level).label,
                "structures": items,
                "subtotal": sum((s.total for s in items), Decimal("0")),
            }
            for level, items in groups.items()
        ]

        context["terms"] = list(self.get_terms())
        context["selected_term"] = term
        context["grand_total"] = sum((s.total for s in structures), Decimal("0"))

        # Which classes at this branch still have no fees set for the term --
        # the question a school owner actually has during onboarding.
        if term is not None:
            context["classes_without_fees"] = list(
                Class.objects.filter(is_active=True, branch=term.branch)
                .exclude(pk__in=[s.school_class_id for s in structures])
            )
        else:
            context["classes_without_fees"] = []

        context["page_title"] = "Fee structures"
        return context


class ComponentFormsetMixin:
    """Drives the fee structure form and its line-item formset together.

    Both must validate before either is written, so a bad line item cannot
    leave a structure behind with no components.
    """

    updating = False
    formset_class = FeeComponentFormSet

    def get_formset(self, instance, data=None):
        return self.formset_class(data=data, instance=instance)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.setdefault("formset", self.get_formset(self.object))
        return context

    def post(self, request, *args, **kwargs):
        self.object = self.get_object() if self.updating else None
        form = self.get_form()
        formset = self.get_formset(self.object or FeeStructure(), data=request.POST)

        if form.is_valid() and formset.is_valid():
            with transaction.atomic():
                self.object = form.save()
                formset.instance = self.object
                formset.save()
                self._apply_positions(formset)
            messages.success(
                request,
                f"Fees for {self.object.school_class.display_name} "
                f"({self.object.term.name}) saved — total "
                f"₦{self.object.total:,.0f}.",
            )
            return HttpResponseRedirect(self.get_success_url())

        return self.render_to_response(
            self.get_context_data(form=form, formset=formset)
        )

    @staticmethod
    def _apply_positions(formset) -> None:
        """Persist the on-screen order of the line items."""
        position = 0
        for form in formset.forms:
            if not form.cleaned_data or form.cleaned_data.get("DELETE"):
                continue
            component = form.instance
            if component.pk and component.position != position:
                component.position = position
                component.save(update_fields=["position"])
            position += 1

    def get_success_url(self):
        return f"{reverse('fees:structure_list')}?term={self.object.term_id}"


class FeeStructureCreateView(
    ManageFeesMixin, ComponentFormsetMixin, TermContextMixin, CreateView
):
    model = FeeStructure
    form_class = FeeStructureForm
    template_name = "fees/structure_form.html"
    updating = False
    formset_class = NewFeeComponentFormSet
    extra_context = {"page_title": "New fee structure", "verb": "Create"}

    def get_initial(self):
        initial = super().get_initial()
        # Arrive from "Set fees" on the list and the class is already chosen.
        requested_class = self.request.GET.get("class")
        if requested_class:
            klass = Class.objects.filter(pk=requested_class).first()
            if klass:
                initial["school_class"] = klass
        term = self.get_selected_term()
        if term:
            initial["term"] = term
        return initial


class RefusePastTermMixin:
    """Keep an earlier term's fees as they were billed.

    Children's balances for that term are read from these figures, so changing them
    would quietly change what families owe. A correction for one child is an
    adjustment. Listed after the capability mixin, whose check runs first, so a bursar
    still gets their 403.
    """

    def get(self, request, *args, **kwargs):
        return self._refusal() or super().get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        return self._refusal() or super().post(request, *args, **kwargs)

    def _refusal(self):
        structure = self.get_object()
        if not structure.term.is_past:
            return None
        messages.error(
            self.request,
            f"{structure.term.name} has ended, so its fees can't be changed. To correct "
            f"one child's bill, add an adjustment on their page.",
        )
        return HttpResponseRedirect(
            f"{reverse('fees:structure_list')}?term={structure.term_id}"
        )


class FeeStructureUpdateView(
    ManageFeesMixin, RefusePastTermMixin, ComponentFormsetMixin, UpdateView
):
    model = FeeStructure
    form_class = FeeStructureForm
    template_name = "fees/structure_form.html"
    updating = True
    extra_context = {"verb": "Save"}

    def get_queryset(self):
        return super().get_queryset().select_related("school_class", "term")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"Fees — {self.object.school_class.display_name}"
        return context


class FeeStructureDeleteView(ManageFeesMixin, RefusePastTermMixin, DeleteView):
    model = FeeStructure
    template_name = "fees/structure_confirm_delete.html"
    success_url = reverse_lazy("fees:structure_list")
    extra_context = {"page_title": "Delete fee structure"}

    def form_valid(self, form):
        label = f"{self.object.school_class.display_name} ({self.object.term.name})"
        messages.success(self.request, f"Fee structure for {label} deleted.")
        return super().form_valid(form)


# ---------------------------------------------------------------------------
# Terms
#
# "Terms" in the sidebar used to be a Django admin link, which the proprietor's
# own account cannot open (not ``is_staff``) and which is not tenant-scoped.
# A term is ordinary school setup, so it gets an ordinary screen, gated on the
# same fee capabilities as the structures priced against it.
# ---------------------------------------------------------------------------


class TermListView(ReadFeesMixin, ListView):
    model = Term
    template_name = "fees/term_list.html"
    context_object_name = "terms"

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related("branch")
            .annotate(structure_count=Count("fee_structures", distinct=True))
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "Terms"
        return context


class TermCreateView(ManageFeesMixin, CreateView):
    model = Term
    form_class = TermForm
    template_name = "fees/term_form.html"
    success_url = reverse_lazy("fees:term_list")
    extra_context = {"page_title": "New term", "verb": "Create"}

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, f"{self.object.name} created.")
        return response

    def get_success_url(self):
        """Back to a blank form when asked, rather than to the list.

        Terms are set up three at a time, once a year. The record written is
        identical either way -- this only decides where you land.
        """
        if "save_and_add_another" in self.request.POST:
            return reverse("fees:term_create")
        return super().get_success_url()


class TermUpdateView(ManageFeesMixin, UpdateView):
    model = Term
    form_class = TermForm
    template_name = "fees/term_form.html"
    success_url = reverse_lazy("fees:term_list")
    extra_context = {"page_title": "Edit term", "verb": "Save"}

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, f"{self.object.name} updated.")
        return response



# ---------------------------------------------------------------------------
# One child's bill
# ---------------------------------------------------------------------------


class AdjustmentCreateView(ManageFeesMixin, CreateView):
    """A discount, scholarship, charge or opening balance for one child."""

    model = StudentFeeAdjustment
    form_class = StudentFeeAdjustmentForm
    template_name = "fees/adjustment_form.html"

    def get_student(self):
        if not hasattr(self, "_student"):
            # Scoped manager: another school's child is a 404.
            self._student = get_object_or_404(Student.objects, pk=self.kwargs["student"])
        return self._student

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["student"] = self.get_student()
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["student"] = self.get_student()
        context["page_title"] = f"Adjustment — {self.get_student().full_name}"
        return context

    def form_valid(self, form):
        form.instance.created_by = self.request.user
        response = super().form_valid(form)
        messages.success(
            self.request,
            f"{self.object.get_kind_display()} recorded for "
            f"{self.get_student().full_name}.",
        )
        return response

    def get_success_url(self):
        return self.get_student().get_absolute_url()


class AdjustmentVoidView(ManageFeesMixin, FormView):
    """Take an adjustment back. It stays on record, with who did it and why."""

    form_class = VoidAdjustmentForm
    template_name = "fees/adjustment_void.html"

    def get_adjustment(self):
        if not hasattr(self, "_adjustment"):
            self._adjustment = get_object_or_404(
                StudentFeeAdjustment.objects.select_related("student", "term"),
                pk=self.kwargs["pk"],
            )
        return self._adjustment

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["adjustment"] = self.get_adjustment()
        context["page_title"] = "Void adjustment"
        return context

    def form_valid(self, form):
        adjustment = self.get_adjustment()
        if adjustment.is_void:
            messages.info(self.request, "That adjustment was already voided.")
        else:
            adjustment.void(by=self.request.user, reason=form.cleaned_data["reason"])
            messages.warning(self.request, "Adjustment voided.")
        return HttpResponseRedirect(adjustment.student.get_absolute_url())


# ---------------------------------------------------------------------------
# Excel import
#
# Pricing nineteen classes by hand is nineteen trips through the structure form,
# each with its own line-item formset. A school that already keeps its fee
# schedule in a spreadsheet -- which is all of them -- should be able to hand it
# over instead.
#
# The term is chosen on the upload screen rather than per row. Fees are priced
# per class per term, and a file that could name three terms would also be a
# file that could price next year's fees by accident.
# ---------------------------------------------------------------------------


class FeeImportFlow(ImportFlow):
    sheet = importers.SHEET
    capability = Capability.MANAGE_FEES
    session_key = "fees.import"

    url_template = "fees:structure_import_template"
    url_upload = "fees:structure_import"
    url_review = "fees:structure_import_review"
    url_done = "fees:structure_list"

    upload_template_name = "fees/structure_import.html"
    review_template_name = "fees/structure_import_review.html"

    heading = "Import fee structures"
    intro = (
        "Your fee schedule from a spreadsheet — one row per charge. Nothing is "
        "saved until you have seen what the file contains."
    )
    scope_label = "Term"
    scope_help = "Every fee in the file is priced for this term."
    scope_field_name = "term"
    no_scope_title = "No term to price"
    no_scope_body = (
        "Fees belong to a term. Set the term up first and the import will have "
        "somewhere to put them."
    )
    back_label = "fees"

    def scope_queryset(self):
        return not_past(Term.objects.select_related("branch"))

    def requested_scope(self, request):
        """The term a download is for, defaulting to the one the school is in.

        ``Term.objects`` orders newest year first, so without this a school
        mid-session would be handed last year's third term -- the shared
        implementation takes the first row, and for terms that is not the
        obvious one.
        """
        scope = super().requested_scope(request)
        if request.GET.get(self.scope_field_name):
            return scope
        return self.scope_queryset().filter(is_current=True).first() or scope

    def template_links(self, request) -> list[dict]:
        """One download per campus, not per term.

        The shared implementation offers one link per scope, which is right when
        the scope is a campus and wrong here: the only thing a term changes
        about the template is which campus's classes are listed on the reference
        tab, and a school in its third year would have been handed nine
        identical buttons.
        """
        base = reverse(self.url_template)
        terms = list(self.scope_queryset())
        # The current term for each campus, falling back to its newest.
        per_branch = {}
        for term in terms:
            if term.branch_id not in per_branch or term.is_current:
                per_branch[term.branch_id] = term
        chosen = list(per_branch.values())
        if len(chosen) <= 1:
            return [{"label": "Download template (.xlsx)", "href": base}]
        return [
            {
                "label": f"Download template — {term.branch.name}",
                "href": f"{base}?{self.scope_field_name}={term.pk}",
            }
            for term in sorted(chosen, key=lambda t: t.branch.name)
        ]

    def done_url(self, scope=None) -> str:
        # The fee list is driven by ?term=, so land on the term just imported
        # into rather than on whichever the selector would have defaulted to.
        base = reverse(self.url_done)
        return f"{base}?term={scope.pk}" if scope is not None else base

    def references(self, scope):
        """The term's own campus's classes, as a drop-down on the Class column.

        Unlike the subject sheet, one cell names one class here, so the
        drop-down is the right control and it is the cheapest place to stop the
        error this import would otherwise produce most.
        """
        names = ClassIndex(scope.branch).names
        if not names:
            return ()
        return (
            Reference(
                name="Classes",
                title=(
                    f"Classes at {scope.branch.name} — use one of these in the "
                    f"Class column"
                ),
                values=tuple(names),
                column="school_class",
                error=(
                    "That class does not exist at this campus. Pick one from the "
                    "list, or set the class up in SCHOOLCORD first."
                ),
                error_title="Unknown class",
            ),
        )

    def validate(self, rows, scope, **parsed):
        return importers.validate(rows, term=scope, **parsed)

    def commit(self, report, scope):
        return importers.commit(report, scope)

    def success_message(self, created, report, scope):
        total = sum((c.amount for c in created), Decimal("0"))
        classes = len({c.fee_structure_id for c in created})
        skipped = report.failed_count
        note = (
            f" {skipped} row{'' if skipped == 1 else 's'} "
            f"{'was' if skipped == 1 else 'were'} skipped and still "
            f"{'needs' if skipped == 1 else 'need'} correcting."
            if skipped else ""
        )
        return (
            f"{len(created)} fee{'' if len(created) == 1 else 's'} imported — "
            f"{classes} class{'' if classes == 1 else 'es'} priced for "
            f"{scope.name}, ₦{total:,.0f} in total.{note}"
        )

    def upload_context(self, request):
        return {"class_count": Class.objects.filter(is_active=True).count()}

    def review_context(self, report, scope):
        # Grouped by class with a subtotal, because "what will Primary 1 cost?"
        # is the question somebody checks this screen to answer, and a flat list
        # of line items in sheet order does not answer it.
        groups = importers.grouped_ready(report)
        return {
            "class_groups": groups,
            "grand_total": sum((g["total"] for g in groups), Decimal("0")),
        }


class FeeImportTemplateView(ImportTemplateView):
    flow_class = FeeImportFlow


class FeeImportView(ImportUploadView):
    flow_class = FeeImportFlow


class FeeImportReviewView(ImportReviewView):
    flow_class = FeeImportFlow
