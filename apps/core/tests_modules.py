"""Per-school module toggles: what must never regress.

The requirement was that enforcement be real rather than cosmetic, so that is
what these test. Hiding a nav entry is the easy half; the half that matters is
that the URL is not reachable by typing it, that it answers cleanly rather than
with a 500, that switching a module back on restores everything, and that one
school's switches are nothing to do with another's.

Six groups, matching the six things that could go wrong:

1. **The registry and the defaults** -- what a school gets when nobody has decided.
2. **Who may toggle** -- the platform owner, and provably nobody else.
3. **Enforcement** -- the nav, the capabilities, the URLs, the public enquiry page.
4. **Reversibility** -- switching off destroys nothing, and switching on restores.
5. **Isolation** -- school A's switches never move school B.
6. **The pilot** -- Fulfilled Academy's recorded state is the one the client asked
   for.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.academics.models import Class, Level
from apps.core import modules
from apps.core.navigation import nav_for
from apps.core.permissions import Capability, capabilities_for, capabilities_of
from apps.core.roles import Role
from apps.schools.models import Branch, School, SchoolModule

User = get_user_model()


class ModuleTestCase(TestCase):
    """Two schools, so every isolation assertion has something to fail against.

    Neither has a stored decision to begin with: they are both on the registry
    defaults, which is the state a school that signed up this morning is in.
    """

    @classmethod
    def setUpTestData(cls):
        cls.alpha = School.all_objects.create(name="Alpha Schools")
        cls.alpha_main = Branch.all_objects.create(school=cls.alpha, name="Main")
        cls.beta = School.all_objects.create(name="Beta Academy")
        cls.beta_main = Branch.all_objects.create(school=cls.beta, name="Main")

        # An active class at each, so the public enquiry page has something to
        # offer and its 200 means what it says.
        for school, branch in ((cls.alpha, cls.alpha_main), (cls.beta, cls.beta_main)):
            Class.all_objects.create(
                school=school, branch=branch, name="Primary 1",
                level=Level.PRIMARY, year_in_level=1,
            )

        cls.alpha_owner = User.objects.create_user(
            "alpha.owner", email="alpha@example.com", password="pw",
            role=Role.SCHOOL_OWNER, school=cls.alpha,
        )
        cls.alpha_principal = User.objects.create_user(
            "alpha.principal", email="ap@example.com", password="pw",
            role=Role.PRINCIPAL, school=cls.alpha, branch=cls.alpha_main,
        )
        cls.alpha_bursar = User.objects.create_user(
            "alpha.bursar", email="ab@example.com", password="pw",
            role=Role.BURSAR, school=cls.alpha, branch=cls.alpha_main,
        )
        cls.beta_owner = User.objects.create_user(
            "beta.owner", email="beta@example.com", password="pw",
            role=Role.SCHOOL_OWNER, school=cls.beta,
        )
        cls.platform = User.objects.create_superuser(
            "platform.owner", email="platform@example.com", password="pw",
            role=Role.PLATFORM_OWNER,
        )

    # -- helpers -----------------------------------------------------------

    def toggle(self, school, key, on: bool, *, follow=True):
        """POST the switch the way the screen does."""
        data = {"module": key}
        if on:
            data["enabled"] = "on"
        return self.client.post(
            reverse("core:school_module_toggle", args=[school.pk]),
            data,
            follow=follow,
        )

    def as_platform(self):
        self.client.force_login(self.platform)

    def enabled(self, school) -> set[str]:
        return set(modules.enabled_for_school(school.pk))


# ===========================================================================
# 1. The registry and the defaults
# ===========================================================================


class RegistryTests(ModuleTestCase):
    def test_fees_and_student_records_are_always_on(self):
        """The client's "fees + student enrollment always on"."""
        for key in ("students", "fees", "academics"):
            with self.subTest(module=key):
                self.assertIn(key, modules.ALWAYS_ON)
                self.assertTrue(modules.BY_KEY[key].always_on)
                self.assertIn(key, modules.default_keys())

    def test_messaging_and_admissions_are_off_until_somebody_says_otherwise(self):
        for key in ("messaging", "admissions"):
            with self.subTest(module=key):
                self.assertFalse(modules.BY_KEY[key].default_on)
                self.assertNotIn(key, modules.default_keys())

    def test_a_brand_new_school_needs_no_rows_written_for_it(self):
        fresh = School.all_objects.create(name="Signed Up This Morning")
        self.assertEqual(
            SchoolModule.all_objects.filter(school=fresh).count(), 0
        )
        self.assertEqual(self.enabled(fresh), set(modules.default_keys()))

    def test_only_the_toggleable_modules_are_offered_as_switches(self):
        offered = {row["module"].key for row in modules.state_for_school(self.alpha.pk)}
        self.assertEqual(offered, {"messaging", "admissions", "families"})

    def test_a_switch_knows_whether_it_is_a_decision_or_a_default(self):
        rows = {r["module"].key: r for r in modules.state_for_school(self.alpha.pk)}
        self.assertTrue(rows["messaging"]["is_default"])
        SchoolModule.set_state(self.alpha, "messaging", False)
        rows = {r["module"].key: r for r in modules.state_for_school(self.alpha.pk)}
        # Same answer, but now it is somebody's decision rather than the default,
        # which is what stops a later change of default moving it.
        self.assertFalse(rows["messaging"]["enabled"])
        self.assertFalse(rows["messaging"]["is_default"])

    def test_every_path_a_module_claims_resolves_to_that_module(self):
        for module in modules.MODULES:
            for prefix in module.paths:
                with self.subTest(path=prefix):
                    self.assertEqual(modules.module_for_path(prefix), module)
                    # And so does anything beneath it, including screens that do
                    # not exist yet.
                    self.assertEqual(
                        modules.module_for_path(f"{prefix}something/new/"), module
                    )

    def test_a_path_no_module_claims_is_not_gated(self):
        for path in ("/", "/dashboard/", "/school/", "/accounts/settings/",
                     "/platform/", "/branches/", "/staff/"):
            with self.subTest(path=path):
                self.assertIsNone(modules.module_for_path(path))

    def test_a_stored_key_the_registry_does_not_know_is_ignored(self):
        """A module that has been renamed or removed leaves rows behind. The
        registry is the authority on what exists, so they are inert."""
        SchoolModule.all_objects.create(
            school=self.alpha, key="telepathy", enabled=True
        )
        self.assertNotIn("telepathy", self.enabled(self.alpha))
        self.assertEqual(self.enabled(self.alpha), set(modules.default_keys()))

    def test_a_stored_row_cannot_switch_off_an_always_on_module(self):
        SchoolModule.all_objects.create(
            school=self.alpha, key="students", enabled=False
        )
        self.assertIn("students", self.enabled(self.alpha))

    def test_set_state_refuses_an_unknown_key(self):
        with self.assertRaises(ValueError):
            SchoolModule.set_state(self.alpha, "telepathy", True)
        self.assertFalse(
            SchoolModule.all_objects.filter(key="telepathy").exists()
        )

    def test_set_state_refuses_an_always_on_module(self):
        with self.assertRaises(ValueError):
            SchoolModule.set_state(self.alpha, "students", False)
        self.assertFalse(
            SchoolModule.all_objects.filter(key="students").exists()
        )

    def test_one_decision_per_module_per_school(self):
        SchoolModule.set_state(self.alpha, "messaging", True)
        SchoolModule.set_state(self.alpha, "messaging", False)
        self.assertEqual(
            SchoolModule.all_objects.filter(
                school=self.alpha, key="messaging"
            ).count(),
            1,
        )

    def test_platform_staff_are_not_a_school_and_have_everything(self):
        self.assertEqual(modules.enabled_for_user(self.platform), modules.ALL_KEYS)


