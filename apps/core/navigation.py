"""Server-side navigation -- the equivalent of a client ``navFor(role)``.

Navigation is derived from the permission role, not from the job title, and is
resolved on the server so a browser can never reveal a link the account is not
allowed to follow.  Destinations that do not exist yet resolve to a disabled
entry, which keeps the shell honest while feature screens are still being
built.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field

from django.urls import NoReverseMatch, reverse

from .roles import Role
from .tenancy import TenantContext

#: Sentinel url_name: "wherever this role's dashboard is". Resolved per role in
#: :func:`_resolve`, so the sidebar's Dashboard entry points a bursar at the
#: bursar dashboard and a principal at their branch's.
HOME = "__role_home__"

#: The one place that answers "where does this role live?". Both the sidebar
#: entry above and the post-login redirect read it, so the link in the shell and
#: the page you land on after signing in can never drift apart.
ROLE_HOME: dict[str, str] = {
    Role.PLATFORM_OWNER: "core:platform_overview",
    Role.SCHOOL_OWNER: "core:school_dashboard",
    Role.PRINCIPAL: "core:branch_dashboard",
    Role.BURSAR: "core:bursar_dashboard",
}

#: An account whose role we do not recognise gets the narrowest dashboard, the
#: same way scope_for() gives it the narrowest visibility.
DEFAULT_HOME = "core:branch_dashboard"


def home_url_name(role: str | None) -> str:
    """The URL name of ``role``'s dashboard."""
    return ROLE_HOME.get(role, DEFAULT_HOME)


def home_url_for(user) -> str:
    """Where ``user`` should land after signing in.

    Goes through :class:`TenantContext` rather than reading ``user.role``
    directly, so a Django superuser -- who is promoted to platform scope for
    every other purpose -- is sent to the platform overview rather than to
    whatever role happens to be stored on their row.
    """
    return reverse(home_url_name(TenantContext.from_user(user).role))

