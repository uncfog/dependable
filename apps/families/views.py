"""Family screens. Nothing filters by school: the scoped managers already have."""

from __future__ import annotations

from django.contrib import messages
from django.db import transaction
from django.db.models import Count, Q
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.generic import CreateView, DetailView, ListView, UpdateView, View

from apps.core.permissions import Capability, CapabilityRequiredMixin, has_capability
from apps.payments import shared

from .balances import summary_for
from .forms import AddChildForm, FamilyForm
from .models import Family, FamilyMembership
from . import suggestions
from .services import FamilyLinkError, link, unlink


class ViewFamiliesMixin(CapabilityRequiredMixin):
    capability = Capability.VIEW_FAMILIES


class ManageFamiliesMixin(CapabilityRequiredMixin):
    capability = Capability.MANAGE_FAMILIES


class FamilyListView(ViewFamiliesMixin, ListView):
    model = Family
    template_name = "families/family_list.html"
    context_object_name = "families"
    paginate_by = 25

    def get_queryset(self):
        families = (
            super()
            .get_queryset()
            .annotate(
                child_count=Count("memberships", filter=Q(memberships__ended_at__isnull=True))
            )
        )
        query = self.request.GET.get("q", "").strip()
        if query:
            families = families.filter(
                Q(name__icontains=query)
                | Q(family_code__icontains=query)
                | Q(contact_phone__icontains=query)
            )
        return families

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["query"] = self.request.GET.get("q", "").strip()
        context["page_title"] = "Families"
        return context


class FamilyDetailView(ViewFamiliesMixin, DetailView):
    model = Family
    template_name = "families/family_detail.html"
    context_object_name = "family"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["summary"] = summary_for(self.object)
        context["add_form"] = AddChildForm()
        if has_capability(self.request.user, Capability.VIEW_PAYMENTS):
            context["payments"] = shared.transactions().filter(family=self.object).order_by(
                "-date_paid", "-id"
            )[:10]
        context["page_title"] = self.object.name
        return context


class FamilyCreateView(ManageFamiliesMixin, CreateView):
    model = Family
    form_class = FamilyForm
    template_name = "families/family_form.html"
    extra_context = {"page_title": "New family", "verb": "Create family"}

    def _school(self):
        school = self.request.user.school
        if school is None:
            # A platform account belongs to no school, so there is nowhere to put it.
            raise Http404("Families are created from inside a school.")
        return school

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["school"] = self._school()
        return kwargs


class FamilyUpdateView(ManageFamiliesMixin, UpdateView):
    model = Family
    form_class = FamilyForm
    template_name = "families/family_form.html"
    extra_context = {"page_title": "Edit family", "verb": "Save"}


class FamilySuggestionsView(ManageFamiliesMixin, View):
    """Children who share a phone or email. Only ever a question; staff decide."""

    def get(self, request):
        return render(request, "families/suggestions.html", {
            "groups": suggestions.groups(),
            "page_title": "Suggested families",
        })

    def post(self, request):
        signature = request.POST.get("signature", "")
        group = next((g for g in suggestions.groups() if g.signature == signature), None)
        if group is None:
            messages.info(request, "That suggestion has already been dealt with.")
        elif request.POST.get("action") == "reject":
            suggestions.reject(group, by=request.user, school=group.students[0].school)
            messages.success(request, "Understood. This won't be suggested again.")
        else:
            self._confirm(request, group)
        return HttpResponseRedirect(reverse("families:suggestions"))

    def _confirm(self, request, group):
        chosen = set(request.POST.getlist("students"))
        # Only children in the group, whatever the request names.
        students = [s for s in group.students if str(s.pk) in chosen]
        name = request.POST.get("name", "").strip()
        if not name or len(students) < 2:
            messages.error(request, "Give the family a name and choose at least two children.")
            return
        with transaction.atomic():
            family = Family.all_objects.create(
                school_id=students[0].school_id, name=name[:160],
                contact_name=students[0].parent_name[:160],
                contact_phone=students[0].parent_phone,
            )
            for student in students:
                link(family, student, by=request.user)
        messages.success(request, f"{family.name} created with {len(students)} children.")


class AddChildView(ManageFamiliesMixin, View):
    def post(self, request, pk):
        family = get_object_or_404(Family.objects, pk=pk)
        form = AddChildForm(request.POST)
        if form.is_valid():
            student = form.cleaned_data["student"]
            try:
                link(family, student, by=request.user)
            except FamilyLinkError as refused:
                messages.error(request, str(refused))
            else:
                messages.success(request, f"{student.full_name} added to {family.name}.")
        else:
            messages.error(request, " ".join(form.errors.get("student", [])))
        return HttpResponseRedirect(family.get_absolute_url())


class RemoveChildView(ManageFamiliesMixin, View):
    """POST only. It ends the link and keeps the record, and no money moves."""

    def post(self, request, pk):
        membership = get_object_or_404(
            FamilyMembership.objects.select_related("family", "student"), pk=pk
        )
        unlink(membership, by=request.user)
        messages.success(
            request,
            f"{membership.student.full_name} removed from {membership.family.name}. "
            f"Their record and payments are unchanged.",
        )
        return HttpResponseRedirect(reverse("families:detail", args=[membership.family_id]))