# ===========================================================================
# 2. Who may toggle
# ===========================================================================


class WhoMayToggleTests(ModuleTestCase):
    def test_the_platform_owner_can_toggle(self):
        self.as_platform()
        response = self.toggle(self.alpha, "messaging", True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("messaging", self.enabled(self.alpha))

        self.toggle(self.alpha, "messaging", False)
        self.assertNotIn("messaging", self.enabled(self.alpha))

    def test_the_platform_owner_can_open_any_schools_detail_screen(self):
        self.as_platform()
        for school in (self.alpha, self.beta):
            with self.subTest(school=school.name):
                response = self.client.get(
                    reverse("core:platform_school", args=[school.pk])
                )
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, school.name)

    def test_a_school_owner_cannot_open_the_detail_screen(self):
        """Not even their own. What a school is paying for is not the school's
        own decision."""
        self.client.force_login(self.alpha_owner)
        for school in (self.alpha, self.beta):
            with self.subTest(school=school.name):
                self.assertEqual(
                    self.client.get(
                        reverse("core:platform_school", args=[school.pk])
                    ).status_code,
                    403,
                )

    def test_a_school_owner_cannot_toggle_their_own_modules(self):
        self.client.force_login(self.alpha_owner)
        response = self.toggle(self.alpha, "admissions", True, follow=False)
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("admissions", self.enabled(self.alpha))
        self.assertFalse(SchoolModule.all_objects.exists())

    def test_a_school_owner_cannot_toggle_another_schools_modules(self):
        self.client.force_login(self.alpha_owner)
        self.assertEqual(
            self.toggle(self.beta, "admissions", True, follow=False).status_code, 403
        )
        self.assertNotIn("admissions", self.enabled(self.beta))

    def test_a_principal_and_a_bursar_cannot_either(self):
        for who in (self.alpha_principal, self.alpha_bursar):
            with self.subTest(user=who.username):
                self.client.force_login(who)
                self.assertEqual(
                    self.toggle(self.alpha, "messaging", True, follow=False).status_code,
                    403,
                )
                self.assertNotIn("messaging", self.enabled(self.alpha))

    def test_a_signed_out_visitor_is_sent_to_sign_in(self):
        response = self.toggle(self.alpha, "messaging", True, follow=False)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])
        self.assertFalse(SchoolModule.all_objects.exists())

    def test_only_the_platform_role_holds_the_capability(self):
        self.assertIn(
            Capability.MANAGE_SCHOOL_MODULES,
            capabilities_for(Role.PLATFORM_OWNER),
        )
        for role in (Role.SCHOOL_OWNER, Role.PRINCIPAL, Role.BURSAR):
            with self.subTest(role=role):
                self.assertNotIn(
                    Capability.MANAGE_SCHOOL_MODULES, capabilities_for(role)
                )

    def test_the_sidebar_offers_the_schools_screen_to_the_platform_only(self):
        for role in Role.values:
            with self.subTest(role=role):
                entries = {
                    item.label: item
                    for section in nav_for(
                        role,
                        "/",
                        {c.value for c in capabilities_for(role)},
                        modules.ALL_KEYS,
                    )
                    for item in section.items
                }
                if role == Role.PLATFORM_OWNER:
                    self.assertIn("Schools", entries)
                    self.assertEqual(
                        entries["Schools"].href, reverse("core:platform_schools")
                    )
                else:
                    self.assertNotIn("Schools", entries)

    def test_a_toggle_is_never_a_get(self):
        """A GET that changed what a school pays for would be followed by every
        crawler and link prefetcher on the internet."""
        self.as_platform()
        response = self.client.get(
            reverse("core:school_module_toggle", args=[self.alpha.pk])
        )
        self.assertEqual(response.status_code, 405)
        self.assertFalse(SchoolModule.all_objects.exists())

    def test_the_switch_records_who_flipped_it(self):
        self.as_platform()
        self.toggle(self.alpha, "messaging", True)
        row = SchoolModule.all_objects.get(school=self.alpha, key="messaging")
        self.assertEqual(row.changed_by, self.platform)

    def test_a_hand_made_post_cannot_switch_off_an_always_on_module(self):
        self.as_platform()
        response = self.toggle(self.alpha, "students", False)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(SchoolModule.all_objects.filter(key="students").exists())
        self.assertIn("students", self.enabled(self.alpha))

    def test_a_hand_made_post_with_an_unknown_module_changes_nothing(self):
        self.as_platform()
        response = self.toggle(self.alpha, "telepathy", True)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(SchoolModule.all_objects.exists())

    def test_a_toggle_for_a_school_that_does_not_exist_is_a_404(self):
        self.as_platform()
        response = self.client.post(
            reverse("core:school_module_toggle", args=[9999]),
            {"module": "messaging", "enabled": "on"},
        )
        self.assertEqual(response.status_code, 404)


