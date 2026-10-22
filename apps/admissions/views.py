"""Admissions screens: the public front door, and the school's daily tool.

Two audiences, and the split is the whole shape of this module.

**The public page** is unauthenticated and scoped by the school's slug. It is
the second place on the platform -- after signup -- that runs with no tenant
context, so every queryset it touches is spelled ``all_objects`` and narrowed
by hand to the school in the URL. That narrowing is the security boundary:
nothing the browser sends decides which school a submission lands in.

**The staff screens** are the opposite. Not one of them filters by school or
branch, because ``Applicant.objects`` is a tenant-scoped manager and has
already done it -- a principal's pipeline is their own campus, another school's
applicant 404s rather than leaking, and no view here has to remember why.

What the staff screens do carry is the *stage* logic: which button a person
sees on an applicant depends on where that applicant stands, and every
transition is guarded server-side as well, so a stale tab cannot decide
somebody twice.
"""

from __future__ import annotations

from decimal import Decimal

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Count, Q, Sum
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.formats import date_format
from django.views.generic import (
    CreateView,
    DetailView,
    FormView,
    ListView,
    TemplateView,
    UpdateView,
    View,
)

from apps.academics.models import Class, Level
from apps.core import modules
from apps.schools.models import School

from . import enrolment, notifications
from .access import (
    DecideAdmissionsMixin,
    ManageAdmissionsMixin,
    ManageRequirementsMixin,
    RecordAdmissionPaymentsMixin,
    ViewAdmissionPaymentsMixin,
    ViewAdmissionsMixin,
    can_decide,
    can_manage_applicants,
    can_record_admission_payments,
    can_view_applicants,
)
from .forms import (
    AdmissionPaymentForm,
    AdmissionsConfigForm,
    ApplicationForm,
    AssessmentForm,
    DecisionForm,
    EnrolmentForm,
    PipelineFilterForm,
    PublicEnquiryForm,
    RequirementSetForm,
    StaffEnquiryForm,
)
from .models import (
    PIPELINE_STATUSES,
    AdmissionFeeSchedule,
    AdmissionPayment,
    AdmissionPaymentStatus,
    AdmissionsConfig,
    Applicant,
    ApplicantStatus,
    Assessment,
    RequirementSet,
)
from .requirements import profile_for

ZERO = Decimal("0")

#: How many applicants each pipeline column shows before it offers a link to
#: the rest. A board is for seeing the shape of the intake, not for reading
#: three hundred names.
COLUMN_LIMIT = 12


# ===========================================================================
# Public -- the school's own enquiry page
# ===========================================================================


class SchoolFromSlugMixin:
    """Resolve the school from the URL, or 404.

    ``all_objects`` because the visitor is anonymous and the scoped manager
    would find nothing -- and a scoping bug here would present as "every
    school's enquiry page is missing", which is at least loud.

    A suspended or cancelled school's page goes dark rather than quietly
    collecting enquiries nobody will read. So does a school that does not have
    the admissions module, and that check lives here rather than in
    ``ModuleAccessMiddleware`` for a reason worth stating: this page belongs to
    the school named in the URL, not to whoever is reading it, and usually nobody
    is signed in at all. The middleware knows the caller's school; only this
    mixin knows the page's.

    404 rather than the middleware's 403 page, and deliberately. To a parent with
    a link from Instagram there is no such page, and a signed-out stranger is owed
    no account of which features a school has or has not bought.
    """

    def get_school(self) -> School:
        if not hasattr(self, "_school"):
            school = get_object_or_404(
                School.all_objects, slug=self.kwargs["school_slug"]
            )
            if not school.is_active:
                raise Http404("This school is not accepting enquiries.")
            if "admissions" not in modules.enabled_for_school(school.pk):
                raise Http404("This school does not have an enquiry page.")
            self._school = school
        return self._school

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        school = self.get_school()
        context["school"] = school
        # The page is the school's, not the platform's: its name is the title,
        # its logo is the mark, and SCHOOLCORD appears nowhere on it.
        context["page_title"] = school.name
        return context


