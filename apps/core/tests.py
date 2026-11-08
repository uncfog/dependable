"""Tests for the row-level tenancy foundation.

These cover the one thing that must never regress: a query made while a tenant
is active can only see that tenant's rows.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.db import connection, models
from django.db.models.signals import post_migrate
from django.test import TestCase

from apps.core.models import TenantScopedModel
from apps.core.roles import Role, Scope, scope_for
from apps.core.navigation import nav_for
from apps.core.tenancy import get_current_context, scope_to, unscoped
from apps.schools.models import Branch, School

User = get_user_model()


class ScopedThing(TenantScopedModel):
    """A stand-in for a real feature model (student, invoice, message).

    Declared here rather than shipped in ``models.py`` so the scaffold carries
    no junk table and no migration.
    """

    name = models.CharField(max_length=50)

    class Meta(TenantScopedModel.Meta):
        app_label = "core"
        abstract = False


def _create_the_stand_in_table(sender, **kwargs):
    """Give ``ScopedThing`` a table for the whole test run, once.

    It used to be created in ``TenancyTestCase.setUpClass`` and dropped in
    ``tearDownClass``, which was fine for the tests in this file and a landmine
    for everyone else: defining the model registers a ``school`` foreign key, so
    *any* ``School`` cascade anywhere in the suite walks into this table, and
    outside this one test case it did not exist. Nothing had ever deleted a
    school, so nothing had found out -- see apps/schools/tests_teardown.py,
    which does.

    Hooked on ``post_migrate``, which the test runner fires when it builds the
    test database, after the test modules have been imported. In production this
    module is never imported, so the receiver is never connected and no junk
    table is created -- which was the point of declaring the model here in the
    first place.
    """
    if sender.label != "core":
        return
    if ScopedThing._meta.db_table in connection.introspection.table_names():
        return
    with connection.schema_editor() as editor:
        editor.create_model(ScopedThing)


post_migrate.connect(_create_the_stand_in_table)


class TenancyTestCase(TestCase):
    """Two schools, three branches, one user per role."""

    @classmethod
    def setUpTestData(cls):
        cls.acme = School.all_objects.create(name="Acme High")
        cls.rival = School.all_objects.create(name="Rival College")

        cls.acme_main = Branch.all_objects.create(school=cls.acme, name="Main")
        cls.acme_annex = Branch.all_objects.create(school=cls.acme, name="Annex")
        cls.rival_main = Branch.all_objects.create(school=cls.rival, name="Main")

        cls.platform = User.objects.create_user(
            "plat", role=Role.PLATFORM_OWNER, school=None
        )
        cls.acme_owner = User.objects.create_user(
            "owner", role=Role.SCHOOL_OWNER, school=cls.acme
        )
        cls.acme_bursar = User.objects.create_user(
            "bursar", role=Role.BURSAR, school=cls.acme, branch=cls.acme_main
        )

        # One row per branch, plus one school-wide row with no branch.
        cls.thing_main = ScopedThing.all_objects.create(
            school=cls.acme, branch=cls.acme_main, name="acme-main"
        )
        cls.thing_annex = ScopedThing.all_objects.create(
            school=cls.acme, branch=cls.acme_annex, name="acme-annex"
        )
        cls.thing_shared = ScopedThing.all_objects.create(
            school=cls.acme, branch=None, name="acme-shared"
        )
        cls.thing_rival = ScopedThing.all_objects.create(
            school=cls.rival, branch=cls.rival_main, name="rival"
        )

    def as_user(self, user):
        return scope_to(
            school_id=user.school_id, branch_id=user.branch_id, role=user.role
        )


class RoleScopeTests(TestCase):
    def test_each_role_maps_to_its_access_level(self):
        self.assertIs(scope_for(Role.PLATFORM_OWNER), Scope.PLATFORM)
        self.assertIs(scope_for(Role.SCHOOL_OWNER), Scope.SCHOOL)
        self.assertIs(scope_for(Role.PRINCIPAL), Scope.BRANCH)
        self.assertIs(scope_for(Role.BURSAR), Scope.BRANCH)

    def test_unknown_role_fails_closed(self):
        self.assertIs(scope_for("not-a-role"), Scope.BRANCH)
        self.assertIs(scope_for(None), Scope.BRANCH)


class NoContextTests(TenancyTestCase):
    def test_queries_outside_a_request_are_not_filtered(self):
        """Shell, migrations and management commands must see everything."""
        self.assertIsNone(get_current_context())
        self.assertEqual(ScopedThing.objects.count(), 4)
        self.assertEqual(School.objects.count(), 2)


class PlatformOwnerTests(TenancyTestCase):
    def test_sees_every_tenant(self):
        with self.as_user(self.platform):
            self.assertEqual(School.objects.count(), 2)
            self.assertEqual(Branch.objects.count(), 3)
            self.assertEqual(ScopedThing.objects.count(), 4)


class SchoolOwnerTests(TenancyTestCase):
    def test_sees_own_school_only(self):
        with self.as_user(self.acme_owner):
            self.assertEqual(list(School.objects.all()), [self.acme])

    def test_sees_every_branch_of_own_school(self):
        with self.as_user(self.acme_owner):
            self.assertCountEqual(
                Branch.objects.all(), [self.acme_main, self.acme_annex]
            )

    def test_never_sees_another_schools_rows(self):
        with self.as_user(self.acme_owner):
            names = set(ScopedThing.objects.values_list("name", flat=True))
        self.assertEqual(names, {"acme-main", "acme-annex", "acme-shared"})
        self.assertNotIn("rival", names)

    def test_get_on_another_tenants_row_raises_does_not_exist(self):
        with self.as_user(self.acme_owner):
            with self.assertRaises(ScopedThing.DoesNotExist):
                ScopedThing.objects.get(pk=self.thing_rival.pk)


class BranchScopedUserTests(TenancyTestCase):
    def test_sees_only_own_branch_plus_school_wide_rows(self):
        with self.as_user(self.acme_bursar):
            names = set(ScopedThing.objects.values_list("name", flat=True))
        # "acme-annex" belongs to a branch this bursar has no business seeing.
        self.assertEqual(names, {"acme-main", "acme-shared"})

    def test_branch_list_is_limited_to_own_branch(self):
        with self.as_user(self.acme_bursar):
            self.assertEqual(list(Branch.objects.all()), [self.acme_main])

    def test_branch_role_with_no_branch_assigned_sees_nothing(self):
        """A misconfigured account fails closed rather than seeing the school."""
        with scope_to(school_id=self.acme.pk, branch_id=None, role=Role.BURSAR):
            self.assertEqual(ScopedThing.objects.count(), 0)


class AnonymousTests(TenancyTestCase):
    def test_unauthenticated_context_sees_nothing(self):
        from apps.core.tenancy import TenantContext, activate, deactivate

        token = activate(TenantContext(is_authenticated=False))
        try:
            self.assertEqual(ScopedThing.objects.count(), 0)
            self.assertEqual(School.objects.count(), 0)
        finally:
            deactivate(token)


class EscapeHatchTests(TenancyTestCase):
    def test_unscoped_block_lifts_filtering(self):
        with self.as_user(self.acme_bursar):
            self.assertEqual(ScopedThing.objects.count(), 2)
            with unscoped():
                self.assertEqual(ScopedThing.objects.count(), 4)
            # ...and filtering is restored on the way out.
            self.assertEqual(ScopedThing.objects.count(), 2)

    def test_all_objects_manager_is_never_filtered(self):
        with self.as_user(self.acme_bursar):
            self.assertEqual(ScopedThing.all_objects.count(), 4)


class AutoStampTests(TenancyTestCase):
    def test_save_fills_school_and_branch_from_the_active_tenant(self):
        with self.as_user(self.acme_bursar):
            thing = ScopedThing.objects.create(name="new")
        thing.refresh_from_db()
        self.assertEqual(thing.school_id, self.acme.pk)
        self.assertEqual(thing.branch_id, self.acme_main.pk)

    def test_explicit_values_are_not_overwritten(self):
        with self.as_user(self.acme_owner):
            thing = ScopedThing.objects.create(name="annex", branch=self.acme_annex)
        self.assertEqual(thing.branch_id, self.acme_annex.pk)


class UserScopingTests(TenancyTestCase):
    def test_default_user_manager_stays_unscoped_for_authentication(self):
        """Auth backends resolve users before any tenant context exists."""
        with self.as_user(self.acme_bursar):
            self.assertTrue(User.objects.filter(username="plat").exists())

    def test_scoped_manager_limits_the_staff_directory(self):
        with self.as_user(self.acme_owner):
            usernames = set(User.scoped.values_list("username", flat=True))
        self.assertEqual(usernames, {"owner", "bursar"})


class NavigationTests(TestCase):
    def test_bursar_gets_no_platform_section(self):
        labels = {s.label for s in nav_for(Role.BURSAR, "/")}
        self.assertNotIn("Platform", labels)
        self.assertIn("Finance", labels)

    def test_platform_owner_gets_the_platform_section(self):
        labels = {s.label for s in nav_for(Role.PLATFORM_OWNER, "/")}
        self.assertIn("Platform", labels)

    def test_unbuilt_destinations_render_as_unavailable(self):
        """The shell stays honest while feature screens are still being built.

        The pair moves as features land. Payments was the unbuilt half of this
        assertion until the payments app shipped; Reports was next, then Plans &
        billing. Every destination in the sidebar exists now, so this checks that
        each of them resolves rather than one that does not.
        """
        sections = {s.label: s.items for s in nav_for(Role.SCHOOL_OWNER, "/")}
        students = next(i for i in sections["School"] if i.label == "Students")
        payments = next(i for i in sections["Finance"] if i.label == "Payments")
        reports = next(i for i in sections["Overview"] if i.label == "Reports")
        self.assertTrue(students.available)   # roster screens exist now
        self.assertTrue(payments.available)   # and payments screens do too
        self.assertTrue(reports.available)    # and the report does as well

        platform = {s.label: s.items for s in nav_for(Role.PLATFORM_OWNER, "/")}
        billing = next(i for i in platform["Platform"] if i.label == "Plans & billing")
        self.assertTrue(billing.available)    # and Plans & billing has shipped

    def test_dashboard_is_marked_active_on_its_own_path(self):
        from django.urls import reverse

        from apps.core.navigation import home_url_name

        # "/" is the public landing page now, so a role's dashboard is wherever
        # ROLE_HOME sends it -- the bursar's is the finance one.
        home = reverse(home_url_name(Role.BURSAR))
        dashboard = nav_for(Role.BURSAR, home)[0].items[0]
        self.assertEqual(dashboard.href, home)
        self.assertTrue(dashboard.active)

    def test_every_role_has_exactly_one_entry_pointing_at_its_own_home(self):
        """The invariant, stated without assuming what the entry is called.

        Three roles reach their home through the Overview "Dashboard" row. The
        platform owner reaches theirs through Platform > "Schools", because for
        them the two are one screen -- and a second row pointing at it was what
        lit two rows at once. So the thing to assert is that each role has one
        entry for its home, not that the entry is labelled Dashboard.
        """
        from django.urls import reverse

        from apps.core.modules import ALL_KEYS
        from apps.core.navigation import home_url_name
        from apps.core.permissions import capabilities_for

        for role in Role.values:
            with self.subTest(role=role):
                home = reverse(home_url_name(role))
                matching = [
                    item.label
                    for section in nav_for(
                        role, "/", {c.value for c in capabilities_for(role)},
                        ALL_KEYS,
                    )
                    for item in section.items
                    if item.href == home
                ]
                self.assertEqual(len(matching), 1, f"{role}: {matching}")

    def test_the_four_homes_are_four_different_screens(self):
        from django.urls import reverse

        from apps.core.navigation import home_url_name

        homes = {role: reverse(home_url_name(role)) for role in Role.values}
        self.assertEqual(len(set(homes.values())), len(homes))
        self.assertEqual(homes[Role.BURSAR], "/finance/")
        self.assertEqual(homes[Role.PLATFORM_OWNER], "/platform/")

    def test_two_entries_on_one_href_never_both_light_up(self):
        """The bug behind "clicking a new item leaves the old one highlighted".

        `_keep_only_the_closest_match` compares href *lengths*, so a tie cleared
        neither -- and the platform owner had a tie, with Dashboard and Schools
        both resolving to /platform/. The duplicate row is gone; this guards the
        tie-break itself, since any future pair would fail the same way.
        """
        import dataclasses

        from apps.core.navigation import (
            ResolvedItem,
            ResolvedSection,
            _keep_only_the_closest_match,
        )

        twins = [
            ResolvedSection(
                label="Overview",
                items=[
                    ResolvedItem(label="First", href="/same/", active=True),
                    ResolvedItem(label="Second", href="/same/", active=True),
                ],
            )
        ]
        _keep_only_the_closest_match(twins)
        active = [i.label for s in twins for i in s.items if i.active]
        self.assertEqual(active, ["First"], "the first declared should win")

    def test_anonymous_gets_no_navigation(self):
        self.assertEqual(nav_for(None), [])

    def test_exactly_one_row_is_ever_highlighted(self):
        """Reported as "the previous item stays highlighted".

        It was not a stale highlight: `_resolve` marks an item active when the
        path *starts with* its href, and four entries in this menu are prefixes
        of another -- /fees/ of /fees/terms/, /payments/ of
        /payments/outstanding/, and so on. Opening Terms lit up Fee Structures
        as well, which reads exactly like a row that failed to clear.
        """
        from apps.core.permissions import capabilities_for

        from apps.core.modules import ALL_KEYS

        capabilities = {c.value for c in capabilities_for(Role.SCHOOL_OWNER)}
        nested = [
            "/fees/terms/",
            "/payments/outstanding/",
            "/admissions/new/",
            "/messaging/identity/",
        ]
        # Every module in hand: this test is about prefix matching, and two of
        # the four paths belong to modules that are off by default.
        for path in nested:
            with self.subTest(path=path):
                active = [
                    item.label
                    for section in nav_for(
                        Role.SCHOOL_OWNER, path, capabilities, ALL_KEYS
                    )
                    for item in section.items
                    if item.active
                ]
                self.assertEqual(len(active), 1, active)

    def test_a_deep_page_still_lights_its_section(self):
        """The prefix match is what makes /students/41/ light Students, so
        narrowing it must not cost that."""
        from apps.core.permissions import capabilities_for

        capabilities = {c.value for c in capabilities_for(Role.SCHOOL_OWNER)}
        for path, expected in (
            ("/students/41/", "Students"),
            ("/academics/classes/add-many/", "Classes"),
        ):
            with self.subTest(path=path):
                active = [
                    item.label
                    for section in nav_for(Role.SCHOOL_OWNER, path, capabilities)
                    for item in section.items
                    if item.active
                ]
                self.assertEqual(active, [expected])


class StatusBreakdownTests(TestCase):
    """The payment-status chart on the dashboards.

    Display-only, but two things about it must not drift: it has to render
    when there is nothing to render, and its four buckets have to keep naming
    the four states ``apps.students.fees`` actually produces. A rename there
    would otherwise leave every dashboard quietly showing an empty chart, with
    nothing failing anywhere.
    """

    def test_no_current_term_gives_four_empty_buckets(self):
        from apps.core.views import status_breakdown

        result = status_breakdown(None)
        self.assertEqual(result["total"], 0)
        self.assertEqual(
            [bucket["key"] for bucket in result["buckets"]],
            ["paid", "partial", "unpaid", "overdue"],
        )
        self.assertTrue(all(bucket["count"] == 0 for bucket in result["buckets"]))

    def test_the_bucket_keys_are_the_states_the_fee_layer_produces(self):
        """The chart and the pill beside a child's name read the same values."""
        from apps.students.fees import FeeState
        from apps.core.views import status_breakdown

        keys = {bucket["key"] for bucket in status_breakdown(None)["buckets"]}
        self.assertEqual(
            keys,
            {
                FeeState.PAID,
                FeeState.PARTIAL,
                FeeState.UNPAID,
                FeeState.OVERDUE,
            },
        )
        # UNPRICED is deliberately absent: it is a setup gap, not a payment
        # state, and counting it would invent a debt nobody owes.
        self.assertNotIn(FeeState.UNPRICED, keys)

    def test_the_order_is_fixed_so_a_state_keeps_its_colour(self):
        from apps.core.views import status_breakdown

        labels = [bucket["label"] for bucket in status_breakdown(None)["buckets"]]
        self.assertEqual(labels, ["Paid", "Part paid", "Unpaid", "Overdue"])