# ===========================================================================
# 3. Enforcement
# ===========================================================================


class NavigationIsGatedTests(ModuleTestCase):
    def test_a_school_without_messaging_has_no_messaging_entries(self):
        self.client.force_login(self.alpha_owner)
        response = self.client.get(reverse("core:school_dashboard"))
        self.assertNotContains(response, reverse("messaging:index"))
        self.assertNotContains(response, reverse("messaging:identity"))

    def test_switching_it_on_puts_them_back(self):
        SchoolModule.set_state(self.alpha, "messaging", True)
        self.client.force_login(self.alpha_owner)
        response = self.client.get(reverse("core:school_dashboard"))
        self.assertContains(response, reverse("messaging:index"))

    def test_a_school_without_admissions_has_no_admissions_entries(self):
        self.client.force_login(self.alpha_owner)
        response = self.client.get(reverse("core:school_dashboard"))
        self.assertNotContains(response, reverse("admissions:pipeline"))
        self.assertNotContains(response, reverse("admissions:fee_schedules"))

    def test_the_always_on_entries_are_always_there(self):
        self.client.force_login(self.alpha_owner)
        response = self.client.get(reverse("core:school_dashboard"))
        for url_name in ("students:student_list", "fees:structure_list",
                         "payments:index", "reports:index",
                         "academics:class_list"):
            with self.subTest(entry=url_name):
                self.assertContains(response, reverse(url_name))


