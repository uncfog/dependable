"""Academic setup screens.

No view here filters by school or branch. It does not need to: ``Class.objects``
and ``Subject.objects`` are tenant-scoped managers, so list querysets are
already narrowed and ``get_object()`` returns 404 for another branch's row
rather than leaking it.
"""

from __future__ import annotations

import json
from collections import OrderedDict

from django.contrib import messages
from django.db import transaction
from django.db.models import Count, Prefetch, ProtectedError
from django.http import HttpResponseRedirect
from django.urls import reverse, reverse_lazy
from django.views.generic import CreateView, DeleteView, ListView, TemplateView, UpdateView

from apps.core.import_flow import (
    ImportFlow,
    ImportReviewView,
    ImportTemplateView,
    ImportUploadView,
)
from apps.core.permissions import Capability, CapabilityRequiredMixin
from apps.core.spreadsheets import Choice, Reference
from apps.schools.models import Branch

from . import importers
from .curriculum import ladder_presets, subject_presets
from .lookup import ClassIndex, level_names
from .forms import (
    BranchChoiceForm,
    ClassEditFormSet,
    ClassForm,
    ClassRowFormSet,
    SubjectBatchClassesForm,
    SubjectForm,
    SubjectRowFormSet,
)
from .models import Class, Level, Subject


class ReadAcademicsMixin(CapabilityRequiredMixin):
    capability = Capability.VIEW_ACADEMICS


class ManageAcademicsMixin(CapabilityRequiredMixin):
    """Owner- and principal-level only. A bursar reaches these and gets a 403."""

    capability = Capability.MANAGE_ACADEMICS


class FlashOnSuccessMixin:
    success_message = ""

    def form_valid(self, form):
        response = super().form_valid(form)
        if self.success_message:
            messages.success(self.request, self.success_message % {"object": self.object})
        return response


class SaveAndAddAnotherMixin:
    """Lets a create form come back to itself instead of the list.

    Setup is the one time somebody adds several of these in a row, and landing
    on the list after each one means finding the New button again. The button
    posts ``save_and_add_another``; everything else about the save is unchanged,
    so the record written is identical either way.

    The bulk screens are the better answer for a whole ladder of classes -- this
    is for the third subject or the second term, where a table would be more
    ceremony than the job needs.
    """

    #: Where "save and add another" goes back to. Defaults to this same view.
    add_another_url_name: str | None = None

    def get_success_url(self):
        if "save_and_add_another" in self.request.POST:
            return reverse(self.add_another_url_name or self.request.resolver_match.view_name)
        return super().get_success_url()


# ---------------------------------------------------------------------------
# Classes
# ---------------------------------------------------------------------------

class ClassListView(ReadAcademicsMixin, ListView):
    model = Class
    template_name = "academics/class_list.html"
    context_object_name = "classes"

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related("branch")
            .annotate(subject_count=Count("subjects", distinct=True))
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Grouped by band, in admission order, which is how a head of school
        # reads a class list.
        groups = OrderedDict()
        for klass in context["classes"]:
            groups.setdefault(klass.level, []).append(klass)
        context["level_groups"] = [
            {"label": Level(level).label, "classes": items}
            for level, items in groups.items()
        ]
        context["page_title"] = "Classes"
        return context


class ClassCreateView(
    ManageAcademicsMixin, SaveAndAddAnotherMixin, FlashOnSuccessMixin, CreateView
):
    model = Class
    form_class = ClassForm
    template_name = "academics/class_form.html"
    success_url = reverse_lazy("academics:class_list")
    success_message = "Class %(object)s created."
    extra_context = {"page_title": "New class", "verb": "Create"}