class PublicEnquiryView(SchoolFromSlugMixin, FormView):
    """``/<school-slug>/enquiry/`` -- what a school links from its Instagram bio.

    Deliberately branded to the school and to nothing else. A parent arriving
    from a WhatsApp status has come to enquire about *that school*; a page that
    introduced them to the software vendor instead would be answering a
    question they did not ask.
    """

    template_name = "admissions/public_enquiry.html"
    form_class = PublicEnquiryForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["school"] = self.get_school()
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        school = self.get_school()
        # A school with no campus or no active class cannot be enquired into
        # yet. Saying so beats an empty dropdown the parent cannot get past.
        #
        # Asked of the models rather than of the bound form: on a failed POST
        # `get_form()` would build a second copy of the form purely to read a
        # queryset off it.
        context["is_open"] = Class.all_objects.filter(
            branch__school=school, branch__is_active=True, is_active=True
        ).exists()
        return context

    def form_valid(self, form):
        applicant = form.save()
        # Best effort: an acknowledgement that does not send must not turn a
        # captured enquiry into an error page for the parent.
        notifications.send_enquiry_acknowledgement(applicant)
        self.request.session["admissions_reference"] = applicant.reference
        return HttpResponseRedirect(
            reverse("admissions_public:public_enquiry_done", args=[self.get_school().slug])
        )


class PublicEnquiryDoneView(SchoolFromSlugMixin, TemplateView):
    """"We have your enquiry" -- with the reference, and what happens next."""

    template_name = "admissions/public_enquiry_done.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Held in the session rather than the query string: a reference in a
        # URL is a reference in a browser history and a shared screenshot.
        context["reference"] = self.request.session.pop("admissions_reference", "")
        return context


# ===========================================================================
# Staff -- the pipeline
# ===========================================================================


class PipelineView(ViewAdmissionsMixin, TemplateView):
    """Every applicant, grouped by the stage they are at.

    The school's daily tool. Columns are the pipeline stages in order, so the
    board reads left to right the way the process runs, and the filters narrow
    every column at once rather than one at a time.

    Closed applicants -- rejected, expired, withdrawn -- are counted but not
    shown by default. They are still the answer to "what happened to that
    family?", and the status filter is how you get to them.
    """

    template_name = "admissions/pipeline.html"

    def get_filter_form(self) -> PipelineFilterForm:
        if not hasattr(self, "_filter_form"):
            self._filter_form = PipelineFilterForm(data=self.request.GET or None)
            self._filter_form.is_valid()
        return self._filter_form

    def get_queryset(self):
        # Already narrowed to the caller's school and branch by the manager.
        queryset = Applicant.objects.select_related(
            "school_class", "branch", "student"
        ).prefetch_related("assessment")

        form = self.get_filter_form()
        filters = form.cleaned_data if form.is_bound and form.is_valid() else {}

        term = filters.get("q")
        if term:
            queryset = queryset.filter(
                Q(first_name__icontains=term)
                | Q(last_name__icontains=term)
                | Q(other_names__icontains=term)
                | Q(reference__icontains=term)
                | Q(parent_name__icontains=term)
                | Q(parent_phone__icontains=term)
            )
        if filters.get("branch"):
            queryset = queryset.filter(branch=filters["branch"])
        if filters.get("level"):
            queryset = queryset.filter(level=filters["level"])
        if filters.get("school_class"):
            queryset = queryset.filter(school_class=filters["school_class"])
        if filters.get("status"):
            queryset = queryset.filter(status=filters["status"])
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        queryset = self.get_queryset()

        counts = {
            row["status"]: row["n"]
            for row in queryset.values("status").annotate(n=Count("id"))
        }
        chosen_status = (self.get_filter_form().data or {}).get("status") or ""

        # One query per column rather than one per applicant. Five columns on a
        # board is a fixed cost; slicing in Python would pull the whole intake.
        columns = []
        for status in PIPELINE_STATUSES:
            if chosen_status and chosen_status != status:
                continue
            applicants = list(queryset.filter(status=status)[:COLUMN_LIMIT])
            columns.append({
                "status": status,
                "label": ApplicantStatus(status).label,
                "count": counts.get(status, 0),
                "applicants": applicants,
                "overflow": max(counts.get(status, 0) - len(applicants), 0),
            })
        # A filter that picked a closed status has no pipeline column of its
        # own, so it gets one built for it rather than showing an empty board.
        if chosen_status and chosen_status not in PIPELINE_STATUSES:
            applicants = list(queryset.filter(status=chosen_status)[:COLUMN_LIMIT])
            columns = [{
                "status": chosen_status,
                "label": ApplicantStatus(chosen_status).label,
                "count": counts.get(chosen_status, 0),
                "applicants": applicants,
                "overflow": max(counts.get(chosen_status, 0) - len(applicants), 0),
            }]

        context["columns"] = columns
        context["counts"] = counts
        context["total"] = sum(counts.values())
        context["open_count"] = sum(
            counts.get(status, 0) for status in PIPELINE_STATUSES
        )
        context["closed_counts"] = [
            {"status": status, "label": ApplicantStatus(status).label, "count": n}
            for status, n in counts.items()
            if status not in PIPELINE_STATUSES and n
        ]
        # Enquiries whose window has run out but which nobody has swept yet.
        # Shown as a nudge rather than hidden until the nightly command runs.
        context["lapsed_count"] = Applicant.objects.lapsed().count()

        context["filter_form"] = self.get_filter_form()
        context["is_filtered"] = any(
            self.request.GET.get(name)
            for name in ("q", "branch", "level", "school_class", "status")
        )
        if not context["total"] and not context["is_filtered"]:
            context["enquiry_links"] = _public_links(self.request)
        context["page_title"] = "Applications"
        return context


