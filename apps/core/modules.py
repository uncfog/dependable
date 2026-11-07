"""Which features a school has, as distinct from what its staff may do with them.

Three questions now decide whether an account can reach a screen, and they are
deliberately separate:

* **Tenancy** (``apps.core.tenancy``) -- *which rows?*
* **Capabilities** (``apps.core.permissions``) -- *which actions?*
* **Modules** (this file) -- *which features does this school have at all?*

A module is a commercial fact about a school, not a permission. Admissions is a
paid add-on; parent messaging costs the platform money per message. A school that
has not taken them should not see them, and its principal should not be able to
reach them by typing a URL -- but nothing about that is a statement about the
principal, which is why it cannot live in the capability table.

**The registry below is the only place a module is declared.** Adding one means
adding an entry here: its URL prefixes, the capabilities it owns, its default, and
whether it can be switched off at all. Nothing else in the codebase should grow a
list of module names.

**Nothing is deleted when a module is switched off.** The rows stay, the code stays,
the URLs stay routed; the module is simply not available to that school, and
switching it back on the same afternoon restores every screen exactly as it was.
That is the whole point of a toggle rather than an uninstall.

**Enforcement is in three places, each because it knows something the others do
not.**

1. :class:`apps.core.middleware.ModuleAccessMiddleware` gates the URL prefixes for
   the caller's own school. It is a middleware rather than a mixin so a view added
   later is covered without anybody remembering to cover it.
2. :func:`apps.core.permissions.capabilities_of` withdraws the module's
   capabilities, which closes the buttons and links that other apps' screens draw
   -- the fee-reminder button on the bursar's dashboard is a messaging action
   sitting on a payments page.
3. :func:`apps.core.navigation.nav_for` drops the module's nav entries, so the
   sidebar never offers a door that the middleware would shut.

The one route none of those can gate is the school's *public* enquiry page, at
``/<school-slug>/enquiry/``. It belongs to the school named in the URL rather than
to the caller -- there usually is no caller -- so it checks for itself, in
``apps.admissions.views.PublicEnquiryView``.
"""

from __future__ import annotations

from dataclasses import dataclass

from .roles import is_platform_role


@dataclass(frozen=True)
class Module:
    """One switchable feature of the product."""

    key: str
    label: str
    #: One sentence, shown beside the switch on the platform owner's screen. It
    #: is read by somebody deciding whether a school should be paying for this.
    description: str
    #: URL prefixes this module owns, each with its trailing slash. Matched as
    #: prefixes, so ``/messaging/`` covers every screen beneath it including ones
    #: that do not exist yet.
    paths: tuple[str, ...] = ()
    #: Capability *strings* this module owns. Withdrawn while it is off, which is
    #: what closes the links other apps' screens draw into this one.
    capabilities: tuple[str, ...] = ()
    #: Part of what the product *is*. Cannot be switched off, and is not offered
    #: as a switch -- a school with no student records is not a school.
    always_on: bool = False
    #: What a school gets when nobody has decided. Only consulted for a module
    #: with no row of its own.
    default_on: bool = True

    @property
    def is_toggleable(self) -> bool:
        return not self.always_on


#: The registry. Order is the order the switches appear in.
MODULES: tuple[Module, ...] = (
    Module(
        key="academics",
        label="Classes and subjects",
        description="The academic setup every other feature is priced and "
                    "reported against.",
        paths=("/academics/",),
        always_on=True,
    ),
    Module(
        key="students",
        label="Student records",
        description="The roster: enrolment by hand or from a spreadsheet, and "
                    "each child's fee position.",
        paths=("/students/",),
        always_on=True,
    ),
    Module(
        key="fees",
        label="Fees, payments and reports",
        description="Terms, what each class owes, money recorded against a "
                    "child, and the collections report.",
        paths=("/fees/", "/payments/", "/reports/"),
        always_on=True,
    ),
    Module(
        key="messaging",
        label="Parent messaging",
        description="SMS and WhatsApp to parents, under the school's own "
                    "registered Sender ID. Costs the platform money per message, "
                    "so it is off until a school is set up to send.",
        paths=("/messaging/",),
        capabilities=(
            "view_messages",
            "send_messages",
            "view_messaging_identity",
            "manage_messaging_identity",
        ),
        default_on=False,
    ),
    Module(
        key="admissions",
        label="Admissions and enquiries",
        description="The enquiry-to-enrolment pipeline, admission fees, the "
                    "requirements policy, and the school's own public enquiry "
                    "page. A paid add-on.",
        paths=("/admissions/",),
        capabilities=(
            "view_admissions",
            "manage_admissions",
            "decide_admissions",
            "view_admission_payments",
            "record_admission_payments",
            "manage_admission_requirements",
        ),
        default_on=False,
    ),
)