class CapabilitiesAreWithdrawnTests(ModuleTestCase):
    """The doors into a switched-off module that are drawn on *other* screens.

    The fee-reminder button on the bursar's dashboard is a messaging action on a
    payments page, and the middleware guarding ``/messaging/`` cannot reach it.
    """

    def held_by(self, user) -> set[str]:
        return {c.value for c in capabilities_of(User.objects.get(pk=user.pk))}

    def test_messaging_capabilities_are_withdrawn(self):
        held = self.held_by(self.alpha_bursar)
        self.assertNotIn("send_messages", held)
        self.assertNotIn("view_messages", held)
        # And nothing else went with them.
        self.assertIn("view_payments", held)
        self.assertIn("record_payments", held)

    def test_switching_messaging_on_restores_them(self):
        SchoolModule.set_state(self.alpha, "messaging", True)
        held = self.held_by(self.alpha_bursar)
        self.assertIn("send_messages", held)
        self.assertIn("view_messages", held)

    def test_admissions_capabilities_are_withdrawn(self):
        held = self.held_by(self.alpha_owner)
        for capability in ("view_admissions", "manage_admissions",
                           "decide_admissions", "view_admission_payments",
                           "record_admission_payments"):
            with self.subTest(capability=capability):
                self.assertNotIn(capability, held)

    def test_the_fee_reminder_button_disappears_with_the_module(self):
        self.client.force_login(self.alpha_bursar)
        off = self.client.get(reverse("core:bursar_dashboard"))
        self.assertNotContains(off, reverse("messaging:fee_reminder"))

        SchoolModule.set_state(self.alpha, "messaging", True)
        on = self.client.get(reverse("core:bursar_dashboard"))
        self.assertContains(on, reverse("messaging:fee_reminder"))

    def test_the_role_table_itself_is_untouched(self):
        """A capability is a fact about a role and stays one. Withdrawal is about
        the school, which is why ``capabilities_for`` is the pure answer."""
        self.assertIn(Capability.SEND_MESSAGES, capabilities_for(Role.BURSAR))
        self.assertIn(Capability.VIEW_ADMISSIONS, capabilities_for(Role.SCHOOL_OWNER))