#: Heroicons-style outline paths, drawn on a 24x24 viewbox.
ICONS: dict[str, list[str]] = {
    "home": [
        "m2.25 12 8.954-8.955c.44-.439 1.152-.439 1.591 0L21.75 12M4.5 9.75v10.125"
        "c0 .621.504 1.125 1.125 1.125H9.75v-4.875c0-.621.504-1.125 1.125-1.125h2.25"
        "c.621 0 1.125.504 1.125 1.125V21h4.125c.621 0 1.125-.504 1.125-1.125V9.75"
        "M8.25 21h8.25",
    ],
    "building": [
        "M3.75 21h16.5M4.5 3h15M5.25 3v18m13.5-18v18M9 6.75h1.5m-1.5 3h1.5m-1.5 3h1.5"
        "m3-6H15m-1.5 3H15m-1.5 3H15M9 21v-3.375c0-.621.504-1.125 1.125-1.125h3.75"
        "c.621 0 1.125.504 1.125 1.125V21",
    ],
    "branches": [
        "M6.429 9.75 2.25 12l4.179 2.25m0-4.5 5.571 3 5.571-3m-11.142 0L2.25 7.5"
        "L12 2.25l9.75 5.25-4.179 2.25m0 0L21.75 12l-4.179 2.25m0 0 4.179 2.25L12 21.75"
        "L2.25 16.5l4.179-2.25m11.142 0-5.571 3-5.571-3",
    ],
    "users": [
        "M15 19.128a9.38 9.38 0 0 0 2.625.372 9.337 9.337 0 0 0 4.121-.952 4.125 4.125"
        " 0 0 0-7.533-2.493M15 19.128v-.003c0-1.113-.285-2.16-.786-3.07M15 19.128v.106"
        "A12.318 12.318 0 0 1 8.624 21c-2.331 0-4.512-.645-6.374-1.766l-.001-.109"
        "a6.375 6.375 0 0 1 11.964-3.07M12 6.375a3.375 3.375 0 1 1-6.75 0 3.375 3.375"
        " 0 0 1 6.75 0Zm8.25 2.25a2.625 2.625 0 1 1-5.25 0 2.625 2.625 0 0 1 5.25 0Z",
    ],
    "students": [
        "M4.26 10.147a60.438 60.438 0 0 0-.491 6.347A48.62 48.62 0 0 1 12 20.904"
        "a48.62 48.62 0 0 1 8.232-4.41 60.46 60.46 0 0 0-.491-6.347m-15.482 0"
        "a50.636 50.636 0 0 0-2.658-.813A59.906 59.906 0 0 1 12 3.493a59.903 59.903"
        " 0 0 1 10.399 5.84c-.896.248-1.783.52-2.658.814m-15.482 0A50.717 50.717 0 0 1"
        " 12 13.489a50.702 50.702 0 0 1 7.74-3.342M6.75 15a.75.75 0 1 0 0-1.5.75.75"
        " 0 0 0 0 1.5Zm0 0v-3.675A55.378 55.378 0 0 1 12 8.443m-7.007 11.55A5.981"
        " 5.981 0 0 0 6.75 15.75v-1.5",
    ],
    "money": [
        "M2.25 8.25h19.5M2.25 9h19.5m-16.5 5.25h6m-6 2.25h3m-3.75 3h15a2.25 2.25 0 0 0"
        " 2.25-2.25V6.75A2.25 2.25 0 0 0 19.5 4.5h-15a2.25 2.25 0 0 0-2.25 2.25v10.5"
        "A2.25 2.25 0 0 0 4.5 19.5Z",
    ],
    "grid": [
        "M3.75 6A2.25 2.25 0 0 1 6 3.75h2.25A2.25 2.25 0 0 1 10.5 6v2.25a2.25 2.25 0 0 1"
        "-2.25 2.25H6a2.25 2.25 0 0 1-2.25-2.25V6ZM3.75 15.75A2.25 2.25 0 0 1 6 13.5h2.25"
        "a2.25 2.25 0 0 1 2.25 2.25V18a2.25 2.25 0 0 1-2.25 2.25H6A2.25 2.25 0 0 1 3.75 18"
        "v-2.25ZM13.5 6a2.25 2.25 0 0 1 2.25-2.25H18A2.25 2.25 0 0 1 20.25 6v2.25"
        "A2.25 2.25 0 0 1 18 10.5h-2.25a2.25 2.25 0 0 1-2.25-2.25V6ZM13.5 15.75"
        "a2.25 2.25 0 0 1 2.25-2.25H18a2.25 2.25 0 0 1 2.25 2.25V18A2.25 2.25 0 0 1 18 20.25"
        "h-2.25A2.25 2.25 0 0 1 13.5 18v-2.25Z",
    ],
    "calendar": [
        "M6.75 3v2.25M17.25 3v2.25M3 18.75V7.5a2.25 2.25 0 0 1 2.25-2.25h13.5"
        "A2.25 2.25 0 0 1 21 7.5v11.25m-18 0A2.25 2.25 0 0 0 5.25 21h13.5"
        "A2.25 2.25 0 0 0 21 18.75m-18 0v-7.5A2.25 2.25 0 0 1 5.25 9h13.5"
        "A2.25 2.25 0 0 1 21 11.25v7.5",
    ],
    "book": [
        "M12 6.042A8.967 8.967 0 0 0 6 3.75c-1.052 0-2.062.18-3 .512v14.25A8.987 8.987"
        " 0 0 1 6 18c2.305 0 4.408.867 6 2.292m0-14.25a8.966 8.966 0 0 1 6-2.292"
        "c1.052 0 2.062.18 3 .512v14.25A8.987 8.987 0 0 0 18 18a8.967 8.967 0 0 0-6 2.292"
        "m0-14.25v14.25",
    ],
    "chart": [
        "M3 13.125C3 12.504 3.504 12 4.125 12h2.25c.621 0 1.125.504 1.125 1.125v6.75"
        "C7.5 20.496 6.996 21 6.375 21h-2.25A1.125 1.125 0 0 1 3 19.875v-6.75Z"
        "M9.75 8.625c0-.621.504-1.125 1.125-1.125h2.25c.621 0 1.125.504 1.125 1.125"
        "v11.25c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 0 1-1.125-1.125V8.625Z"
        "M16.5 4.125c0-.621.504-1.125 1.125-1.125h2.25C20.496 3 21 3.504 21 4.125v15.75"
        "c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 0 1-1.125-1.125V4.125Z",
    ],
    "chat": [
        "M8.625 12a.375.375 0 1 1-.75 0 .375.375 0 0 1 .75 0Zm0 0H8.25m4.375 0"
        "a.375.375 0 1 1-.75 0 .375.375 0 0 1 .75 0Zm0 0H12m4.125 0a.375.375 0 1 1-.75 0"
        " .375.375 0 0 1 .75 0Zm0 0h-.375M21 12c0 4.556-4.03 8.25-9 8.25a9.764 9.764"
        " 0 0 1-2.555-.337A5.972 5.972 0 0 1 5.41 20.97a5.969 5.969 0 0 1-.474-.065"
        " 4.48 4.48 0 0 0 .978-2.025c.09-.457-.133-.901-.467-1.226C3.93 16.178 3 14.189"
        " 3 12c0-4.556 4.03-8.25 9-8.25s9 3.694 9 8.25Z",
    ],
    "identity": [
        "M17.982 18.725A7.488 7.488 0 0 0 12 15.75a7.488 7.488 0 0 0-5.982 2.975"
        "m11.963 0a9 9 0 1 0-11.963 0m11.963 0A8.966 8.966 0 0 1 12 21a8.966 8.966"
        " 0 0 1-5.982-2.275M15 9.75a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z",
    ],
    "clipboard": [
        "M9 12h3.75M9 15h3.75M9 18h3.75m3 .75H18a2.25 2.25 0 0 0 2.25-2.25V6.108"
        "c0-1.135-.845-2.098-1.976-2.192a48.424 48.424 0 0 0-1.123-.08m-5.801 0"
        "c-.065.21-.1.433-.1.664 0 .414.336.75.75.75h4.5a.75.75 0 0 0 .75-.75"
        " 2.25 2.25 0 0 0-.1-.664m-5.8 0A2.251 2.251 0 0 1 13.5 2.25H15"
        "c1.012 0 1.867.668 2.15 1.586m-5.8 0c-.376.023-.75.05-1.124.08"
        "C9.095 4.01 8.25 4.973 8.25 6.108V8.25m0 0H4.875c-.621 0-1.125.504-1.125 1.125"
        "v11.25c0 .621.504 1.125 1.125 1.125h9.75c.621 0 1.125-.504 1.125-1.125V9.375"
        "c0-.621-.504-1.125-1.125-1.125H8.25ZM6.75 12h.008v.008H6.75V12Zm0 3h.008"
        "v.008H6.75V15Zm0 3h.008v.008H6.75V18Z",
    ],
    "userplus": [
        "M18 7.5v3m0 0v3m0-3h3m-3 0h-3m-2.25-4.125a3.375 3.375 0 1 1-6.75 0"
        " 3.375 3.375 0 0 1 6.75 0ZM3 19.235v-.11a6.375 6.375 0 0 1 12.75 0v.109"
        "A12.318 12.318 0 0 1 9.374 21c-2.331 0-4.512-.645-6.374-1.766Z",
    ],
    "cog": [
        "M9.594 3.94c.09-.542.56-.94 1.11-.94h2.593c.55 0 1.02.398 1.11.94l.213 1.281"
        "c.063.374.313.686.645.87.074.04.147.083.22.127.325.196.72.257 1.075.124l1.217-.456"
        "a1.125 1.125 0 0 1 1.37.49l1.296 2.247a1.125 1.125 0 0 1-.26 1.431l-1.003.827"
        "c-.293.24-.438.613-.43.992a7.723 7.723 0 0 1 0 .255c-.008.378.137.75.43.991"
        "l1.004.827c.424.35.534.955.26 1.43l-1.298 2.247a1.125 1.125 0 0 1-1.369.491"
        "l-1.217-.456c-.355-.133-.75-.072-1.076.124a6.47 6.47 0 0 1-.22.128"
        "c-.331.183-.581.495-.644.869l-.213 1.281c-.09.543-.56.94-1.11.94h-2.594"
        "c-.55 0-1.019-.398-1.11-.94l-.213-1.281c-.062-.374-.312-.686-.644-.87"
        "a6.52 6.52 0 0 1-.22-.127c-.325-.196-.72-.257-1.076-.124l-1.217.456"
        "a1.125 1.125 0 0 1-1.369-.49l-1.297-2.247a1.125 1.125 0 0 1 .26-1.431l1.004-.827"
        "c.292-.24.437-.613.43-.991a6.932 6.932 0 0 1 0-.255c.007-.38-.138-.751-.43-.992"
        "l-1.004-.827a1.125 1.125 0 0 1-.26-1.43l1.297-2.247a1.125 1.125 0 0 1 1.37-.491"
        "l1.216.456c.356.133.751.072 1.076-.124.072-.044.146-.086.22-.128"
        ".332-.183.582-.495.644-.869l.214-1.28Z",
        "M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z",
    ],
}


