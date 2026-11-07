"""Student management screens.

As with academics and fees, no view here filters by school or branch:
``Student.objects`` is a tenant-scoped manager, so the list is already narrowed
and another branch's student 404s rather than leaking.

What is new at this layer is the fee position shown against each student. It is
never read off the student -- :mod:`apps.students.fees` derives it from the
class's fee structure for the current term, in a fixed number of queries per
page.

The Excel import is three screens rather than one: download the template,
upload and see the verdict, then confirm. The middle screen writes nothing, and
the parsed rows wait in the session between it and the confirmation, where they
are validated a *second* time against the live database -- the roster can have
changed while the report was on screen, and the report the user agreed to has to
be the one that gets written.
"""

from __future__ import annotations

from decimal import Decimal

from django.contrib import messages
from django.db.models import Q
from django.http import HttpResponseRedirect
from django.urls import reverse, reverse_lazy
from django.views.generic import (
    CreateView,
    DetailView,
    ListView,
    UpdateView,
)

from apps.academics.models import Class
from apps.core.import_flow import (
    ImportFlow,
    ImportReviewView,
    ImportTemplateView,
    ImportUploadView,
)
from apps.core.permissions import Capability, CapabilityRequiredMixin
from apps.families.models import FamilyMembership
from apps.fees.models import REDUCING_KINDS
from apps.schools.models import Branch

from . import fees, importer, workbook
from .forms import StudentFilterForm, StudentForm
from .models import Student, StudentStatus


class ReadStudentsMixin(CapabilityRequiredMixin):
    """Every role reaches the roster; a bursar needs it to record payments."""

    capability = Capability.VIEW_STUDENTS


class ManageStudentsMixin(CapabilityRequiredMixin):
    """Owner- and principal-level only. A bursar reaches these and gets a 403."""

    capability = Capability.MANAGE_STUDENTS


class StudentListView(ReadStudentsMixin, ListView):
    """The branch roster: searchable, filterable, paginated.

    Twenty-five to a page: enough that a class fits on one screen, few enough
    that the fee lookup stays a fixed cost per page.
    """

    model = Student
    template_name = "students/student_list.html"
    context_object_name = "students"
    paginate_by = 25

    def get_filter_form(self) -> StudentFilterForm:
        if not hasattr(self, "_filter_form"):
            params = self.request.GET.copy()
            # An absent status means "the people actually here"; an explicitly
            # empty one means the caller asked for everybody.
            params.setdefault("status", StudentStatus.ACTIVE)
            self._filter_form = StudentFilterForm(data=params)
            self._filter_form.is_valid()
        return self._filter_form

    def get_queryset(self):
        queryset = super().get_queryset().select_related(
            "school_class", "branch"
        )
        form = self.get_filter_form()
        filters = form.cleaned_data if form.is_valid() else {}

        term = filters.get("q")
        if term:
            # Admission numbers are searched whole and by fragment, because
            # staff quote either "FA/2025/014" or just "014".
            queryset = queryset.filter(
                Q(first_name__icontains=term)
                | Q(last_name__icontains=term)
                | Q(other_names__icontains=term)
                | Q(admission_number__icontains=term)
            )
        if filters.get("school_class"):
            queryset = queryset.filter(school_class=filters["school_class"])
        if filters.get("status"):
            queryset = queryset.filter(status=filters["status"])
        if filters.get("balance"):
            # A balance is derived, not stored, so this chip has to work each child out.
            students = list(queryset)
            schedule = fees.load(students)
            owes = filters["balance"] == "owes"
            return [
                student
                for student in students
                if (position := schedule.position_for(student)).is_priced
                and (position.total_owed > fees.ZERO) == owes
            ]
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        page = list(context["students"])

        # Two queries for the whole page, however many classes it spans.
        schedule = fees.load(page)
        context["rows"] = [
            {"student": student, "position": schedule.position_for(student)}
            for student in page
        ]
        context["expected_on_page"] = sum(
            (row["position"].expected for row in context["rows"]), fees.ZERO
        )
        context["unpriced_on_page"] = sum(
            1 for row in context["rows"] if not row["position"].is_priced
        )
        context["current_terms"] = list(schedule.terms.values())

        context["filter_form"] = self.get_filter_form()
        context["is_filtered"] = any(
            self.request.GET.get(name)
            for name in ("q", "school_class", "status", "balance")
        )
        # Only asked when the filters found nothing, to tell the user whether the
        # roster is empty or their search was too narrow.
        if not page:
            context["total_count"] = Student.objects.count()
        context["page_title"] = "Students"
        return context


