from django.urls import path

from apps.schools import plan_views

from . import views

app_name = "core"

urlpatterns = [
    # Public.
    path("", views.LandingView.as_view(), name="landing"),
    path("signup/", views.SignupView.as_view(), name="signup"),
    path("privacy/", views.PrivacyView.as_view(), name="privacy"),
    path("about/", views.AboutView.as_view(), name="about"),

    # "The dashboard", for anything that should not have to know the role.
    path("dashboard/", views.dashboard, name="dashboard"),

    # One per role -- see navigation.ROLE_HOME, which decides who lands where.
    path("platform/", views.PlatformOverviewView.as_view(), name="platform_overview"),
    path("platform/billing/", plan_views.PlatformBillingView.as_view(), name="platform_billing"),
    path("platform/billing/prices/", plan_views.PlatformPricesView.as_view(), name="platform_prices"),
    path(
        "platform/billing/orders/<int:pk>/confirm/",
        plan_views.PlatformConfirmOrderView.as_view(),
        name="platform_billing_confirm",
    ),
    # The grid, on a URL of its own. It shared /platform/ with the dashboard
    # above for a while, which gave the sidebar two rows pointing at one screen
    # and lit both of them -- see PlatformOverviewView.
    path(
        "platform/schools/",
        views.PlatformSchoolListView.as_view(),
        name="platform_schools",
    ),
    # The platform owner opens a school from the cards above. Both are gated on
    # MANAGE_SCHOOL_MODULES rather than on the role, so a proprietor typing either
    # gets a 403 -- see PlatformSchoolView.
    path(
        "platform/schools/<int:pk>/",
        views.PlatformSchoolView.as_view(),
        name="platform_school",
    ),
    path(
        "platform/schools/<int:pk>/modules/",
        views.SchoolModuleToggleView.as_view(),
        name="school_module_toggle",
    ),
    path(
        "platform/schools/<int:pk>/profile/",
        views.PlatformSchoolProfileView.as_view(),
        name="platform_school_profile",
    ),
    path("school/", views.SchoolDashboardView.as_view(), name="school_dashboard"),
    path("branch/", views.BranchDashboardView.as_view(), name="branch_dashboard"),
    path("finance/", views.BursarDashboardView.as_view(), name="bursar_dashboard"),

    path("welcome/", views.OnboardingView.as_view(), name="onboarding"),
    # The school's own profile -- name, logo, office contacts. What the
    # "Settings" nav entry has been waiting for.
    path("settings/", views.SchoolSettingsView.as_view(), name="school_settings"),
    # Public: the logo is shown on the school's own enquiry page too.
    path(
        "schools/<slug:school_slug>/logo/",
        views.SchoolLogoView.as_view(),
        name="school_logo",
    ),
]
