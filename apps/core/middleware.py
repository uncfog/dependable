"""Per-request plumbing: who is asking, and which features their school has."""

from __future__ import annotations

from django.shortcuts import render
from django.urls import Resolver404, resolve

from .tenancy import TenantContext, activate, deactivate


def module_unavailable(request, module):
    """The answer to a URL belonging to a module this school does not have.

    A 403 with a page that says which feature it was and who can switch it on --
    not a 404, which would be a small lie to somebody who works here, and not a
    redirect to the dashboard, which reads as the click having failed. The status
    stays 403 so a crawler or a script sees a refusal rather than a page.
    """
    from .branding import SUPPORT_EMAIL

    return render(
        request,
        "403_module.html",
        {
            "module": module,
            "support_email": SUPPORT_EMAIL,
            "page_title": f"{module.label} is not available",
        },
        status=403,
    )


class TenantMiddleware:
    """Derive the tenant from ``request.user`` and publish it to the ORM.

    Must sit *after* ``AuthenticationMiddleware`` so ``request.user`` exists.
    The context is always reset in a ``finally`` block: worker threads are
    reused, and a leaked context would hand the next request someone else's
    school.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        context = TenantContext.from_user(getattr(request, "user", None))
        request.tenant = context
        token = activate(context)
        try:
            return self.get_response(request)
        finally:
            deactivate(token)


#: What a locked-out account can still reach: the way out, and pages anyone can read.
_OPEN_WHEN_LOCKED = frozenset(
    {
        "logout",
        "core:landing",
        "core:privacy",
        "core:about",
        "core:school_logo",
        "robots_txt",
        "sitemap",
        "llms_txt",
        "admissions_public:public_enquiry",
        "admissions_public:public_enquiry_done",
    }
)

#: What a read-only school can still change: a person's own account and their way out.
_WRITABLE_WHEN_READ_ONLY = frozenset(
    {"logout", "accounts:settings", "accounts:generate_password"}
)

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


class SchoolStatusMiddleware:
    """What a school's billing status does to the people who work there.

    * **suspended, cancelled** -- locked out. Every screen answers with a notice
      saying so and how to reach support.
    * **past due** -- read-only. They can read and export their records, which are
      theirs, and change nothing but their own account.
    * **active, trialing** -- nothing.

    Nothing is deleted, and changing the status back restores everything. Platform
    accounts are exempt: they have no school of their own, and they are who has to
    get in to sort it out. Signed-out visitors pass through, so signing in still
    works and leads to the notice rather than to an unexplained failure.

    A middleware for the same reason module access is: a screen added next year is
    covered before anybody remembers it needs to be.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        refusal = self._refuse(request)
        return refusal if refusal is not None else self.get_response(request)

    def _refuse(self, request):
        from apps.schools.models import SchoolStatus

        from .branding import SUPPORT_EMAIL
        from .roles import is_platform_role

        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated or user.is_superuser:
            return None
        if is_platform_role(user.role) or user.school_id is None:
            return None

        school = user.school
        if school.status in (SchoolStatus.SUSPENDED, SchoolStatus.CANCELLED):
            if self._url_name(request) in _OPEN_WHEN_LOCKED:
                return None
        elif school.status == SchoolStatus.PAST_DUE:
            if (
                request.method in _SAFE_METHODS
                or self._url_name(request) in _WRITABLE_WHEN_READ_ONLY
            ):
                return None
        else:
            return None

        return render(
            request,
            "403_school_status.html",
            {
                "school": school,
                "read_only": school.status == SchoolStatus.PAST_DUE,
                "support_email": SUPPORT_EMAIL,
                "page_title": f"{school.name} is not available",
                # A guided tour of screens they cannot open would only confuse.
                "tour_config": {
                    "role": user.role or "", "userId": user.pk, "autoStart": False,
                },
            },
            status=403,
        )

    @staticmethod
    def _url_name(request):
        try:
            return resolve(request.path_info).view_name
        except Resolver404:
            return None


class ModuleAccessMiddleware:
    """Refuse a URL that belongs to a module this school does not have.

    A middleware rather than a view mixin, and deliberately. The requirement is
    that a switched-off module is *never* reachable by typing its URL, and a mixin
    delivers that only for as long as everybody remembers to add it -- the next
    screen under ``/admissions/`` would be a hole nobody noticed. Matching on the
    URL prefix instead means a module's routes are covered before they are written.

    What it does *not* gate:

    * anything no module claims -- the dashboards, the roster, account settings;
    * an always-on module, which cannot be switched off, so there is nothing to
      check and no query worth making;
    * an unauthenticated request, because every prefix a module owns is behind a
      login and ``LoginRequiredMixin`` gives a better answer than this would;
    * a school's *public* enquiry page, which is not under a module prefix. It
      belongs to the school in the URL rather than to the reader, so it checks for
      itself -- see ``apps.admissions.views.PublicEnquiryView``.

    Sits after ``RequirePasswordChangeMiddleware`` so an account on a temporary
    password is sent to choose one before being told a module is unavailable: the
    password is the more fundamental gate, and answering with the module first
    would be a dead end for somebody who cannot use any screen yet.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        refusal = self._refuse(request)
        return refusal if refusal is not None else self.get_response(request)

    def _refuse(self, request):
        from .modules import enabled_for_user, module_for_path

        module = module_for_path(request.path)
        if module is None or module.always_on:
            return None

        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return None
        if module.key in enabled_for_user(user):
            return None
        return module_unavailable(request, module)