@dataclass(frozen=True)
class NavItem:
    label: str
    url_name: str
    icon: str
    roles: tuple[str, ...]
    #: Optional extra gate. Where a role alone does not settle the question --
    #: admissions, where a school decides whether its bursar sees the pipeline
    #: -- the entry also names the capability the account must hold. Left blank
    #: the entry is role-gated only, which is every other entry above.
    capability: str = ""
    #: The module this entry belongs to, from ``apps.core.modules``. A school
    #: without that module never receives the entry, whatever its staff hold --
    #: a different question from the capability above, and answered by the
    #: school's plan rather than by the account. Left blank, the entry is part of
    #: the product and always offered.
    module: str = ""
    #: The name a guided-tour stop finds this row by, rendered as
    #: ``data-tour="..."``. Declared here rather than as a class in the template
    #: because it is a contract with static/js/tour.js, not a styling decision --
    #: and because a row a role does not get then simply has no marker, which is
    #: how the tour drops a stop instead of pointing at nothing.
    tour: str = ""


@dataclass(frozen=True)
class NavSection:
    label: str
    items: tuple[NavItem, ...]


@dataclass(frozen=True)
class ResolvedItem:
    """A nav entry with its href already worked out for the template."""

    label: str
    href: str
    icon_paths: list[str] = field(default_factory=list)
    available: bool = True
    active: bool = False
    #: Carried through from :class:`NavItem` -- see the note there.
    tour: str = ""


