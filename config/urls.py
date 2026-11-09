from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.sitemaps.views import sitemap
from django.urls import include, path
from django.views.generic import RedirectView

from apps.core import pwa, seo
from apps.core.branding import PRODUCT_NAME
from apps.core.views import (
    BrandedLogoutView,
    BrandedPasswordResetConfirmView,
    BrandedPasswordResetView,
    RoleAwareLoginView,
)

admin.site.site_header = PRODUCT_NAME
admin.site.site_title = f"{PRODUCT_NAME} admin"
admin.site.index_title = "School management platform"

urlpatterns = [
    # For crawlers and AI assistants. What each says is in apps/core/seo.py.
    path("sw.js", pwa.ServiceWorkerView.as_view(), name="service_worker"),
    path("manifest.webmanifest", pwa.ManifestView.as_view(), name="web_manifest"),
    path("robots.txt", seo.RobotsTxtView.as_view(), name="robots_txt"),
    path("sitemap.xml", sitemap, {"sitemaps": seo.SITEMAPS}, name="sitemap"),
    path("llms.txt", seo.LlmsTxtView.as_view(), name="llms_txt"),
    path("admin/", admin.site.urls),
    # Ours, before the include below, so this is the "login" that gets reversed.
    # Everything else in the auth set -- logout, and the whole password-reset
    # flow -- is Django's, driven by the templates in templates/registration/.
    path("accounts/login/", RoleAwareLoginView.as_view(), name="login"),
    # Ours too, and for the same reason: signing out lands on a page that looks
    # identical to never having signed in, so the confirmation has to be
    # carried across the redirect as a message.
    path("accounts/logout/", BrandedLogoutView.as_view(), name="logout"),
    # Also ours, and for the same reason: the reset *email* is rendered without
    # a request, so context processors do not run and the product name has to
    # be handed in explicitly. See BrandedPasswordResetView.
    path(
        "accounts/password_reset/",
        BrandedPasswordResetView.as_view(),
        name="password_reset",
    ),
    # Ours so a reset sends the "your password was changed" notice. Same path
    # and name as Django's, which the reset email links to.
    path(
        "accounts/reset/<uidb64>/<token>/",
        BrandedPasswordResetConfirmView.as_view(),
        name="password_reset_confirm",
    ),
    # A password is changed on Account settings, next to the email address.
    # Django's standalone page would be a second, unlinked way to do the same.
    path(
        "accounts/password_change/",
        RedirectView.as_view(pattern_name="accounts:settings"),
        name="password_change",
    ),
    # Ours, and before the catch-all auth include so the name resolves here.
    path("accounts/", include("apps.accounts.urls")),
    path("accounts/", include("django.contrib.auth.urls")),
    path("branches/", include("apps.schools.urls")),
    path("plan/", include("apps.schools.plan_urls")),
    path("staff/", include("apps.accounts.staff_urls")),
    path("academics/", include("apps.academics.urls")),
    path("fees/", include("apps.fees.urls")),
    path("students/", include("apps.students.urls")),
    path("families/", include("apps.families.urls")),
    path("onboarding/", include("apps.onboarding.urls")),
    path("messaging/", include("apps.messaging.urls")),
    path("payments/", include("apps.payments.urls")),
    path("admissions/", include("apps.admissions.urls")),
    path("reports/", include("apps.reports.urls")),
    path("notifications/", include("apps.notifications.urls")),
    # The school's own public enquiry page, at /<school-slug>/enquiry/. Before
    # the core include so a two-segment public route can never be shadowed by a
    # catch-all added there later.
    path("", include("apps.admissions.public_urls")),
    path("", include("apps.core.urls")),
]

if settings.DEBUG:
    # Uploaded receipts. In production these are served by the web server or
    # object storage; Django deliberately refuses to do it with DEBUG off.
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

# Authorisation failures answer with a toast on the refusal page rather than a
# bare wall -- see apps.core.views.permission_denied. The status stays 403.
handler403 = "apps.core.views.permission_denied"