class UrlsAreGatedTests(ModuleTestCase):
    """The half that matters: not reachable by typing it, and never a 500."""

    MESSAGING_URLS = (
        "messaging:index",
        "messaging:compose",
        "messaging:fee_reminder",
        "messaging:identity",
        "messaging:recipient_count",
    )
    ADMISSIONS_URLS = (
        "admissions:pipeline",
        "admissions:enquiry_create",
        "admissions:fee_schedules",
        "admissions:settings",
        "admissions:requirements",
    )

    def test_every_messaging_url_refuses_cleanly(self):
        self.client.force_login(self.alpha_owner)
        for url_name in self.MESSAGING_URLS:
            with self.subTest(url=url_name):
                response = self.client.get(reverse(url_name))
                self.assertEqual(response.status_code, 403)
                self.assertContains(
                    response, "not switched on", status_code=403
                )
                self.assertContains(
                    response, "Parent messaging", status_code=403
                )

    def test_every_admissions_url_refuses_cleanly(self):
        self.client.force_login(self.alpha_owner)
        for url_name in self.ADMISSIONS_URLS:
            with self.subTest(url=url_name):
                response = self.client.get(reverse(url_name))
                self.assertEqual(response.status_code, 403)
                self.assertContains(
                    response, "not switched on", status_code=403
                )

    def test_a_post_is_refused_too(self):
        """A gate that only covered GET would be no gate at all."""
        self.client.force_login(self.alpha_owner)
        response = self.client.post(reverse("messaging:compose"), {"body": "hi"})
        self.assertEqual(response.status_code, 403)

    def test_a_url_beneath_the_prefix_is_refused_even_if_it_does_not_exist(self):
        """The reason this is a middleware. A screen added under /messaging/
        tomorrow is gated today, without anybody remembering to gate it."""
        self.client.force_login(self.alpha_owner)
        response = self.client.get("/messaging/some/screen/added/later/")
        self.assertEqual(response.status_code, 403)

    def test_the_refusal_says_nothing_has_been_deleted(self):
        """The first question a school asks on seeing this page."""
        self.client.force_login(self.alpha_owner)
        response = self.client.get(reverse("messaging:index"))
        self.assertContains(response, "Nothing has been deleted", status_code=403)

    def test_it_is_a_403_rather_than_a_404_or_a_500(self):
        self.client.force_login(self.alpha_owner)
        response = self.client.get(reverse("messaging:index"))
        self.assertEqual(response.status_code, 403)

    def test_an_always_on_module_is_never_gated(self):
        self.client.force_login(self.alpha_owner)
        for url_name in ("students:student_list", "fees:structure_list",
                         "payments:index", "reports:index",
                         "academics:class_list"):
            with self.subTest(url=url_name):
                self.assertEqual(
                    self.client.get(reverse(url_name)).status_code, 200
                )

    def test_a_signed_out_visitor_still_gets_the_login_page(self):
        """The middleware leaves anonymous requests alone: every gated prefix is
        behind a login, and LoginRequiredMixin gives the better answer."""
        response = self.client.get(reverse("messaging:index"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])

    def test_the_platform_owner_is_never_gated(self):
        """They are not a school, and the screen they switch modules from must
        not be switchable off underneath them."""
        self.as_platform()
        for url_name in self.MESSAGING_URLS + self.ADMISSIONS_URLS:
            with self.subTest(url=url_name):
                self.assertNotEqual(
                    self.client.get(reverse(url_name)).status_code, 403
                )


class PublicEnquiryIsGatedTests(ModuleTestCase):
    """The school's own public page, which no middleware can gate for it.

    It belongs to the school named in the URL rather than to the reader, and
    usually nobody is signed in at all -- so it checks for itself.
    """

    def url_for(self, school) -> str:
        return reverse(
            "admissions_public:public_enquiry", args=[school.slug]
        )

    def test_it_is_a_404_while_admissions_is_off(self):
        self.assertEqual(self.client.get(self.url_for(self.alpha)).status_code, 404)

    def test_the_done_page_is_a_404_too(self):
        url = reverse(
            "admissions_public:public_enquiry_done", args=[self.alpha.slug]
        )
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_a_post_cannot_slip_an_enquiry_in(self):
        response = self.client.post(self.url_for(self.alpha), {
            "child_first_name": "Ada", "child_last_name": "Nwosu",
            "parent_name": "Parent", "parent_phone": "08031234567",
        })
        self.assertEqual(response.status_code, 404)

    def test_switching_admissions_on_opens_it(self):
        SchoolModule.set_state(self.alpha, "admissions", True)
        self.assertEqual(self.client.get(self.url_for(self.alpha)).status_code, 200)

    def test_switching_it_back_off_closes_it_again(self):
        SchoolModule.set_state(self.alpha, "admissions", True)
        self.assertEqual(self.client.get(self.url_for(self.alpha)).status_code, 200)
        SchoolModule.set_state(self.alpha, "admissions", False)
        self.assertEqual(self.client.get(self.url_for(self.alpha)).status_code, 404)

    def test_one_schools_page_is_not_the_others(self):
        SchoolModule.set_state(self.alpha, "admissions", True)
        self.assertEqual(self.client.get(self.url_for(self.alpha)).status_code, 200)
        self.assertEqual(self.client.get(self.url_for(self.beta)).status_code, 404)

    def test_a_signed_in_stranger_gets_the_same_404(self):
        """The answer does not depend on who is reading: it is a fact about the
        school in the URL."""
        self.client.force_login(self.beta_owner)
        self.assertEqual(self.client.get(self.url_for(self.alpha)).status_code, 404)