class StudentDetailView(ReadStudentsMixin, DetailView):
    model = Student
    template_name = "students/student_detail.html"
    context_object_name = "student"

    def get_queryset(self):
        return super().get_queryset().select_related("school_class", "branch", "school")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        student = self.object
        schedule = fees.load([student])
        position = schedule.position_for(student)
        context["position"] = position
        context["statement"] = schedule.statement_for(student)
        context["reducing_kinds"] = REDUCING_KINDS
        # The line items behind the expected figure: a parent asking "what am I
        # paying for?" is answered on this page, not by hopping to /fees/.
        context["fee_components"] = (
            list(position.structure.components.all()) if position.structure else []
        )
        context["classmate_count"] = (
            Student.objects.filter(
                school_class_id=student.school_class_id, status=StudentStatus.ACTIVE
            )
            .exclude(pk=student.pk)
            .count()
        )
        context.update(payment_history(student, self.request))
        context["family_membership"] = (
            FamilyMembership.objects.filter(student=student, ended_at__isnull=True)
            .select_related("family")
            .first()
        )
        context["page_title"] = student.full_name
        return context


#: How many receipts the detail page shows before sending the reader to the
#: full history. Enough that a typical term's payments fit; few enough that a
#: student with three years of history does not bury the page.
DETAIL_PAYMENTS = 8


def payment_history(student, request) -> dict:
    """The payment card's context, or an honest empty one.

    Resolved through the app registry rather than imported, for the same reason
    ``apps.students.fees`` does it: the roster shipped before payments did, and
    a hard import would tie this screen's ability to render to an app that may
    not be installed.
    """
    from django.apps import apps as django_apps

    from apps.core.permissions import Capability, has_capability

    context = {
        "payments": [],
        "payment_count": 0,
        "more_payments": False,
        "pending_total": None,
        "can_record_payments": has_capability(
            request.user, Capability.RECORD_PAYMENTS
        ),
    }
    if not django_apps.is_installed("apps.payments"):
        return context

    from apps.payments.models import Payment, PaymentStatus

    rows = Payment.objects.filter(student=student).select_related("term")
    context["payment_count"] = rows.count()
    context["payments"] = list(rows[:DETAIL_PAYMENTS])
    context["more_payments"] = context["payment_count"] > DETAIL_PAYMENTS

    # Surfaced next to the balance, because "I paid last week" and "the balance
    # has not moved" are both true while a receipt sits in the pending queue.
    pending = sum(
        (p.amount for p in rows.filter(status=PaymentStatus.PENDING)),
        Decimal("0"),
    )
    context["pending_total"] = pending or None

    # Arriving from an enrolment (?receipt=<payment>): the receipt to print is
    # the first thing on the page. Looked up within this student's own rows, so
    # a hand-edited id can only ever name one of their payments.
    receipt = request.GET.get("receipt", "")
    if receipt.isdigit():
        context["fresh_receipt"] = rows.filter(pk=int(receipt)).first()
    return context


class StudentCreateView(ManageStudentsMixin, CreateView):
    model = Student
    form_class = StudentForm
    template_name = "students/student_form.html"
    extra_context = {"page_title": "New student", "verb": "Enrol"}

    def get_initial(self):
        initial = super().get_initial()
        # Arrive from a filtered list and the class is already chosen, so
        # enrolling a whole intake into one class is one field less each time.
        requested_class = self.request.GET.get("school_class")
        if requested_class:
            klass = Class.objects.filter(pk=requested_class).first()
            if klass:
                initial["school_class"] = klass
        return initial

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(
            self.request,
            f"{self.object.full_name} enrolled in "
            f"{self.object.school_class.display_name} as "
            f"{self.object.admission_number}.",
        )
        return response

    def get_success_url(self):
        return self.object.get_absolute_url()


