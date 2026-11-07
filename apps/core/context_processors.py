"""Template context shared by every page in the shell."""

from __future__ import annotations

from django.apps import apps as django_apps
from django.urls import reverse

from .modules import enabled_for_user
from .navigation import BOTTOM_RECORD_ROLES, bottom_nav_for, nav_for
from .permissions import capabilities_of
from .roles import Role, scope_for


def _capability_strings(user) -> set[str]:
    """Every capability this account holds, as plain strings.

    Mostly the static role table, minus anything belonging to a module the school
    does not have -- ``capabilities_of`` takes care of that, so the sidebar and
    the views close together rather than one of them offering what the other
    refuses.

    Admissions adds the one grant that is not a property of the role at all --
    whether a bursar may see the applicant pipeline is the *school's* decision --
    so it is asked for its answer rather than having its rule restated here.
    Resolved through the app registry, the same guard the bursar dashboard uses
    for payments: the shell has to render whether or not the add-on is installed.
    """
    granted = {c.value for c in capabilities_of(user)}
    if django_apps.is_installed("apps.admissions"):
        from apps.admissions.access import admissions_capabilities

        granted |= admissions_capabilities(user)
    return granted


def tenancy(request):
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {
            "nav_sections": [],
            "bottom_record_href": "",
            "tenant": None,
            "capabilities": frozenset(),
            "enabled_modules": frozenset(),
        }

    context = getattr(request, "tenant", None)
    # The effective role, which promotes Django superusers to platform scope.
    role = context.role if context is not None else getattr(user, "role", None)
    capabilities = _capability_strings(user)
    # Which features this school has. Memoised on the user for the life of the
    # request, so the sidebar, the capability set above and the middleware that
    # guards the URLs all share one query.
    modules = enabled_for_user(user)
    # Capability- and module-gated nav entries need the same answers the views and
    # the middleware will give, or the sidebar offers a link that 403s. See
    # navigation.nav_for.
    sections = nav_for(role, request.path, capabilities, modules)
    return {
        "nav_sections": sections,
        "bottom_nav": bottom_nav_for(role, sections),
        # The lime "+" in the phone bar. Empty for every other role, so the template
        # draws nothing for them.
        "bottom_record_href": (
            reverse("payments:record")
            if role in BOTTOM_RECORD_ROLES and "record_payments" in capabilities
            else ""
        ),
        "tenant": context,
        "current_school": getattr(user, "school", None),
        "current_branch": getattr(user, "branch", None),
        "current_role_label": Role(role).label if role in Role.values else "",
        "current_scope": scope_for(role).value,
        # Plain strings so templates can write {% if "manage_academics" in capabilities %}
        "capabilities": capabilities,
        # Module keys, for the handful of places a template asks about a feature
        # rather than about an action -- {% if "messaging" in enabled_modules %}.
        "enabled_modules": modules,
        # What the guided tour needs, as one dict for json_script. The role
        # decides which stops it makes; the id keys the "already seen it" flag so
        # two people sharing an office computer each get their own first run.
        #
        # Auto-start is held back until the account is past its temporary
        # password -- that screen holds them there until they choose one, and a
        # tour of a sidebar they cannot use yet would be a tour of a locked door.
        "tour_config": {
            "role": role or "",
            "userId": user.pk,
            "autoStart": not getattr(user, "must_change_password", False),
        },
    }