# ===========================================================================
# 4. Reversibility
# ===========================================================================


class ReversibilityTests(ModuleTestCase):
    """Toggling is reversible instantly, with no redeploy and no data loss."""

    def setUp(self):
        self.as_platform()

    def test_switching_off_and_on_again_restores_access(self):
        owner = self.alpha_owner

        self.toggle(self.alpha, "messaging", True)
        self.client.force_login(owner)
        self.assertEqual(self.client.get(reverse("messaging:index")).status_code, 200)

        self.as_platform()
        self.toggle(self.alpha, "messaging", False)
        self.client.force_login(owner)
        self.assertEqual(self.client.get(reverse("messaging:index")).status_code, 403)

        self.as_platform()
        self.toggle(self.alpha, "messaging", True)
        self.client.force_login(owner)
        self.assertEqual(self.client.get(reverse("messaging:index")).status_code, 200)

    def test_switching_off_deletes_no_rows(self):
        """A toggle, not an uninstall. The data has to be there when it comes
        back on, or the switch is a trapdoor."""
        from apps.admissions.models import Applicant

        SchoolModule.set_state(self.alpha, "admissions", True)
        applicant = Applicant.all_objects.create(
            school=self.alpha, branch=self.alpha_main, level=Level.PRIMARY,
            first_name="Ada", last_name="Nwosu", sex="female",
            parent_name="Parent of Ada", parent_phone="08031234567",
        )

        self.toggle(self.alpha, "admissions", False)
        self.assertTrue(Applicant.all_objects.filter(pk=applicant.pk).exists())

        self.toggle(self.alpha, "admissions", True)
        self.client.force_login(self.alpha_owner)
        response = self.client.get(reverse("admissions:pipeline"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Nwosu")

    def test_a_toggle_is_one_row_and_takes_effect_at_once(self):
        """"Reversible instantly, no redeploy", stated as something testable.

        A toggle writes one row and nothing else -- no schema change, no cache to
        bust, no process to restart. So the answer moves inside the same request
        cycle, which is what this asserts: read it, flip it, read it again.

        Deliberately *not* asserted by running the migration autodetector. That
        would be measuring the whole project's migration state, which other suites
        in this codebase perturb by defining throwaway models, and the assertion
        would fail depending on what ran before it. ``makemigrations --check`` is
        the right place for that question.
        """
        self.assertNotIn("messaging", self.enabled(self.alpha))

        self.toggle(self.alpha, "messaging", True)
        self.assertEqual(
            SchoolModule.all_objects.filter(school=self.alpha).count(), 1
        )
        self.assertIn("messaging", self.enabled(self.alpha))

        self.toggle(self.alpha, "messaging", False)
        self.assertEqual(
            SchoolModule.all_objects.filter(school=self.alpha).count(), 1
        )
        self.assertNotIn("messaging", self.enabled(self.alpha))

    def test_the_same_state_twice_is_not_two_rows(self):
        self.toggle(self.alpha, "messaging", True)
        self.toggle(self.alpha, "messaging", True)
        self.assertEqual(
            SchoolModule.all_objects.filter(
                school=self.alpha, key="messaging"
            ).count(),
            1,
        )


# ===========================================================================
# 5. Isolation
# ===========================================================================


class IsolationTests(ModuleTestCase):
    def test_toggling_school_a_never_moves_school_b(self):
        self.as_platform()
        self.toggle(self.alpha, "messaging", True)
        self.toggle(self.alpha, "admissions", True)

        self.assertEqual(
            self.enabled(self.alpha),
            set(modules.ALWAYS_ON) | {"messaging", "admissions"},
        )
        self.assertEqual(self.enabled(self.beta), set(modules.default_keys()))

    def test_one_schools_staff_reach_it_and_the_others_do_not(self):
        SchoolModule.set_state(self.alpha, "messaging", True)

        self.client.force_login(self.alpha_owner)
        self.assertEqual(self.client.get(reverse("messaging:index")).status_code, 200)

        self.client.force_login(self.beta_owner)
        self.assertEqual(self.client.get(reverse("messaging:index")).status_code, 403)

    def test_switching_one_off_does_not_switch_the_other_off(self):
        SchoolModule.set_state(self.alpha, "messaging", True)
        SchoolModule.set_state(self.beta, "messaging", True)
        SchoolModule.set_state(self.alpha, "messaging", False)

        self.client.force_login(self.beta_owner)
        self.assertEqual(self.client.get(reverse("messaging:index")).status_code, 200)

    def test_a_school_reads_only_its_own_switches(self):
        """``SchoolModule.objects`` is tenant-scoped like every other feature
        model, so a school querying it sees its own rows and nobody else's."""
        from apps.core.tenancy import TenantContext, activate, deactivate

        SchoolModule.set_state(self.alpha, "messaging", True)
        SchoolModule.set_state(self.beta, "admissions", True)

        token = activate(TenantContext.from_user(self.alpha_owner))
        try:
            visible = list(SchoolModule.objects.values_list("school_id", "key"))
        finally:
            deactivate(token)
        self.assertEqual(visible, [(self.alpha.pk, "messaging")])

    def test_the_platform_screens_show_each_schools_own_state(self):
        self.as_platform()
        SchoolModule.set_state(self.alpha, "messaging", True)

        overview = self.client.get(reverse("core:platform_schools"))
        by_name = {
            card["school"].name: {m["key"]: m["enabled"] for m in card["modules"]}
            for card in overview.context["cards"]
        }
        self.assertTrue(by_name["Alpha Schools"]["messaging"])
        self.assertFalse(by_name["Beta Academy"]["messaging"])

        detail = self.client.get(
            reverse("core:platform_school", args=[self.beta.pk])
        )
        state = {r["module"].key: r["enabled"] for r in detail.context["modules"]}
        self.assertFalse(state["messaging"])


# ===========================================================================
# 6. The pilot
# ===========================================================================


class PilotStateTests(TestCase):
    """What the client asked for: Fulfilled Academy with messaging and the full
    admissions pipeline off, fees and student enrolment on.

    The migration that records it is keyed on the school's name and guarded by an
    ``exists()``, so it does nothing in a test database -- which is why this
    asserts on the state rather than on the migration having run. The state is
    what was asked for; the migration is one way of getting there.
    """

    @classmethod
    def setUpTestData(cls):
        cls.school = School.all_objects.create(name="Fulfilled Academy")
        cls.branch = Branch.all_objects.create(
            school=cls.school, name="Main Campus"
        )
        for key, enabled in (("messaging", False), ("admissions", False)):
            SchoolModule.set_state(cls.school, key, enabled)
        cls.owner = User.objects.create_user(
            "fulfilled.owner", email="owner@fulfilled.example", password="pw",
            role=Role.SCHOOL_OWNER, school=cls.school,
        )

    def test_messaging_and_admissions_are_off(self):
        enabled = modules.enabled_for_school(self.school.pk)
        self.assertNotIn("messaging", enabled)
        self.assertNotIn("admissions", enabled)

    def test_fees_student_records_and_classes_are_on(self):
        enabled = modules.enabled_for_school(self.school.pk)
        for key in ("fees", "students", "academics"):
            with self.subTest(module=key):
                self.assertIn(key, enabled)

    def test_the_state_is_recorded_rather_than_left_to_the_default(self):
        """Deliberate, and the reason the pilot migration writes rows for a state
        the defaults would produce anyway: a later change of default must not
        silently switch a live school's features on mid-term, with an SMS bill
        attached."""
        decided = dict(
            SchoolModule.all_objects.filter(school=self.school).values_list(
                "key", "enabled"
            )
        )
        self.assertEqual(decided, {"messaging": False, "admissions": False})
        rows = {r["module"].key: r for r in modules.state_for_school(self.school.pk)}
        self.assertFalse(rows["messaging"]["is_default"])
        self.assertFalse(rows["admissions"]["is_default"])

    def test_the_owner_sees_neither_in_their_sidebar(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("core:school_dashboard"))
        self.assertNotContains(response, reverse("messaging:index"))
        self.assertNotContains(response, reverse("admissions:pipeline"))

    def test_the_owner_still_has_fees_students_and_reports(self):
        self.client.force_login(self.owner)
        for url_name in ("fees:structure_list", "students:student_list",
                         "payments:index", "reports:index"):
            with self.subTest(url=url_name):
                self.assertEqual(
                    self.client.get(reverse(url_name)).status_code, 200
                )

    def test_the_public_enquiry_page_is_closed(self):
        url = reverse("admissions_public:public_enquiry", args=[self.school.slug])
        self.assertEqual(self.client.get(url).status_code, 404)