def _public_links(request) -> list[dict]:
    """The public enquiry URL for each school the caller can see.

    Shown on an empty board, because "where do I send parents?" is the first
    question a school asks and the answer is a URL they have never been told.
    """
    return [
        {
            "school": school,
            "url": request.build_absolute_uri(
                reverse("admissions_public:public_enquiry", args=[school.slug])
            ),
        }
        for school in School.objects.all()[:5]
    ]


class ApplicantDetailView(ViewAdmissionsMixin, DetailView):
    """One applicant: everything captured, what is outstanding, and what is next."""

    model = Applicant
    template_name = "admissions/applicant_detail.html"
    context_object_name = "applicant"

    def get_queryset(self):
        return (
            Applicant.objects.select_related(
                "school", "branch", "school_class", "student", "decided_by"
            )
            .prefetch_related("assessment", "payments")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        applicant = self.object
        profile = applicant.requirement_profile

        context["profile"] = profile
        context["missing"] = applicant.missing_requirements
        context["assessment"] = getattr(applicant, "assessment", None)
        context["schedule"] = applicant.fee_schedule
        context["payments"] = list(
            applicant.payments.select_related("recorded_by", "term_payment")
        )
        context["blockers"] = enrolment.can_enrol(applicant)

        # What this person may do to this applicant, worked out once here so
        # the template asks about buttons rather than about roles.
        user = self.request.user
        manage = can_manage_applicants(user)
        context["can_advance"] = manage and applicant.status == ApplicantStatus.ENQUIRY
        context["can_assess"] = (
            manage
            and profile.requires_assessment
            and applicant.status
            in (ApplicantStatus.APPLICATION, ApplicantStatus.ASSESSED)
        )
        context["can_decide"] = can_decide(user) and applicant.status in (
            ApplicantStatus.APPLICATION,
            ApplicantStatus.ASSESSED,
        )
        context["can_enrol"] = manage and applicant.status == ApplicantStatus.OFFERED
        context["can_withdraw"] = manage and applicant.is_open
        context["can_pay"] = can_record_admission_payments(user) and applicant.is_open
        context["page_title"] = applicant.full_name
        return context


class EnquiryCreateView(ManageAdmissionsMixin, CreateView):
    """The walk-in form. Same model, same pipeline, branch taken from the user."""

    model = Applicant
    form_class = StaffEnquiryForm
    template_name = "admissions/enquiry_form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "New enquiry"
        return context

    def form_valid(self, form):
        response = super().form_valid(form)
        applicant = self.object
        note = f"Enquiry {applicant.reference} captured for {applicant.full_name}."
        if applicant.expires_at:
            # date_format rather than strftime: the platform-specific
            # no-padding codes (%-d on Linux, %#d on Windows) are not portable,
            # and Django's own formatter is already what every template uses.
            when = date_format(timezone.localtime(applicant.expires_at), "j F Y")
            note += f" It expires on {when}."
        messages.success(self.request, note)
        return response


class ApplicationUpdateView(ManageAdmissionsMixin, UpdateView):
    """Enquiry to application: add what is missing, re-ask for nothing."""

    model = Applicant
    form_class = ApplicationForm
    template_name = "admissions/application_form.html"
    context_object_name = "applicant"

    def get_queryset(self):
        return Applicant.objects.select_related("school_class", "branch")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        applicant = self.object
        context["profile"] = applicant.requirement_profile
        # The whole point of the screen, said out loud at the top of it.
        context["carried_over"] = [
            ("Child", applicant.full_name),
            ("Date of birth", applicant.date_of_birth),
            ("Sex", applicant.get_sex_display()),
            ("Parent / guardian", applicant.parent_name),
            ("Phone", applicant.parent_phone_display),
            ("Email", applicant.parent_email),
            ("Previous school", applicant.previous_school),
        ]
        context["page_title"] = f"Application — {applicant.full_name}"
        return context

    def form_valid(self, form):
        response = super().form_valid(form)
        applicant = self.object
        if applicant.requires_assessment and not applicant.has_sat_assessment:
            messages.success(
                self.request,
                f"{applicant.full_name}'s application is in. "
                f"{applicant.level_label} sits the entrance assessment — "
                f"schedule it next.",
            )
        else:
            messages.success(
                self.request,
                f"{applicant.full_name}'s application is in. "
                f"{applicant.level_label} does not sit an assessment, so this "
                f"is ready for a decision.",
            )
        return response

    def get_success_url(self):
        return self.object.get_absolute_url()


class AssessmentUpdateView(ManageAdmissionsMixin, UpdateView):
    """Schedule or record the entrance exam.

    404s for a level that does not sit one. Nursery skipping the assessment is
    not a hidden button -- it is a URL that does not exist for that applicant,
    so a bookmarked link cannot create a record the level has no use for.
    """

    model = Assessment
    form_class = AssessmentForm
    template_name = "admissions/assessment_form.html"

    def get_object(self, queryset=None) -> Assessment:
        self.applicant = get_object_or_404(
            Applicant.objects.select_related("branch"), pk=self.kwargs["pk"]
        )
        if not self.applicant.requirement_profile.requires_assessment:
            raise Http404(
                f"{self.applicant.level_label} applicants do not sit an "
                f"entrance assessment."
            )
        assessment, _ = Assessment.all_objects.get_or_create(
            applicant=self.applicant,
            defaults={
                "school_id": self.applicant.school_id,
                "branch_id": self.applicant.branch_id,
            },
        )
        return assessment

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["applicant"] = self.applicant
        context["page_title"] = f"Assessment — {self.applicant.full_name}"
        return context

    def form_valid(self, form):
        form.instance.recorded_by = self.request.user
        response = super().form_valid(form)
        applicant = self.applicant
        if self.object.has_been_sat and applicant.status == ApplicantStatus.APPLICATION:
            applicant.status = ApplicantStatus.ASSESSED
            applicant.save(update_fields=["status", "updated_at"])
            messages.success(
                self.request,
                f"Assessment recorded. {applicant.full_name} is ready for a "
                f"decision.",
            )
        else:
            messages.success(self.request, "Assessment saved.")
        return response

    def get_success_url(self):
        return self.applicant.get_absolute_url()


class DecisionView(DecideAdmissionsMixin, FormView):
    """Offer a place, or refuse one. Principal and owner only, always.

    Guarded twice over: the mixin refuses the wrong role, and the stage check
    below refuses the right role at the wrong moment -- a second tab left open
    on a decided applicant cannot decide them again.
    """

    form_class = DecisionForm
    template_name = "admissions/decision_form.html"

    def dispatch(self, request, *args, **kwargs):
        self.applicant = get_object_or_404(
            Applicant.objects.select_related("school", "branch", "school_class"),
            pk=kwargs["pk"],
        )
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["applicant"] = self.applicant
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        applicant = self.applicant
        context["applicant"] = applicant
        context["profile"] = applicant.requirement_profile
        context["missing"] = applicant.missing_requirements
        context["assessment"] = getattr(applicant, "assessment", None)
        context["page_title"] = f"Decision — {applicant.full_name}"
        return context

    def form_valid(self, form):
        applicant = self.applicant
        if applicant.status not in (
            ApplicantStatus.APPLICATION,
            ApplicantStatus.ASSESSED,
        ):
            messages.error(
                self.request,
                f"{applicant.full_name} is at the "
                f"{applicant.status_label.lower()} stage, so there is nothing "
                f"to decide.",
            )
            return redirect(applicant.get_absolute_url())

        applicant.status = form.cleaned_data["decision"]
        applicant.decision_note = form.cleaned_data["decision_note"]
        applicant.decided_at = timezone.now()
        applicant.decided_by = self.request.user
        applicant.save()

        offered = applicant.status == ApplicantStatus.OFFERED
        sent = False
        if form.cleaned_data.get("notify_parent"):
            sent = notifications.send_decision(applicant)

        verb = "offered a place" if offered else "not offered a place"
        told = "The parent has been emailed." if sent else "The parent was not emailed."
        messages.success(self.request, f"{applicant.full_name} was {verb}. {told}")
        return redirect(applicant.get_absolute_url())


class EnrolView(ManageAdmissionsMixin, FormView):
    """The conversion. Creates the Student and closes the application."""

    form_class = EnrolmentForm
    template_name = "admissions/enrol_form.html"

    def dispatch(self, request, *args, **kwargs):
        self.applicant = get_object_or_404(
            Applicant.objects.select_related("school", "branch", "school_class"),
            pk=kwargs["pk"],
        )
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["applicant"] = self.applicant
        return kwargs

    def get_initial(self):
        applicant = self.applicant
        return {
            "admission_number": enrolment.suggest_admission_number(applicant.branch),
            "school_class": applicant.school_class_id,
            "date_admitted": timezone.localdate(),
        }

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        applicant = self.applicant
        context["applicant"] = applicant
        context["schedule"] = applicant.fee_schedule
        # Shown before the form, so a blocked enrolment explains itself rather
        # than failing on submit.
        context["blockers"] = enrolment.can_enrol(applicant)
        context["page_title"] = f"Enrol {applicant.full_name}"
        return context

    def form_valid(self, form):
        try:
            student = enrolment.enrol(
                self.applicant,
                admission_number=form.cleaned_data["admission_number"],
                school_class=form.cleaned_data["school_class"],
                date_admitted=form.cleaned_data["date_admitted"],
                actor=self.request.user,
            )
        except enrolment.EnrolmentError as error:
            form.add_error(None, str(error))
            return self.form_invalid(form)
        except ValidationError as error:
            # Almost always a duplicate admission number: the roster's
            # uniqueness rule is per branch, and `enrol` runs full_clean so the
            # collision arrives here as a field error rather than as a 500 from
            # the database.
            for field, errors in error.message_dict.items():
                target = field if field in form.fields else None
                for message in errors:
                    form.add_error(target, message)
            return self.form_invalid(form)

        messages.success(
            self.request,
            f"{student.full_name} is on the roll in "
            f"{student.school_class.display_name} as {student.admission_number}.",
        )
        return redirect(student.get_absolute_url())


class WithdrawView(ManageAdmissionsMixin, View):
    """The family changed their mind. Closed, kept, and out of the pipeline."""

    def post(self, request, *args, **kwargs):
        applicant = get_object_or_404(Applicant.objects, pk=kwargs["pk"])
        if not applicant.is_open:
            messages.error(
                request, f"{applicant.full_name} is already closed."
            )
            return redirect(applicant.get_absolute_url())

        applicant.status = ApplicantStatus.WITHDRAWN
        applicant.withdrawn_at = timezone.now()
        applicant.closing_note = (request.POST.get("closing_note") or "")[:250]
        applicant.save()
        messages.success(
            request,
            f"{applicant.full_name} has been withdrawn. The record is kept.",
        )
        return redirect(applicant.get_absolute_url())


class AdmissionPaymentCreateView(RecordAdmissionPaymentsMixin, CreateView):
    """Money at the door. The bursar's screen, and separate from term payments."""

    model = AdmissionPayment
    form_class = AdmissionPaymentForm
    template_name = "admissions/payment_form.html"

    def dispatch(self, request, *args, **kwargs):
        # Applicant.objects is scoped, so a bursar at another school 404s here
        # whatever the toggle says about seeing the pipeline.
        self.applicant = get_object_or_404(
            Applicant.objects.select_related("branch", "school_class"),
            pk=kwargs["pk"],
        )
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["applicant"] = self.applicant
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["applicant"] = self.applicant
        context["schedule"] = self.applicant.fee_schedule
        context["page_title"] = f"Admission payment — {self.applicant.full_name}"
        return context

    def form_valid(self, form):
        form.instance.applicant = self.applicant
        form.instance.school_id = self.applicant.school_id
        form.instance.branch_id = self.applicant.branch_id
        form.instance.recorded_by = self.request.user
        response = super().form_valid(form)
        messages.success(
            self.request,
            f"{self.object.amount:,.0f} recorded against "
            f"{self.applicant.reference}.",
        )
        return response

    def get_success_url(self):
        # A bursar who cannot see the pipeline has nowhere to be sent on the
        # applicant, so they go back to the money screen they came from.
        if can_view_applicants(self.request.user):
            return self.applicant.get_absolute_url()
        return reverse("admissions:fee_schedules")


class FeeScheduleListView(ViewAdmissionPaymentsMixin, ListView):
    """What each class charges to come in, and what has been collected.

    Read-only. Admission fees are seeded from
    :mod:`apps.admissions.admission_pricing` and edited in the admin, the same
    way the termly fee schedule was before it grew its own editor.
    """

    model = AdmissionFeeSchedule
    template_name = "admissions/fee_schedules.html"
    context_object_name = "schedules"

    def get_queryset(self):
        return (
            AdmissionFeeSchedule.objects.select_related("school_class", "branch")
            .prefetch_related("items")
            .filter(is_active=True)
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        totals = AdmissionPayment.objects.aggregate(
            confirmed=Sum("amount", filter=Q(status=AdmissionPaymentStatus.CONFIRMED)),
            pending=Sum("amount", filter=Q(status=AdmissionPaymentStatus.PENDING)),
            pending_count=Count("id", filter=Q(status=AdmissionPaymentStatus.PENDING)),
        )
        context["collected_total"] = totals["confirmed"] or ZERO
        context["pending_total"] = totals["pending"] or ZERO
        context["pending_count"] = totals["pending_count"] or 0
        context["recent_payments"] = list(
            AdmissionPayment.objects.select_related("applicant", "recorded_by")[:15]
        )
        context["page_title"] = "Admission fees"
        return context


class AdmissionsSettingsView(ManageAdmissionsMixin, UpdateView):
    """The school's admissions policy, and the requirements each level carries.

    Scoped by ``get_object`` rather than by a URL parameter: a school user has
    exactly one policy to edit, and offering an id would only invite guessing
    at somebody else's.
    """

    form_class = AdmissionsConfigForm
    template_name = "admissions/settings.html"
    success_url = reverse_lazy("admissions:settings")

    def get_object(self, queryset=None) -> AdmissionsConfig:
        school = getattr(self.request.user, "school", None)
        if school is None:
            # Platform staff have no school of their own to configure; they use
            # the admin, which is where cross-tenant work belongs.
            raise Http404("This account is not attached to a school.")
        return AdmissionsConfig.for_school(school)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        school = getattr(self.request.user, "school", None)
        branch = getattr(self.request.user, "branch", None)
        # What is actually in force per level, whether configured or default,
        # so a school can see the policy it is running on before changing it.
        context["profiles"] = [
            profile_for(level.value, school=school, branch=branch)
            for level in Level
        ]
        context["page_title"] = "Admissions settings"
        return context

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, "Admissions settings saved.")
        return response

# ---------------------------------------------------------------------------
# Admission requirements
#
# The settings screen showed these read-only and told the reader to edit them
# "in the admin" -- which the proprietor's own account cannot open. So they are
# editable here, by the proprietor only: a principal runs the pipeline under
# the rules, they do not rewrite the rules.
# ---------------------------------------------------------------------------


class RequirementListView(ViewAdmissionsMixin, TemplateView):
    """What each level asks for today, and whether it is ours or the default."""

    template_name = "admissions/requirement_list.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        school = getattr(self.request.user, "school", None)
        branch = getattr(self.request.user, "branch", None)
        context["profiles"] = [
            profile_for(level.value, school=school, branch=branch) for level in Level
        ]
        context["page_title"] = "Admission requirements"
        return context


class RequirementUpdateView(ManageRequirementsMixin, FormView):
    """Edit one level's policy.

    The level comes from the URL and is checked against the ladder, not trusted:
    an unknown one is a 404 rather than a set filed under a level that does not
    exist.
    """

    template_name = "admissions/requirement_form.html"
    form_class = RequirementSetForm
    success_url = reverse_lazy("admissions:requirements")

    def dispatch(self, request, *args, **kwargs):
        self.level = self._resolve_level(kwargs.get("level"))
        return super().dispatch(request, *args, **kwargs)

    @staticmethod
    def _resolve_level(raw) -> int:
        try:
            return Level(int(raw)).value
        except (TypeError, ValueError):
            raise Http404("No such level.")

    def _school(self):
        school = getattr(self.request.user, "school", None)
        if school is None:
            # Platform staff have no school of their own whose policy this
            # would be. Cross-tenant work belongs in the admin.
            raise Http404("This account is not attached to a school.")
        return school

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["level"] = self.level
        kwargs["school"] = self._school()
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        profile = profile_for(self.level, school=self._school())
        context["profile"] = profile
        context["level_label"] = profile.level_label
        context["page_title"] = f"{profile.level_label} requirements"
        return context

    def form_valid(self, form):
        form.save()
        messages.success(
            self.request,
            f"{Level(self.level).label} requirements saved. They apply to every "
            f"new application from now on.",
        )
        return super().form_valid(form)


class RequirementResetView(ManageRequirementsMixin, View):
    """Delete a level's own policy, so the platform default applies again.

    POST only: a link that a crawler or a prefetch could follow must not undo a
    school's configuration.
    """

    def post(self, request, *args, **kwargs):
        level = RequirementUpdateView._resolve_level(kwargs.get("level"))
        school = getattr(request.user, "school", None)
        if school is None:
            raise Http404("This account is not attached to a school.")
        # Scoped manager: a set belonging to another school is not found here,
        # rather than found and deleted.
        deleted, _ = RequirementSet.objects.filter(
            school=school, level=level, branch__isnull=True
        ).delete()
        if deleted:
            messages.success(
                request,
                f"{Level(level).label} requirements are back to the platform default.",
            )
        else:
            messages.info(
                request, f"{Level(level).label} was already on the default."
            )
        return HttpResponseRedirect(reverse("admissions:requirements"))

