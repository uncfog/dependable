"""Guiding somebody who has just arrived: the tour, and the continuation.

Neither of these can be tested for the thing it is actually for -- whether a person
finds their way -- so these hold the parts underneath that would break it silently:

* the tour's **anchors**. Its steps find elements by `data-tour`, so a step whose
  marker is gone is a step that points at nothing. The markers are a contract, and
  this is where it is written down;
* the tour's **trigger**, which has to be on the page whether or not the tour has
  been seen, because the whole point is that it can be asked for again;
* the continuation's **conditions**. It has to appear after the step it
  congratulates, name the right next one, and take itself away when the checklist
  is finished -- otherwise it is a permanent strip of chrome on four screens.

What is deliberately not here: whether Driver.js draws a popover. That is its
library's job and it has its own tests; ours is to hand it correct steps.
"""

from __future__ import annotations

import json
import pathlib
import re

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.academics.models import Class, Level, Subject
from apps.core.roles import Role
from apps.fees.models import FeeComponent, FeeStructure, Term, TermSequence
from apps.schools.models import Branch, School
from apps.students.models import Sex, Student

User = get_user_model()


class GuidanceTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = School.all_objects.create(name="Fulfilled Academy")
        cls.main = Branch.all_objects.create(school=cls.school, name="Main Campus")
        cls.owner = User.objects.create_user(
            "owner", email="owner@example.com", password="pw",
            first_name="Ada", last_name="Owner",
            role=Role.SCHOOL_OWNER, school=cls.school,
        )
        cls.principal = User.objects.create_user(
            "principal", email="principal@example.com", password="pw",
            role=Role.PRINCIPAL, school=cls.school, branch=cls.main,
        )
        cls.bursar = User.objects.create_user(
            "bursar", email="bursar@example.com", password="pw",
            role=Role.BURSAR, school=cls.school, branch=cls.main,
        )
        cls.platform = User.objects.create_superuser(
            "platform", email="platform@example.com", password="pw",
            role=Role.PLATFORM_OWNER,
        )

    def home_for(self, user) -> str:
        from apps.core.navigation import home_url_for

        return home_url_for(user)

    def page(self, user) -> str:
        self.client.force_login(user)
        response = self.client.get(self.home_for(user))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def markers(self, body) -> set[str]:
        return set(re.findall(r'data-tour="([^"]+)"', body))


# ===========================================================================
# The tour
# ===========================================================================


class TourAssetTests(GuidanceTestCase):
    def test_the_library_is_vendored_rather_than_fetched(self):
        """A tour that depends on a CDN being up is a tour that breaks on the day
        somebody's network blocks it -- and this app otherwise works offline."""
        for path in ("static/js/vendor/driver.js.iife.js",
                     "static/css/vendor/driver.css"):
            with self.subTest(asset=path):
                self.assertTrue(
                    (pathlib.Path(settings.BASE_DIR) / path).exists(), path
                )

    def test_both_assets_are_loaded_on_a_signed_in_page(self):
        body = self.page(self.owner)
        self.assertIn("vendor/driver.js.iife.js", body)
        self.assertIn("vendor/driver.css", body)
        self.assertIn("js/tour.js", body)

    def test_signed_out_gets_the_demo_tour_not_the_app_tour(self):
        """The landing page has a tour of its own over the demo
        (static/js/demo-tour.js), so it loads Driver.js, deferred. The app's tour
        and its per-account config are for signed-in pages only."""
        body = self.client.get(reverse("core:landing")).content.decode()
        self.assertNotIn("js/tour.js", body)
        self.assertNotIn('id="tour-config"', body)
        for src in ("vendor/driver.js.iife.js", "js/demo-tour.js"):
            with self.subTest(script=src):
                tags = re.findall(r'<script[^>]*' + re.escape(src) + r'[^>]*>', body)
                self.assertEqual(len(tags), 1)
                self.assertIn("defer", tags[0])

    def test_the_scripts_do_not_block_paint(self):
        body = self.page(self.owner)
        for src in ("vendor/driver.js.iife.js", "js/tour.js"):
            with self.subTest(script=src):
                tag = re.search(r'<script[^>]*' + re.escape(src) + r'[^>]*>', body)
                self.assertIsNotNone(tag)
                self.assertIn("defer", tag.group(0))