MODULES += (
    Module(
        key="families",
        label="Families",
        description="Group children into households, so one payment can be shared "
                    "between siblings. Each child keeps their own record and balance. "
                    "Off until a school is ready to use it.",
        paths=("/families/", "/payments/family/"),
        capabilities=("view_families", "manage_families"),
        default_on=False,
    ),
)

BY_KEY: dict[str, Module] = {module.key: module for module in MODULES}
ALL_KEYS: frozenset[str] = frozenset(BY_KEY)
#: The ones the platform owner is offered a switch for.
TOGGLEABLE: tuple[Module, ...] = tuple(m for m in MODULES if m.is_toggleable)
ALWAYS_ON: frozenset[str] = frozenset(m.key for m in MODULES if m.always_on)


def default_keys() -> frozenset[str]:
    """What a school with no decisions recorded against it has."""
    return frozenset(m.key for m in MODULES if m.always_on or m.default_on)


def module_for_path(path: str) -> Module | None:
    """The module that owns ``path``, or ``None`` if no module claims it.

    The longest matching prefix wins, so a module mounted beneath another's
    prefix would still be resolved to itself. Nothing is nested today; the rule
    is here so that adding one is not a silent mis-gate.
    """
    best: Module | None = None
    best_length = 0
    for module in MODULES:
        for prefix in module.paths:
            if path.startswith(prefix) and len(prefix) > best_length:
                best, best_length = module, len(prefix)
    return best


def enabled_for_school(school_id) -> frozenset[str]:
    """The modules school ``school_id`` has.

    Absent rows mean "nobody has decided", which is the registry default -- so a
    school that signs up today gets the defaults without anything being written
    for it, and changing a default later moves every school that never chose.

    Read through ``all_objects`` deliberately. The caller has already named the
    school it is asking about -- and for the public enquiry page that school is
    the one in the URL rather than the reader's own, so a scoped manager would
    return nothing and read as "every module off". Nothing sensitive is exposed:
    the answer is which features a school has, keyed by an id the caller supplied.
    """
    keys = set(default_keys())
    if school_id is None:
        return frozenset(keys)

    from apps.schools.models import SchoolModule

    for key, enabled in SchoolModule.all_objects.filter(
        school_id=school_id
    ).values_list("key", "enabled"):
        if key not in BY_KEY or key in ALWAYS_ON:
            # A key nobody recognises is a module that has been renamed or
            # removed. Ignored rather than trusted: the registry is the authority
            # on what exists, and an always-on module cannot be switched off by a
            # stale row either.
            continue
        if enabled:
            keys.add(key)
        else:
            keys.discard(key)
    return frozenset(keys)


def enabled_for_user(user) -> frozenset[str]:
    """The modules available to whoever is asking.

    Platform staff get everything: they are not a school, and the screens they use
    to switch a school's modules must not be switchable off underneath them.

    Memoised on the user instance, which lives exactly as long as the request, so
    the sidebar, the capability check and the middleware share one query --
    the same economy ``apps.admissions.access.config_for`` makes.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return ALL_KEYS
    if getattr(user, "is_superuser", False) or is_platform_role(
        getattr(user, "role", None)
    ):
        return ALL_KEYS

    cached = getattr(user, "_enabled_modules", None)
    if cached is None:
        cached = enabled_for_school(getattr(user, "school_id", None))
        user._enabled_modules = cached
    return cached


def withheld_capabilities(enabled: frozenset[str]) -> frozenset[str]:
    """Capability strings owned by modules that are switched off."""
    return frozenset(
        capability
        for module in MODULES
        if module.key not in enabled
        for capability in module.capabilities
    )


def state_for_school(school_id) -> list[dict]:
    """Every toggleable module and whether this school has it.

    The shape the platform owner's switches render from: one row per module in
    registry order, each knowing whether it is on and whether that is a decision
    somebody made or just the default.
    """
    from apps.schools.models import SchoolModule

    decided = dict(
        SchoolModule.all_objects.filter(school_id=school_id).values_list(
            "key", "enabled"
        )
    )
    enabled = enabled_for_school(school_id)
    return [
        {
            "module": module,
            "enabled": module.key in enabled,
            "is_default": module.key not in decided,
        }
        for module in TOGGLEABLE
    ]
