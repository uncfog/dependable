"""Tests for the public side and the auth flows.

The rules that must never regress: the front door is reachable signed out, a
signup creates exactly one school, one branch and one owner or none of them,
every role lands on its own dashboard and cannot wander into another's, and a
password can be reset by someone who has lost theirs.

The role-redirect tests run against accounts made by ``bootstrap_tenant``
itself, so "log in as each seeded role and check where you land" is asserted
against the same command that produces the demo accounts rather than against a
hand-built copy of them that could drift.
"""

from __future__ import annotations

import re
from io import StringIO

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from apps.academics.models import Class, Level
from apps.core.navigation import home_url_for, nav_for
from apps.core.branding import PRODUCT_NAME
from apps.core.roles import Role
from apps.schools.models import Branch, School

User = get_user_model()


def signup_post(**overrides) -> dict:
    data = {
        "school_name": "Bright Star Academy",
        "full_name": "Ngozi Okonkwo",
        "email": "ngozi@brightstar.example",
        "password": "correct-horse-battery",
        "password_confirm": "correct-horse-battery",
    }
    data.update(overrides)
    return data


class PublicPagesTests(TestCase):
    def test_the_landing_page_is_reachable_signed_out(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, PRODUCT_NAME)

    def test_it_offers_both_ways_in(self):
        response = self.client.get(reverse("core:landing"))
        self.assertContains(response, reverse("core:signup"))
        self.assertContains(response, reverse("login"))

    def test_the_landing_page_renders_no_signed_in_shell(self):
        """base.html only draws the sidebar for an authenticated user."""
        response = self.client.get("/")
        self.assertNotContains(response, "Sign out")

    def test_a_signed_in_visitor_is_sent_to_their_dashboard(self):
        owner = User.objects.create_user(
            "o", password="pw", role=Role.SCHOOL_OWNER,
            school=School.all_objects.create(name="Somewhere"),
        )
        self.client.force_login(owner)
        self.assertRedirects(
            self.client.get("/"), reverse("core:school_dashboard")
        )

    def test_the_login_page_renders_signed_out(self):
        response = self.client.get(reverse("login"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sign in")
        self.assertContains(response, reverse("password_reset"))
        self.assertContains(response, reverse("core:signup"))


class SignupTests(TestCase):
    url = reverse("core:signup")

    def test_the_form_renders(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "School name")

    def test_it_creates_the_school_its_first_branch_and_the_owner(self):
        response = self.client.post(self.url, signup_post())
        # Plans & billing first, then onboarding.
        self.assertRedirects(response, reverse("billing:checkout") + "?welcome=1")

        school = School.all_objects.get(name="Bright Star Academy")
        self.assertEqual(school.contact_email, "ngozi@brightstar.example")

        branches = Branch.all_objects.filter(school=school)
        self.assertEqual(len(branches), 1)
        self.assertEqual(branches[0].name, "Main Campus")

        owner = User.objects.get(email="ngozi@brightstar.example")
        self.assertEqual(owner.role, Role.SCHOOL_OWNER)
        self.assertEqual(owner.school, school)
        # No branch: an owner sees every campus, present and future.
        self.assertIsNone(owner.branch)
        self.assertEqual(owner.first_name, "Ngozi")
        self.assertEqual(owner.last_name, "Okonkwo")
        self.assertTrue(owner.check_password("correct-horse-battery"))

    def test_the_new_owner_is_signed_in_and_goes_from_plans_to_onboarding(self):
        response = self.client.post(self.url, signup_post(), follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["user"].is_authenticated)
        self.assertEqual(response.context["user"].role, Role.SCHOOL_OWNER)
        self.assertContains(response, "Continue to set up your school")
        self.assertContains(self.client.get(reverse("core:onboarding")), "Academic setup")

    def test_a_one_word_name_does_not_crash(self):
        self.client.post(self.url, signup_post(full_name="Ngozi"))
        owner = User.objects.get(email="ngozi@brightstar.example")
        self.assertEqual(owner.first_name, "Ngozi")
        self.assertEqual(owner.last_name, "")

    def test_the_username_comes_from_the_email(self):
        self.client.post(self.url, signup_post())
        self.assertTrue(User.objects.filter(username="ngozi").exists())

    def test_a_clashing_username_is_given_a_suffix_not_an_error(self):
        User.objects.create_user("ngozi", password="pw", role=Role.PLATFORM_OWNER)
        response = self.client.post(self.url, signup_post())
        self.assertRedirects(response, reverse("billing:checkout") + "?welcome=1")
        self.assertTrue(
            User.objects.filter(email="ngozi@brightstar.example",
                                username="ngozi-2").exists()
        )

    def test_a_duplicate_email_is_refused(self):
        User.objects.create_user(
            "someone", email="ngozi@brightstar.example", password="pw",
            role=Role.PLATFORM_OWNER,
        )
        response = self.client.post(self.url, signup_post())
        self.assertEqual(response.status_code, 200)
        self.assertIn("email", response.context["form"].errors)
        self.assertFalse(School.all_objects.filter(name="Bright Star Academy").exists())

    def test_mismatched_passwords_are_refused(self):
        response = self.client.post(
            self.url, signup_post(password_confirm="something-else")
        )
        self.assertIn("password_confirm", response.context["form"].errors)
        self.assertFalse(School.all_objects.filter(name="Bright Star Academy").exists())

    def test_a_weak_password_is_refused_by_djangos_validators(self):
        response = self.client.post(
            self.url, signup_post(password="1234", password_confirm="1234")
        )
        self.assertIn("password", response.context["form"].errors)
        self.assertFalse(School.all_objects.filter(name="Bright Star Academy").exists())

    def test_a_failed_signup_leaves_no_half_built_tenant(self):
        """School, branch and owner arrive together or not at all."""
        before = (School.all_objects.count(), Branch.all_objects.count(),
                  User.objects.count())
        self.client.post(self.url, signup_post(email="not-an-email"))
        after = (School.all_objects.count(), Branch.all_objects.count(),
                 User.objects.count())
        self.assertEqual(before, after)

    def test_signup_redirects_an_already_signed_in_user_away(self):
        self.client.post(self.url, signup_post())
        self.assertRedirects(
            self.client.get(self.url), reverse("core:school_dashboard")
        )


class SeededRoleTestCase(TestCase):
    """The demo tenant, built by the same command a developer runs."""

    #: Passed to the command rather than read off its default, so renaming the
    #: scaffold's password is not a test failure in four unrelated places.
    password = "scaffold-password"

    @classmethod
    def setUpTestData(cls):
        call_command(
            "bootstrap_tenant",
            "--name", "Fulfilled Academy",
            "--password", cls.password,
            stdout=StringIO(),
        )
        cls.school = School.all_objects.get(name="Fulfilled Academy")
        cls.branch = Branch.all_objects.get(school=cls.school)

    def sign_in(self, username: str):
        return self.client.post(
            reverse("login"),
            {"username": username, "password": self.password},
            follow=True,
        )


class RoleRedirectTests(SeededRoleTestCase):
    """Every seeded role, signed in, landing where it should."""

    EXPECTED = {
        "platform.owner": "core:platform_overview",
        "fulfilled-academy.owner": "core:school_dashboard",
        "fulfilled-academy.principal": "core:branch_dashboard",
        "fulfilled-academy.bursar": "core:bursar_dashboard",
    }

    def test_the_seeded_accounts_exist(self):
        for username in self.EXPECTED:
            user = User.objects.filter(username=username).first()
            self.assertIsNotNone(user, username)
            self.assertTrue(user.check_password(self.password), username)
            # Needed for password reset to be able to reach them.
            self.assertTrue(user.email, username)

    def test_each_role_lands_on_its_own_dashboard(self):
        for username, url_name in self.EXPECTED.items():
            with self.subTest(username=username):
                self.client.logout()
                response = self.sign_in(username)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    response.redirect_chain[-1][0], reverse(url_name)
                )

    def test_each_dashboard_actually_renders(self):
        for username, url_name in self.EXPECTED.items():
            with self.subTest(username=username):
                self.client.logout()
                response = self.sign_in(username)
                self.assertEqual(response.status_code, 200)
                self.assertTemplateUsed(
                    response,
                    {
                        "core:platform_overview": "core/dashboard_platform.html",
                        "core:school_dashboard": "core/dashboard_school.html",
                        "core:branch_dashboard": "core/dashboard_branch.html",
                        "core:bursar_dashboard": "core/dashboard_bursar.html",
                    }[url_name],
                )

    def test_the_dispatcher_sends_each_role_to_the_same_place(self):
        """/dashboard/ and the post-login redirect must never disagree."""
        for username, url_name in self.EXPECTED.items():
            with self.subTest(username=username):
                self.client.force_login(User.objects.get(username=username))
                self.assertRedirects(
                    self.client.get(reverse("core:dashboard")), reverse(url_name)
                )

    def test_the_sidebar_home_link_matches_where_login_lands(self):
        """The invariant that matters: whatever the sidebar's entry for this
        role's home is called, it points where signing in lands.

        Asserted by href rather than by position and label. Three roles reach
        their home through Overview > Dashboard; the platform owner reaches theirs
        through Platform > Schools, because for them that *is* the screen -- and a
        Dashboard row beside it was a duplicate that lit two rows at once.
        """
        from apps.core.modules import ALL_KEYS
        from apps.core.permissions import capabilities_for

        for username, url_name in self.EXPECTED.items():
            with self.subTest(username=username):
                user = User.objects.get(username=username)
                role = Role.PLATFORM_OWNER if user.is_superuser else user.role
                expected = reverse(url_name)

                entries = [
                    item
                    for section in nav_for(
                        role, "/", {c.value for c in capabilities_for(role)},
                        ALL_KEYS,
                    )
                    for item in section.items
                    if item.href == expected
                ]
                self.assertEqual(len(entries), 1, f"{username}: {entries}")
                self.assertTrue(entries[0].available)
                self.assertEqual(home_url_for(user), expected)

    def test_another_roles_dashboard_bounces_you_to_your_own(self):
        self.client.force_login(User.objects.get(username="fulfilled-academy.bursar"))
        for url_name in ("core:platform_overview", "core:school_dashboard",
                         "core:branch_dashboard"):
            with self.subTest(url_name=url_name):
                self.assertRedirects(
                    self.client.get(reverse(url_name)),
                    reverse("core:bursar_dashboard"),
                )

    def test_the_dashboards_need_a_login(self):
        for url_name in ("core:platform_overview", "core:school_dashboard",
                         "core:branch_dashboard", "core:bursar_dashboard",
                         "core:onboarding", "core:dashboard"):
            with self.subTest(url_name=url_name):
                response = self.client.get(reverse(url_name))
                self.assertEqual(response.status_code, 302)
                self.assertIn("/accounts/login/", response["Location"])


class LoginTests(SeededRoleTestCase):
    def test_bad_credentials_say_so_and_do_not_sign_you_in(self):
        response = self.client.post(
            reverse("login"),
            {"username": "fulfilled-academy.owner", "password": "wrong"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Please enter a correct")
        self.assertFalse(response.context["user"].is_authenticated)

    def test_an_unknown_username_gets_the_same_message(self):
        """Not "no such user" -- that would confirm who holds an account."""
        response = self.client.post(
            reverse("login"), {"username": "nobody", "password": "wrong"}
        )
        self.assertContains(response, "Please enter a correct")

    def test_a_deep_link_survives_the_login_page(self):
        target = reverse("students:student_list")
        response = self.client.post(
            f"{reverse('login')}?next={target}",
            {"username": "fulfilled-academy.principal", "password": self.password},
        )
        self.assertRedirects(response, target)

    def test_an_already_signed_in_user_is_bounced_off_the_login_page(self):
        self.client.force_login(User.objects.get(username="fulfilled-academy.bursar"))
        self.assertRedirects(
            self.client.get(reverse("login")), reverse("core:bursar_dashboard")
        )


class LogoutTests(SeededRoleTestCase):
    def test_signing_out_returns_to_the_front_door(self):
        self.client.force_login(User.objects.get(username="fulfilled-academy.owner"))
        response = self.client.post(reverse("logout"))
        self.assertRedirects(response, reverse("core:landing"))

    def test_the_session_really_ends(self):
        self.client.force_login(User.objects.get(username="fulfilled-academy.owner"))
        self.client.post(reverse("logout"))
        response = self.client.get(reverse("core:school_dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])


class PasswordResetTests(SeededRoleTestCase):
    def test_the_whole_flow_ends_with_a_working_new_password(self):
        user = User.objects.get(username="fulfilled-academy.bursar")

        response = self.client.post(
            reverse("password_reset"), {"email": user.email}, follow=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Check your email")

        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        # The reset email renders without a request, so the product name
        # only reaches it via BrandedPasswordResetView.extra_email_context.
        self.assertIn(PRODUCT_NAME, message.subject)
        self.assertIn(PRODUCT_NAME, message.body)
        self.assertEqual(message.to, [user.email])

        match = re.search(r"/accounts/reset/[^/]+/[^/\s]+/", message.body)
        self.assertIsNotNone(match, message.body)

        # The confirm view swaps the token for a session one and redirects to
        # the form; posting to that final URL is what sets the password.
        form_page = self.client.get(match.group(), follow=True)
        self.assertEqual(form_page.status_code, 200)
        self.assertContains(form_page, "Set a new password")

        done = self.client.post(
            form_page.redirect_chain[-1][0],
            {"new_password1": "a-brand-new-secret", "new_password2": "a-brand-new-secret"},
            follow=True,
        )
        self.assertContains(done, "Password changed")

        user.refresh_from_db()
        self.assertTrue(user.check_password("a-brand-new-secret"))

        # ...and the new password works on the real login screen, landing the
        # bursar back where they belong.
        self.client.logout()
        signed_in = self.client.post(
            reverse("login"),
            {"username": user.username, "password": "a-brand-new-secret"},
        )
        self.assertRedirects(signed_in, reverse("core:bursar_dashboard"))

    def test_an_unknown_address_says_the_same_thing_and_sends_nothing(self):
        response = self.client.post(
            reverse("password_reset"), {"email": "stranger@example.com"}, follow=True
        )
        self.assertContains(response, "Check your email")
        self.assertEqual(len(mail.outbox), 0)

    def test_the_request_form_renders(self):
        response = self.client.get(reverse("password_reset"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Reset your password")


class OnboardingTests(SeededRoleTestCase):
    def test_a_brand_new_school_has_everything_still_to_do(self):
        self.client.post(reverse("core:signup"), signup_post())
        response = self.client.get(reverse("core:onboarding"))
        setup = response.context["setup"]
        self.assertEqual(setup["done_count"], 0)
        self.assertFalse(setup["is_complete"])
        self.assertEqual(setup["next_step"]["label"], "Academic setup")

    def test_a_step_ticks_itself_off_once_the_data_exists(self):
        self.client.post(reverse("core:signup"), signup_post())
        school = School.all_objects.get(name="Bright Star Academy")
        Class.all_objects.create(
            branch=Branch.all_objects.get(school=school),
            name="Primary 1", level=Level.PRIMARY, year_in_level=1,
        )
        setup = self.client.get(reverse("core:onboarding")).context["setup"]
        self.assertEqual(setup["done_count"], 1)
        self.assertEqual(setup["steps"][0]["label"], "Academic setup")
        self.assertTrue(setup["steps"][0]["done"])
        self.assertEqual(setup["next_step"]["label"], "Finance setup")

    def test_progress_is_the_signed_in_schools_own(self):
        """A busy neighbour must not tick off your setup steps."""
        self.client.post(reverse("core:signup"), signup_post())
        # Fulfilled Academy (from bootstrap) is a different tenant entirely.
        Class.all_objects.create(
            branch=self.branch, name="JSS 1",
            level=Level.JUNIOR_SECONDARY, year_in_level=1,
        )
        setup = self.client.get(reverse("core:onboarding")).context["setup"]
        self.assertEqual(setup["done_count"], 0)

    def test_the_owner_dashboard_nudges_until_setup_is_done(self):
        self.client.post(reverse("core:signup"), signup_post())
        response = self.client.get(reverse("core:school_dashboard"))
        self.assertContains(response, "Finish setting up")
        self.assertContains(response, reverse("core:onboarding"))


class DashboardScopingTests(SeededRoleTestCase):
    """The dashboards must not be the place tenant isolation springs a leak."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.rival = School.all_objects.create(name="Rival College")
        cls.rival_branch = Branch.all_objects.create(
            school=cls.rival, name="West Campus"
        )
        cls.annex = Branch.all_objects.create(
            school=cls.school, name="Annex", city="Ibadan"
        )

    def test_an_owner_sees_their_own_campuses_and_no_others(self):
        """The campus-by-campus table moved to Reports, so the leak it could
        spring is tested where it now lives -- and the dashboard is still checked
        for the rival's name, because a leak there would be just as bad."""
        self.client.force_login(User.objects.get(username="fulfilled-academy.owner"))

        dashboard = self.client.get(reverse("core:school_dashboard"))
        self.assertEqual(dashboard.context["branch_count"], 2)
        self.assertNotContains(dashboard, "West Campus")

        report = self.client.get(reverse("reports:index"))
        names = {
            branch.name for branch in report.context["scope"].branches
        }
        self.assertEqual(names, {"Main Campus", "Annex"})
        self.assertNotContains(report, "West Campus")

    def test_a_platform_owner_sees_every_school(self):
        """The cards are their own screen now -- /platform/ is the dashboard and
        /platform/schools/ is the grid, because the two sharing a URL was what lit
        both sidebar rows at once."""
        self.client.force_login(User.objects.get(username="platform.owner"))
        response = self.client.get(reverse("core:platform_schools"))
        names = {card["school"].name for card in response.context["cards"]}
        self.assertEqual(names, {"Fulfilled Academy", "Rival College"})
        self.assertEqual(response.context["school_count"], 2)

    def test_their_dashboard_counts_every_school_too(self):
        self.client.force_login(User.objects.get(username="platform.owner"))
        response = self.client.get(reverse("core:platform_overview"))
        self.assertEqual(response.context["school_count"], 2)

    def test_a_principal_sees_only_their_own_branch(self):
        self.client.force_login(
            User.objects.get(username="fulfilled-academy.principal")
        )
        response = self.client.get(reverse("core:branch_dashboard"))
        self.assertEqual(response.status_code, 200)
        # The Annex belongs to their school but not to them.
        self.assertNotContains(response, "Annex")


class PlanBeforeOnboardingTests(TestCase):
    """A plan picked on the landing page rides through sign-up to Plans & billing,
    which offers the way on to onboarding until the school gets there."""

    url = reverse("core:signup")

    def test_the_landing_plan_is_carried_in_the_form(self):
        body = self.client.get(self.url + "?plan=growth").content.decode()
        self.assertIn('<input type="hidden" name="plan" value="growth">', body)

    def test_an_unknown_plan_is_dropped(self):
        body = self.client.get(self.url + "?plan=platinum").content.decode()
        self.assertNotIn('name="plan"', body)

    def test_sign_up_opens_plans_with_the_pick_marked(self):
        response = self.client.post(self.url, signup_post(plan="growth"))
        self.assertRedirects(response, reverse("billing:checkout") + "?welcome=1&plan=growth")
        page = self.client.get(reverse("billing:checkout") + "?welcome=1&plan=growth")
        self.assertContains(page, "Welcome to SCHOOLCORD.")
        self.assertContains(page, "Your pick")
        self.assertContains(page, f'href="{reverse("core:onboarding")}"')
        self.assertContains(page, "Continue to set up your school")

    def test_onboarding_ends_the_plan_step(self):
        self.client.post(self.url, signup_post())
        self.client.get(reverse("core:onboarding"))
        page = self.client.get(reverse("billing:checkout"))
        self.assertNotContains(page, "Continue to set up your school")

    def test_the_sign_in_pages_wear_the_landing_look(self):
        for url in (reverse("login"), self.url, reverse("password_reset")):
            with self.subTest(url=url):
                body = self.client.get(url).content.decode()
                self.assertIn("sc-auth", body)
                self.assertIn("data-sc-wash", body)
                self.assertIn("sc-auth-card", body)
                self.assertIn("js/vendor/23rd/shader-gradient-vanilla.js", body)