@dataclass(frozen=True)
class ResolvedSection:
    label: str
    items: list[ResolvedItem]


ALL_ROLES = tuple(Role.values)
_LEADERSHIP = (Role.PLATFORM_OWNER, Role.SCHOOL_OWNER, Role.PRINCIPAL)

#: Everyone who works *at a school*, as opposed to on the platform. Used by the
#: sections that are a school's own day-to-day work -- admissions and writing to
#: parents -- which a platform owner has no part in and does not need in their
#: sidebar. It scopes the *menu*, not the permission: the routes stay reachable so
#: the platform can still support a school through them.
_SCHOOL_SIDE = (Role.SCHOOL_OWNER, Role.PRINCIPAL, Role.BURSAR)
_SCHOOL_LEADERSHIP = (Role.SCHOOL_OWNER, Role.PRINCIPAL)

#: The whole navigation map, declared once.  Feature destinations are listed
#: now and light up on their own as the URL names come into existence.
NAVIGATION: tuple[NavSection, ...] = (
    NavSection(
        label="Overview",
        items=(
            # Resolves per role -- see HOME and ROLE_HOME above. Every role,
            # the platform owner included: their dashboard is /platform/ and the
            # schools grid is /platform/schools/, two screens with a URL each.
            #
            # They briefly shared one, which is what made both rows light up --
            # `_keep_only_the_closest_match` compares href *lengths*, so a tie
            # cleared neither. The split is the fix; the tie-break is the guard
            # against a future pair doing it again.
            NavItem("Dashboard", HOME, "home", ALL_ROLES, tour="nav-home"),
            NavItem("Reports", "reports:index", "chart", _LEADERSHIP + (Role.BURSAR,),
                    tour="nav-reports"),
        ),
    ),
    NavSection(
        label="Platform",
        items=(
            # A real screen rather than the admin changelist: this is where the
            # platform owner opens a school and switches its modules, and the
            # admin is neither tenant-aware nor the place to make a commercial
            # decision from.
            NavItem("Schools", "core:platform_schools", "building",
                    (Role.PLATFORM_OWNER,), tour="nav-schools"),
            # Platform work that happens to live under /messaging/: the platform
            # registers a school's Sender ID with the gateway and approves it, and
            # a school can only read its own. So it sits here rather than in
            # Communication, which for a platform owner holds nothing else --
            # they do not write a school's messages to its parents.
            #
            # The screen itself already answers both questions: a school sees its
            # own identity, the platform sees the roll of every school's. See
            # apps/messaging/views.py::MessagingIdentityView.
            NavItem("Sender IDs", "messaging:identity", "identity",
                    (Role.PLATFORM_OWNER,)),
            NavItem("Plans & billing", "core:platform_billing", "money", (Role.PLATFORM_OWNER,)),
        ),
    ),
    NavSection(
        label="School",
        items=(
            # Real screens rather than admin links: the proprietor's own
            # account is not `is_staff`, so an admin URL was a locked door for
            # exactly the role that needs these most -- and the admin is not
            # tenant-scoped, so letting them in would have been worse.
            NavItem("Branches", "schools:branch_list", "branches", _LEADERSHIP,
                    capability="view_branches"),
            # Two entries, one screen. A school sees "Staff" -- the people who
            # work there. The platform sees "Users", because the same list at
            # platform scope is every account on the product, across every
            # school, and calling that a school's staff would be wrong. The
            # screen itself reads the caller's scope and shows a School column
            # and the deactivate control only where they apply.
            NavItem("Staff", "staff:list", "users",
                    (Role.SCHOOL_OWNER, Role.PRINCIPAL), capability="view_staff"),
            NavItem("Users", "staff:list", "users", (Role.PLATFORM_OWNER,),
                    capability="view_staff"),
            # A bursar reads the roster to record payments against it;
            # owner/principal level enrols and edits (Capability.MANAGE_STUDENTS),
            # so every role gets the link.
            NavItem("Students", "students:student_list", "students", ALL_ROLES,
                    tour="nav-students"),
            NavItem("Families", "families:list", "users", _SCHOOL_SIDE,
                    capability="view_families", module="families"),
            NavItem("Import records", "onboarding:index", "clipboard", _SCHOOL_SIDE,
                    capability="import_records", module="onboarding"),
        ),
    ),
    NavSection(
        label="Academics",
        items=(
            # Every role may read the academic setup; only owner/principal
            # level roles get the edit controls (see Capability.MANAGE_ACADEMICS).
            NavItem("Classes", "academics:class_list", "grid", ALL_ROLES),
            NavItem("Subjects", "academics:subject_list", "book", ALL_ROLES),
        ),
    ),
    NavSection(
        label="Finance",
        items=(
            # A bursar reads the fee structure; owner/principal level sets it
            # (see Capability.MANAGE_FEES), so every role gets the link.
            NavItem("Fee Structures", "fees:structure_list", "money", ALL_ROLES,
                    tour="nav-fees"),
            NavItem("Terms", "fees:term_list", "calendar", _LEADERSHIP),
            # A bursar records and confirms; owner and principal level also
            # view and void (see Capability.VOID_PAYMENTS), so every role gets
            # the link.
            NavItem("Payments", "payments:index", "money", ALL_ROLES,
                    tour="nav-payments"),
            NavItem("Family payments", "payments:shared_list", "money", ALL_ROLES,
                    capability="view_payments", module="families"),
            NavItem("Outstanding", "payments:outstanding", "chart", ALL_ROLES),
        ),
    ),
    NavSection(
        label="Admissions",
        items=(
            # Module-gated first: a school without the admissions add-on never
            # receives this section at all. Capability-gated on top of that,
            # because even at a school that has it, a bursar reaches the pipeline
            # only where the school has switched *them* on -- and the sidebar has
            # to agree with what the view will actually allow.
            # School-side roles only. Admissions is a school's own pipeline --
            # enquiries, assessments, offers, enrolment -- and none of it is the
            # platform's work, so the whole section is absent from their sidebar.
            # The routes stay reachable for support; it is the menu that is
            # scoped, not the permission.
            NavItem("Applications", "admissions:pipeline", "clipboard", _SCHOOL_SIDE,
                    capability="view_admissions", module="admissions"),
            NavItem("New enquiry", "admissions:enquiry_create", "userplus",
                    _SCHOOL_LEADERSHIP, capability="manage_admissions",
                    module="admissions"),
            NavItem("Admission fees", "admissions:fee_schedules", "money",
                    _SCHOOL_SIDE, capability="view_admission_payments",
                    module="admissions"),
            NavItem("Requirements", "admissions:requirements", "clipboard",
                    _SCHOOL_SIDE, capability="view_admissions", module="admissions"),
            NavItem("Admissions settings", "admissions:settings", "cog",
                    _SCHOOL_LEADERSHIP, capability="manage_admissions",
                    module="admissions"),
        ),
    ),
    NavSection(
        label="Communication",
        items=(
            # A school writing to its own parents. Not the platform's, which is
            # why this section is empty for them -- their half of messaging is
            # Sender IDs, up in Platform.
            NavItem("Messaging", "messaging:index", "chat", _SCHOOL_SIDE,
                    module="messaging"),
            # What parents see the school's messages come from. Read-only here:
            # the platform registers and approves it, from its own entry above.
            NavItem("Sender ID", "messaging:identity", "identity",
                    _SCHOOL_LEADERSHIP, module="messaging"),
        ),
    ),
    NavSection(
        label="Settings",
        items=(
            # School-side roles only. A platform owner has no school of their
            # own, so /settings/ raised a 404 for exactly the account whose
            # sidebar offered it -- they reach each school's profile from
            # Platform > Schools, where it belongs alongside that school's plan
            # and modules.
            NavItem("School settings", "core:school_settings", "cog",
                    (Role.SCHOOL_OWNER, Role.PRINCIPAL)),
            # Everyone's own email and password. Every role, because every
            # account has both.
            NavItem("Account settings", "accounts:settings", "identity", ALL_ROLES),
        ),
    ),
)


