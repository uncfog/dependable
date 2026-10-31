"""Staff admissions URLs, mounted at ``/admissions/``.

The public enquiry page is deliberately *not* here -- it lives at the root
under the school's slug, so a school can hand a parent
``schoolcord.app/fulfilled-academy/enquiry`` rather than a path with the
platform's own vocabulary in it. See :mod:`apps.admissions.public_urls`.
"""

from django.urls import path

from . import views

app_name = "admissions"

urlpatterns = [
    path("", views.PipelineView.as_view(), name="pipeline"),
    # Before the <int:pk> patterns, so these words are never read as ids.
    path("new/", views.EnquiryCreateView.as_view(), name="enquiry_create"),
    path("walk-in/", views.WalkInEnrolView.as_view(), name="walk_in"),
    path("walk-in/bill/", views.WalkInBillView.as_view(), name="walk_in_bill"),
    path("fees/", views.FeeScheduleListView.as_view(), name="fee_schedules"),
    path("settings/", views.AdmissionsSettingsView.as_view(), name="settings"),
    path(
        "requirements/",
        views.RequirementListView.as_view(),
        name="requirements",
    ),
    path(
        "requirements/<int:level>/edit/",
        views.RequirementUpdateView.as_view(),
        name="requirement_update",
    ),
    path(
        "requirements/<int:level>/reset/",
        views.RequirementResetView.as_view(),
        name="requirement_reset",
    ),

    path("<int:pk>/", views.ApplicantDetailView.as_view(), name="applicant_detail"),
    path(
        "<int:pk>/documents/<slug:kind>/",
        views.ApplicantDocumentView.as_view(),
        name="applicant_document",
    ),
    path(
        "<int:pk>/application/",
        views.ApplicationUpdateView.as_view(),
        name="application",
    ),
    path(
        "<int:pk>/assessment/",
        views.AssessmentUpdateView.as_view(),
        name="assessment",
    ),
    path("<int:pk>/decision/", views.DecisionView.as_view(), name="decision"),
    path("<int:pk>/enrol/", views.EnrolView.as_view(), name="enrol"),
    path("<int:pk>/withdraw/", views.WithdrawView.as_view(), name="withdraw"),
    path(
        "<int:pk>/payment/",
        views.AdmissionPaymentCreateView.as_view(),
        name="payment_create",
    ),
]