class TourConfigTests(GuidanceTestCase):
    def config(self, user) -> dict:
        body = self.page(user)
        match = re.search(
            r'id="tour-config"[^>]*>(.*?)</script>', body, re.S
        )
        self.assertIsNotNone(match, "no tour config on the page")
        return json.loads(match.group(1))

    def test_it_carries_the_role_the_tour_branches_on(self):
        for user, role in (
            (self.owner, Role.SCHOOL_OWNER),
            (self.principal, Role.PRINCIPAL),
            (self.bursar, Role.BURSAR),
            (self.platform, Role.PLATFORM_OWNER),
        ):
            with self.subTest(user=user.username):
                self.assertEqual(self.config(user)["role"], role)

    def test_it_carries_the_account_id_so_the_flag_is_per_person(self):
        """Two people sharing an office computer each get their own first run."""
        self.assertEqual(self.config(self.owner)["userId"], self.owner.pk)
        self.assertEqual(self.config(self.bursar)["userId"], self.bursar.pk)

    def test_it_auto_starts_for_an_ordinary_account(self):
        self.assertTrue(self.config(self.owner)["autoStart"])

    def test_it_does_not_auto_start_over_a_temporary_password(self):
        """That screen holds them there until they choose one, so a tour of a
        sidebar they cannot use yet would be a tour of a locked door.

        Read off Account settings rather than their dashboard, because the
        middleware redirects them there -- which is the very state being tested.
        """
        User.objects.filter(pk=self.bursar.pk).update(must_change_password=True)
        self.client.force_login(self.bursar)
        body = self.client.get(reverse("accounts:settings")).content.decode()
        config = json.loads(
            re.search(r'id="tour-config"[^>]*>(.*?)</script>', body, re.S).group(1)
        )
        self.assertFalse(config["autoStart"])
        # The button is still there -- they can ask for it once they are through.
        self.assertIn("data-tour-start", body)

    def test_it_is_json_script_rather_than_attributes(self):
        """A school name or a role label can never break out of it."""
        body = self.page(self.owner)
        self.assertIn('type="application/json"', body)


class TourTriggerTests(GuidanceTestCase):
    def test_every_role_has_a_take_a_tour_button(self):
        for user in (self.owner, self.principal, self.bursar, self.platform):
            with self.subTest(user=user.username):
                body = self.page(user)
                self.assertIn("data-tour-start", body)
                self.assertIn("Take a tour", body)

    def test_it_is_a_button_rather_than_a_link(self):
        """It opens an overlay on this page and navigates nowhere."""
        body = self.page(self.owner)
        match = re.search(r"<(\w+)[^>]*data-tour-start", body)
        self.assertEqual(match.group(1), "button")

    def test_it_is_there_on_every_screen_not_just_the_dashboard(self):
        """"Always visible" is the requirement: a tour you got once and cannot
        get back is not one you can ask for."""
        self.client.force_login(self.owner)
        for url_name in ("students:student_list", "reports:index",
                         "accounts:settings", "core:onboarding"):
            with self.subTest(screen=url_name):
                body = self.client.get(reverse(url_name)).content.decode()
                self.assertIn("data-tour-start", body)