def _resolve(item: NavItem, current_path: str, role: str | None = None) -> ResolvedItem:
    url_name = home_url_name(role) if item.url_name == HOME else item.url_name
    try:
        href = reverse(url_name)
        available = True
    except NoReverseMatch:
        # Destination not built yet -- render it greyed out rather than 404ing.
        href = "#"
        available = False
    active = available and href != "/" and current_path.startswith(href)
    if available and href == "/":
        active = current_path == "/"
    return ResolvedItem(
        label=item.label,
        href=href,
        icon_paths=ICONS.get(item.icon, []),
        available=available,
        active=active,
        tour=item.tour,
    )


def nav_for(
    role: str | None,
    current_path: str = "",
    capabilities: frozenset[str] | set[str] = frozenset(),
    modules: frozenset[str] | set[str] = frozenset(),
) -> list[ResolvedSection]:
    """Return the navigation tree a holder of ``role`` should see.

    Sections with no visible items are dropped, so a bursar simply never receives
    a "Platform" heading, and a school without the admissions add-on never
    receives an "Admissions" one.

    Three gates, and an entry has to pass all three:

    * ``role`` -- who is asking;
    * ``capabilities`` -- the capability *strings* the account holds. Only entries
      that declare one consult it;
    * ``modules`` -- the module keys the account's school has. Only entries that
      declare one consult it.

    Both sets default to empty, so a caller that does not pass them gets exactly
    the role-only navigation with every gated entry hidden. That is the safe
    direction for a default to fail in: a menu offering too little is a bug
    somebody reports, and one offering too much is a door the middleware then has
    to slam in their face.
    """
    if not role:
        return []
    sections: list[ResolvedSection] = []
    for section in NAVIGATION:
        items = [
            _resolve(i, current_path, role)
            for i in section.items
            if role in i.roles
            and (not i.capability or i.capability in capabilities)
            and (not i.module or i.module in modules)
        ]
        if items:
            sections.append(ResolvedSection(label=section.label, items=items))
    _keep_only_the_closest_match(sections)
    return sections