class BulkRowsMixin(ManageAcademicsMixin):
    """The shape shared by the two bulk-entry screens.

    One campus chosen at the top, a table of rows under it, everything written
    in a single transaction. The subclass supplies the formset, the label and
    where to go afterwards; the back-and-forth removal is all here.

    The transaction matters for a reason beyond tidiness: nineteen classes half
    created, with no indication of which nine, is a worse state to hand back
    than nothing at all.
    """

    formset_class = None
    template_name = ""
    noun = "record"
    noun_plural = "records"
    list_url_name = ""

    def build_formset(self, data=None, branch=None):
        return self.formset_class(
            data=data,
            queryset=self.formset_class.model._default_manager.none(),
            branch=branch,
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.setdefault("branch_form", BranchChoiceForm())
        context.setdefault("formset", self.build_formset())
        context["list_url"] = reverse(self.list_url_name)
        return context

    def save_rows(self, formset, branch):
        """Persist the filled rows. Returns what was created."""
        created = []
        for form in formset.filled_forms():
            record = form.save(commit=False)
            record.branch = branch
            record.is_active = True
            record.save()
            created.append(record)
        return created

    def post(self, request, *args, **kwargs):
        branch_form = BranchChoiceForm(request.POST)
        branch = (
            branch_form.cleaned_data["branch"] if branch_form.is_valid() else None
        )
        formset = self.build_formset(data=request.POST, branch=branch)

        if branch_form.is_valid() and formset.is_valid():
            with transaction.atomic():
                created = self.save_rows(formset, branch)
            count = len(created)
            messages.success(
                request,
                f"{count} {self.noun if count == 1 else self.noun_plural} added "
                f"at {branch.name}.",
            )
            # "Save and add another" stays on the table with fresh blank rows,
            # which is what somebody working through a long list wants.
            if "save_and_add_another" in request.POST:
                return HttpResponseRedirect(request.path)
            return HttpResponseRedirect(reverse(self.list_url_name))

        return self.render_to_response(
            self.get_context_data(branch_form=branch_form, formset=formset)
        )


class ClassBulkCreateView(BulkRowsMixin, TemplateView):
    """Add a whole ladder of classes without leaving the page.

    The screen the client's "too back and forth" was about. A school sets up
    around nineteen classes; this is one campus choice, one press of a preset,
    and one save.
    """

    formset_class = ClassRowFormSet
    template_name = "academics/class_bulk_form.html"
    noun = "class"
    noun_plural = "classes"
    list_url_name = "academics:class_list"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Serialised here rather than in the template so the preset buttons and
        # the seeding command describe the same ladder -- see curriculum.py.
        context["presets"] = [
            {"label": group["label"], "json": json.dumps(group["rows"]),
             "count": len(group["rows"])}
            for group in ladder_presets()
        ]
        everything = [row for group in ladder_presets() for row in group["rows"]]
        context["preset_all"] = {
            "label": "The whole ladder",
            "json": json.dumps(everything),
            "count": len(everything),
        }
        context["page_title"] = "Add classes"
        return context


class ClassEditAllView(ManageAcademicsMixin, TemplateView):
    """Every class on one screen, editable in place.

    The other half of the "too back and forth" complaint. Adding a ladder is
    one problem, and the bulk-add screen answers it; correcting one is the
    other -- a school that typed "Primary 1" as "Primry 1" and wants to
    deactivate the two SSS arms it no longer runs should not visit six pages
    to do it.

    Deliberately edit-only: no blank rows at the bottom. A table whose last
    three rows behave differently from the rest is a table people mistrust,
    and adding already has a screen of its own.
    """

    template_name = "academics/class_edit_all.html"
    extra_context = {"page_title": "Edit classes"}

    def get_queryset(self):
        # Scoped manager, so this is already only what the user may see -- a
        # principal gets their campus, an owner every campus they run.
        return Class.objects.select_related("branch")

    def build_formset(self, data=None):
        return ClassEditFormSet(data=data, queryset=self.get_queryset())

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.setdefault("formset", self.build_formset())
        # An owner seeing two campuses needs to know which row is whose.
        context["show_branch"] = Branch.objects.count() > 1
        return context

    def post(self, request, *args, **kwargs):
        formset = self.build_formset(data=request.POST)
        if not formset.is_valid():
            return self.render_to_response(self.get_context_data(formset=formset))

        with transaction.atomic():
            changed = formset.save()

        if changed:
            messages.success(
                request,
                f"{len(changed)} class{'' if len(changed) == 1 else 'es'} updated.",
            )
        else:
            messages.info(request, "Nothing was changed.")
        return HttpResponseRedirect(reverse("academics:class_list"))


class ClassUpdateView(ManageAcademicsMixin, FlashOnSuccessMixin, UpdateView):
    model = Class
    form_class = ClassForm
    template_name = "academics/class_form.html"
    success_url = reverse_lazy("academics:class_list")
    success_message = "Class %(object)s updated."
    extra_context = {"page_title": "Edit class", "verb": "Save"}


class ClassDeleteView(ManageAcademicsMixin, DeleteView):
    model = Class
    template_name = "academics/class_confirm_delete.html"
    success_url = reverse_lazy("academics:class_list")
    extra_context = {"page_title": "Delete class"}

    def form_valid(self, form):
        name = str(self.object)
        try:
            response = super().form_valid(form)
        except ProtectedError:
            # Students are on it, or were billed through it. Both are history.
            messages.error(
                self.request,
                f"{name} has students or fee history, so it can't be deleted. "
                f"You can deactivate it instead, which keeps the history.",
            )
            return HttpResponseRedirect(self.success_url)
        messages.success(self.request, f"Class {name} deleted.")
        return response


# ---------------------------------------------------------------------------
# Subjects
# ---------------------------------------------------------------------------

class SubjectListView(ReadAcademicsMixin, ListView):
    model = Subject
    template_name = "academics/subject_list.html"
    context_object_name = "subjects"

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related("branch")
            .prefetch_related(
                # Class.objects is scoped too, so the chips shown against each
                # subject cannot include another branch's classes.
                Prefetch("classes", queryset=Class.objects.all())
            )
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "Subjects"
        context["unassigned_count"] = sum(
            1 for s in context["subjects"] if not s.classes.all()
        )
        return context


class SubjectFormMixin:
    """Groups the class checkboxes by level so the list stays readable."""

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        bound = context["form"]["classes"]
        groups = OrderedDict()
        for widget, klass in zip(bound.subwidgets, bound.field.queryset):
            groups.setdefault(klass.level, []).append(widget)
        context["class_groups"] = [
            {"label": Level(level).label, "widgets": widgets}
            for level, widgets in groups.items()
        ]
        return context


class SubjectBulkCreateView(BulkRowsMixin, TemplateView):
    """Add several subjects at once, optionally attaching them all to a band.

    The "Taught in" tick list is asked once for the batch rather than once per
    row, because the batches schools actually add are level-shaped: the primary
    set, the junior secondary set. A subject with an exception to that is
    edited on its own form afterwards.
    """

    formset_class = SubjectRowFormSet
    template_name = "academics/subject_bulk_form.html"
    noun = "subject"
    noun_plural = "subjects"
    list_url_name = "academics:subject_list"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.setdefault(
            "classes_form",
            getattr(self, "classes_form", None) or SubjectBatchClassesForm(),
        )
        context["presets"] = [
            {"label": group["label"], "json": json.dumps(group["rows"]),
             "count": len(group["rows"])}
            for group in subject_presets()
        ]
        context["page_title"] = "Add subjects"
        return context

    def post(self, request, *args, **kwargs):
        # Kept on the view so a failed save re-renders what was ticked rather
        # than clearing it.
        self.classes_form = SubjectBatchClassesForm(request.POST)
        return super().post(request, *args, **kwargs)

    def save_rows(self, formset, branch):
        created = super().save_rows(formset, branch)
        # Re-bound with the chosen branch, so the queryset the choice is
        # validated against is that campus's classes only -- the same guard
        # SubjectForm.clean() applies to the single form.
        classes_form = SubjectBatchClassesForm(self.request.POST, branch=branch)
        if classes_form.is_valid():
            chosen = classes_form.cleaned_data["classes"]
            if chosen:
                for subject in created:
                    subject.classes.set(chosen)
        return created


class SubjectCreateView(
    ManageAcademicsMixin, SaveAndAddAnotherMixin, SubjectFormMixin,
    FlashOnSuccessMixin, CreateView
):
    model = Subject
    form_class = SubjectForm
    template_name = "academics/subject_form.html"
    success_url = reverse_lazy("academics:subject_list")
    success_message = "Subject %(object)s created."
    extra_context = {"page_title": "New subject", "verb": "Create"}


class SubjectUpdateView(
    ManageAcademicsMixin, SubjectFormMixin, FlashOnSuccessMixin, UpdateView
):
    model = Subject
    form_class = SubjectForm
    template_name = "academics/subject_form.html"
    success_url = reverse_lazy("academics:subject_list")
    success_message = "Subject %(object)s updated."
    extra_context = {"page_title": "Edit subject", "verb": "Save"}


class SubjectDeleteView(ManageAcademicsMixin, DeleteView):
    model = Subject
    template_name = "academics/subject_confirm_delete.html"
    success_url = reverse_lazy("academics:subject_list")
    extra_context = {"page_title": "Delete subject"}

    def form_valid(self, form):
        messages.success(self.request, f"Subject {self.object} deleted.")
        return super().form_valid(form)


# ---------------------------------------------------------------------------
# Excel import
#
# The third way in, beside the standard ladder and one-at-a-time. A school that
# runs Grade 1 to Grade 12, or names its classes after houses, cannot use the
# presets at all -- the ladder on the bulk screen is the Nigerian one -- and
# typing thirty rows into a web form is the back-and-forth those screens were
# built to remove. So the classes themselves can arrive by spreadsheet, and
# then so can the subjects.
#
# Everything about how an import behaves is in apps/core/import_flow.py. These
# two classes are the declarations.
# ---------------------------------------------------------------------------


class ClassImportFlow(ImportFlow):
    sheet = importers.CLASS_SHEET
    capability = Capability.MANAGE_ACADEMICS
    session_key = "academics.class_import"

    url_template = "academics:class_import_template"
    url_upload = "academics:class_import"
    url_review = "academics:class_import_review"
    url_done = "academics:class_list"

    upload_template_name = "academics/class_import.html"
    review_template_name = "academics/class_import_review.html"

    heading = "Import classes"
    intro = (
        "Your own class list from a spreadsheet. Nothing is saved until you "
        "have seen what the file contains."
    )
    scope_help = "Every class in the file is created at this campus."
    no_scope_title = "No campus to import into"
    no_scope_body = "A branch has to exist before classes can be set up at it."
    back_label = "classes"

    def scope_queryset(self):
        return Branch.objects.filter(is_active=True)

    def choices(self):
        # A drop-down rather than free text, because the four bands are ours and
        # a school cannot be expected to guess their spelling. Still optional:
        # blank is read off the class name.
        return (Choice("level", tuple(level_names())),)

    def validate(self, rows, scope, **parsed):
        return importers.validate_classes(rows, branch=scope, **parsed)

    def commit(self, report, scope):
        return importers.commit_classes(report)


class SubjectImportFlow(ImportFlow):
    sheet = importers.SUBJECT_SHEET
    capability = Capability.MANAGE_ACADEMICS
    session_key = "academics.subject_import"

    url_template = "academics:subject_import_template"
    url_upload = "academics:subject_import"
    url_review = "academics:subject_import_review"
    url_done = "academics:subject_list"

    upload_template_name = "academics/subject_import.html"
    review_template_name = "academics/subject_import_review.html"

    heading = "Import subjects"
    intro = (
        "Your own subject list from a spreadsheet, with the classes each one is "
        "taught in. Nothing is saved until you have seen what the file contains."
    )
    scope_help = "Every subject in the file is created at this campus."
    no_scope_title = "No campus to import into"
    no_scope_body = "A branch has to exist before subjects can be set up at it."
    back_label = "subjects"

    def scope_queryset(self):
        return Branch.objects.filter(is_active=True)

    def references(self, scope):
        """The campus's classes, listed but without a drop-down.

        A drop-down would be wrong here: Taught In holds a *list*, and Excel's
        list validation accepts one value per cell. So the names go on a tab the
        school can read and copy from, and the validation happens on our side
        where it can say which of the three names it did not recognise.
        """
        index = ClassIndex(scope)
        names = index.names
        if not names:
            return ()
        return (
            Reference(
                name="Classes",
                title=(
                    "Classes at this campus — copy these into Taught In, "
                    "separated by semicolons. A level name (Primary, Junior "
                    "Secondary) or All also works."
                ),
                values=tuple(level_names()) + tuple(names),
                width=64,
            ),
        )

    def validate(self, rows, scope, **parsed):
        return importers.validate_subjects(rows, branch=scope, **parsed)

    def commit(self, report, scope):
        return importers.commit_subjects(report)

    def upload_context(self, request):
        return {"class_count": Class.objects.filter(is_active=True).count()}


class ClassImportTemplateView(ImportTemplateView):
    flow_class = ClassImportFlow


class ClassImportView(ImportUploadView):
    flow_class = ClassImportFlow


class ClassImportReviewView(ImportReviewView):
    flow_class = ClassImportFlow


class SubjectImportTemplateView(ImportTemplateView):
    flow_class = SubjectImportFlow


class SubjectImportView(ImportUploadView):
    flow_class = SubjectImportFlow


class SubjectImportReviewView(ImportReviewView):
    flow_class = SubjectImportFlow