class StudentUpdateView(ManageStudentsMixin, UpdateView):
    model = Student
    form_class = StudentForm
    template_name = "students/student_form.html"
    extra_context = {"verb": "Save"}

    def get_queryset(self):
        return super().get_queryset().select_related("school_class", "branch")

    def get_initial(self):
        initial = super().get_initial()
        # "Withdraw instead" on the delete page lands here with the status
        # pre-selected. It is still only a suggestion until the form is saved.
        requested = self.request.GET.get("status")
        if requested in StudentStatus.values:
            initial["status"] = requested
        return initial

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"Edit — {self.object.full_name}"
        return context

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, f"{self.object.full_name} updated.")
        return response

    def get_success_url(self):
        return self.object.get_absolute_url()


class StudentRemoveView(ManageStudentsMixin, DetailView):
    """Take a student off the roster -- by withdrawing them, never by deleting.

    "Remove" is what a principal or proprietor means when a child leaves, and
    what they need afterwards is the child's payment history still attached to
    a person. So the action is a status change: the student drops off the
    active roster and stops being billed, and the record, its payments and
    every receipt that names them stay exactly where they were. Undoing it is
    setting the status back to Active on the edit screen.

    Held by everyone with MANAGE_STUDENTS -- the owner and the principal. It
    touches the roster only; the principal's read-only hold on classes, fees
    and settings is a different set of capabilities and is not widened here.
    """

    model = Student
    template_name = "students/student_confirm_remove.html"
    context_object_name = "student"
    extra_context = {"page_title": "Remove student"}

    def get_queryset(self):
        return super().get_queryset().select_related("school_class", "branch")

    def post(self, request, *args, **kwargs):
        student = self.get_object()
        if student.status == StudentStatus.WITHDRAWN:
            messages.info(request, f"{student.full_name} was already withdrawn.")
        else:
            student.status = StudentStatus.WITHDRAWN
            student.save(update_fields=["status", "updated_at"])
            messages.success(
                request,
                f"{student.full_name} removed from the roster and marked "
                f"withdrawn. Their record and payment history are kept.",
            )
        return HttpResponseRedirect(reverse("students:student_list"))


# ===========================================================================
# Excel import
# ===========================================================================

class StudentImportFlow(ImportFlow):
    """The student import, declared against the shared machinery."""

    sheet = importer.SHEET
    capability = Capability.MANAGE_STUDENTS
    session_key = "students.import"

    url_template = "students:student_import_template"
    url_upload = "students:student_import"
    url_review = "students:student_import_review"
    url_done = "students:student_list"

    upload_template_name = "students/student_import.html"
    review_template_name = "students/student_import_review.html"

    heading = "Import students"
    intro = (
        "Bulk enrolment from a spreadsheet. Nothing is saved until you have "
        "seen what the file contains."
    )
    scope_label = "Campus"
    scope_help = "Every student in the file is enrolled at this branch."
    scope_field_name = "branch"
    no_scope_title = "No campus to import into"
    no_scope_body = "A branch has to exist before students can be enrolled at it."
    back_label = "roster"

    def scope_queryset(self):
        return Branch.objects.filter(is_active=True)

    def choices(self):
        return workbook.choices()

    def references(self, scope):
        return workbook.references(importer.ClassIndex(scope).names)

    def validate(self, rows, scope, **parsed):
        return importer.validate(rows, branch=scope, **parsed)

    def commit(self, report, scope):
        return importer.commit(report)

    def upload_context(self, request):
        # Drives the "set up your classes first" nudge: every student has to be
        # placed in a class that already exists, so an empty class list is the
        # one prerequisite worth saying out loud before the download.
        return {"class_count": Class.objects.filter(is_active=True).count()}


class StudentImportTemplateView(ImportTemplateView):
    flow_class = StudentImportFlow


class StudentImportView(ImportUploadView):
    flow_class = StudentImportFlow


class StudentImportReviewView(ImportReviewView):
    flow_class = StudentImportFlow