class TourAnchorTests(GuidanceTestCase):
    """Every stop in tour.js has to find something. A `data-tour` name is a
    contract between that file and the templates, and nothing else enforces it."""

    TOUR_JS = pathlib.Path(settings.BASE_DIR) / "static" / "js" / "tour.js"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tour_source = cls.TOUR_JS.read_text(encoding="utf-8")

    def declared_anchors(self) -> set[str]:
        """The `element:` names the steps ask for."""
        return set(re.findall(r"element:\s*'([^']+)'", self.tour_source))

    def test_the_shell_carries_the_anchors_every_role_is_shown(self):
        for user in (self.owner, self.principal, self.bursar, self.platform):
            with self.subTest(user=user.username):
                markers = self.markers(self.page(user))
                self.assertIn("sidebar", markers)
                self.assertIn("nav-home", markers)
                self.assertIn("nav-reports", markers)
                self.assertIn("theme-toggle", markers)

    def test_a_schools_roles_get_the_roster_anchor(self):
        for user in (self.owner, self.principal, self.bursar):
            with self.subTest(user=user.username):
                self.assertIn("nav-students", self.markers(self.page(user)))

    def test_a_bursar_gets_the_payments_anchor(self):
        self.assertIn("nav-payments", self.markers(self.page(self.bursar)))

    def test_leadership_gets_the_fees_anchor(self):
        for user in (self.owner, self.principal):
            with self.subTest(user=user.username):
                self.assertIn("nav-fees", self.markers(self.page(user)))

    def test_every_anchor_a_step_asks_for_exists_somewhere(self):
        """Not per role -- a step is skipped where its target is absent, which is
        the mechanism. What must not happen is an anchor no template has at all."""
        seen = set()
        for user in (self.owner, self.principal, self.bursar, self.platform):
            seen |= self.markers(self.page(user))
        # The setup banner only shows while onboarding is unfinished, which it is
        # for this fixture school.
        missing = self.declared_anchors() - seen
        self.assertEqual(missing, set(), f"no template carries: {missing}")

    def test_the_anchors_are_declared_in_navigation_not_in_the_template(self):
        """`data-tour` on a nav row comes from NavItem.tour, so a row a role does
        not get carries no marker and the tour drops that stop."""
        from apps.core.navigation import NAVIGATION

        tagged = {
            item.tour for section in NAVIGATION for item in section.items
            if item.tour
        }
        self.assertIn("nav-home", tagged)
        self.assertIn("nav-reports", tagged)
        self.assertIn("nav-students", tagged)

    def test_the_platform_owner_is_not_shown_a_schools_setup_step(self):
        """They have no school, so there is no checklist to point at."""
        self.assertNotIn("setup-progress", self.markers(self.page(self.platform)))


# ===========================================================================
# The onboarding continuation
# ===========================================================================