#: What goes in the bar at the bottom of a phone screen, per role, in order. The
#: screens that role opens every day; everything else is under More.
#:
#: Whoever records payments gets Record a payment as a lime "+" in the middle of the
#: bar. Reports and Outstanding stay under More and in the sidebar.
BOTTOM_NAV: dict[str, tuple[str, ...]] = {
    Role.PLATFORM_OWNER: (
        HOME, "core:platform_schools", "staff:list", "students:student_list",
    ),
    Role.SCHOOL_OWNER: (HOME, "students:student_list", "payments:index"),
    Role.PRINCIPAL: (HOME, "students:student_list", "payments:index"),
    Role.BURSAR: (HOME, "students:student_list", "payments:index"),
}

#: Roles whose bar carries the "+" for Record a payment.
BOTTOM_RECORD_ROLES = (Role.SCHOOL_OWNER, Role.PRINCIPAL, Role.BURSAR)


def bottom_nav_for(role: str | None, sections: list[ResolvedSection]) -> list[ResolvedItem]:
    """The phone bar's entries, taken from what :func:`nav_for` already allowed.

    Looked up in the resolved menu rather than resolved again, so a shortcut passes
    the same role, capability and module gates and keeps the sidebar's highlighting.
    An entry this account did not get is simply left out.
    """
    by_href = {item.href: item for section in sections for item in section.items}
    found = []
    for url_name in BOTTOM_NAV.get(role, ()):
        name = home_url_name(role) if url_name == HOME else url_name
        try:
            item = by_href.get(reverse(name))
        except NoReverseMatch:
            item = None
        if item is not None:
            found.append(item)
    return found