class ContinuationTests(GuidanceTestCase):
    """The dead end: finishing a step used to land you on a list with nothing to
    say what followed."""

    def setUp(self):
        self.client.force_login(self.owner)

    def classes(self) -> str:
        return self.client.get(reverse("academics:class_list")).content.decode()

    def add_class(self, name="Primary 1", year=1):
        return Class.all_objects.create(
            school=self.school, branch=self.main, name=name,
            level=Level.PRIMARY, year_in_level=year,
        )

    def price_a_term(self):
        term = Term.all_objects.create(
            school=self.school, branch=self.main, name="First Term 2026/2027",
            academic_year="2026/2027", sequence=TermSequence.FIRST, is_current=True,
        )
        structure = FeeStructure.all_objects.create(
            school=self.school, branch=self.main,
            school_class=Class.all_objects.first(), term=term,
        )
        FeeComponent.all_objects.create(
            school=self.school, branch=self.main, fee_structure=structure,
            name="Tuition", amount=50000,
        )

    def add_student(self):
        Student.all_objects.create(
            school=self.school, branch=self.main,
            school_class=Class.all_objects.first(),
            admission_number="FA/001", first_name="Ada", last_name="Nwosu",
            sex=Sex.choices[0][0], parent_name="Parent", parent_phone="08030000000",
        )

    # -- when it shows -----------------------------------------------------

    def test_it_is_absent_before_the_step_is_done(self):
        self.assertNotIn("Academic setup done", self.classes())

    def test_it_appears_once_the_step_is_done(self):
        self.add_class()
        self.assertIn("Academic setup done", self.classes())

    def test_it_names_the_next_step(self):
        self.add_class()
        body = " ".join(self.classes().split())
        self.assertIn("Next: <span", body.replace("&lt;", "<"))
        self.assertIn("Finance setup", body)

    def test_it_links_to_the_next_step(self):
        self.add_class()
        self.assertIn(reverse("fees:structure_list"), self.classes())

    def test_it_counts_what_was_added(self):
        self.add_class()
        self.add_class("Primary 2", 2)
        body = " ".join(self.classes().split())
        # "classes", not "classs" -- Django's pluralize appends a letter, which
        # is why the phrase is built beside the noun. See views.setup_progress.
        self.assertIn("2 classes so far", body)
        self.assertNotIn("classs", body)

    def test_it_offers_the_whole_checklist_too(self):
        self.add_class()
        self.assertIn(reverse("core:onboarding"), self.classes())

    # -- when it does not --------------------------------------------------

    def test_it_goes_when_the_checklist_is_finished(self):
        """Otherwise it is a permanent strip of chrome on four screens."""
        self.add_class()
        self.price_a_term()
        self.add_student()
        User.objects.create_user(
            "colleague", email="colleague@example.com", password="pw",
            role=Role.BURSAR, school=self.school, branch=self.main,
        )
        self.assertNotIn("Academic setup done", self.classes())

    def test_a_screen_only_congratulates_its_own_step(self):
        """The roster is not where you are told your classes are set up."""
        self.add_class()
        body = self.client.get(reverse("students:student_list")).content.decode()
        self.assertNotIn("Academic setup done", body)

    def test_the_platform_owner_sees_none_of_it(self):
        """They have no school, so there is nothing for them to set up."""
        self.add_class()
        self.client.force_login(self.platform)
        self.assertNotIn("Academic setup done", self.classes())

    # -- the tag underneath ------------------------------------------------

    def test_the_tag_answers_nothing_for_an_account_with_no_school(self):
        from apps.core.templatetags.onboarding import onboarding_progress

        request = self.client.get(reverse("core:onboarding")).wsgi_request
        request.user = self.platform
        self.assertIsNone(onboarding_progress({"request": request}))

    def test_the_tag_answers_nothing_for_a_signed_out_visitor(self):
        from django.contrib.auth.models import AnonymousUser

        from apps.core.templatetags.onboarding import onboarding_progress

        request = self.client.get(reverse("core:landing")).wsgi_request
        request.user = AnonymousUser()
        self.assertIsNone(onboarding_progress({"request": request}))

    def test_every_step_has_the_key_the_prompt_matches_on(self):
        """The prompt finds its step by key, not by label -- a label is prose and
        may be reworded."""
        from apps.core.tenancy import TenantContext, activate, deactivate
        from apps.core.views import onboarding_steps

        request = self.client.get(reverse("core:onboarding")).wsgi_request
        request.user = self.owner
        token = activate(TenantContext.from_user(self.owner))
        try:
            steps = onboarding_steps(request)
        finally:
            deactivate(token)
        keys = [step["key"] for step in steps]
        self.assertEqual(keys, ["academics", "fees", "students", "staff"])

    def test_each_of_the_four_screens_declares_its_own_step(self):
        root = pathlib.Path(settings.BASE_DIR) / "templates"
        for path, key in (
            ("academics/class_list.html", "academics"),
            ("fees/structure_list.html", "fees"),
            ("students/student_list.html", "students"),
            ("accounts/staff_list.html", "staff"),
        ):
            with self.subTest(screen=path):
                source = (root / path).read_text(encoding="utf-8")
                self.assertIn('core/_next_step.html', source)
                self.assertIn(f'after="{key}"', source)