def _keep_only_the_closest_match(sections: list[ResolvedSection]) -> None:
    """Leave exactly one row highlighted: the most specific one.

    ``_resolve`` marks an item active when the current path *starts with* its
    href, which is right for "/students/41/" lighting up Students, and wrong
    whenever one entry's href is a prefix of another's. Four pairs in this menu
    are exactly that -- ``/fees/`` and ``/fees/terms/``, ``/payments/`` and
    ``/payments/outstanding/``, ``/admissions/`` and ``/admissions/new/``,
    ``/messaging/`` and ``/messaging/identity/`` -- so opening Terms lit up
    Fee Structures as well and read as the previous row having failed to clear.

    Resolved here rather than in ``_resolve`` because it is not a question one
    item can answer: which match is closest is only knowable once every
    candidate has been seen. The longest href wins, since a longer prefix of
    the same path is by definition the deeper page.
    """
    active = [item for s in sections for item in s.items if item.active]
    if len(active) < 2:
        return
    deepest = max(len(item.href) for item in active)

    # ResolvedItem is frozen, so the losers are replaced rather than edited.
    # The section's list is mutable even though its items are not, which is
    # what lets this happen in place.
    #
    # `kept` breaks a tie. Two entries pointing at the *same* href are equally
    # deep, so the length test alone clears neither and both rows stay lit -- the
    # bug the platform owner met when their Dashboard and their Schools entry both
    # resolved to /platform/. The first one declared wins, which makes the outcome
    # a property of the menu's order rather than of dictionary iteration.
    kept = False
    for section in sections:
        for index, item in enumerate(section.items):
            if not item.active:
                continue
            if len(item.href) < deepest or kept:
                section.items[index] = dataclasses.replace(item, active=False)
            else:
                kept = True
