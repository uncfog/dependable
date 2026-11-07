"""The parts of the phone experience the server is responsible for.

How pages actually lay out is checked in a real browser, in e2e/. This covers what
it sends: which shortcuts each role gets, the student search behind the payment
form, the error pages, and that nothing depends on a third party's fonts.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from apps.academics.models import Class, Level
from apps.core.navigation import bottom_nav_for, nav_for
from apps.core.roles import Role
from apps.fees.models import Term, TermSequence
from apps.schools.models import Branch, School, SchoolStatus
from apps.students.models import Sex, Student, StudentStatus

User = get_user_model()


class ResponsiveTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = School.all_objects.create(name="Alpha", status=SchoolStatus.ACTIVE)
        cls.north = Branch.all_objects.create(school=cls.school, name="North")
        cls.south = Branch.all_objects.create(school=cls.school, name="South")
        cls.klass = Class.all_objects.create(
            branch=cls.north, name="JSS 1", level=Level.JUNIOR_SECONDARY, year_in_level=1
        )
        cls.south_class = Class.all_objects.create(
            branch=cls.south, name="JSS 1", level=Level.JUNIOR_SECONDARY, year_in_level=1
        )
        for branch in (cls.north, cls.south):
            Term.all_objects.create(
                branch=branch, name="First", academic_year="2026/2027",
                sequence=TermSequence.FIRST, is_current=True,
            )
        cls.ada = cls.student("N/1", "Ada", "Obi")
        cls.bola = cls.student("N/2", "Bola", "Ade")
        cls.gone = cls.student("N/3", "Gone", "Obi", status=StudentStatus.WITHDRAWN)
        cls.south_kid = cls.student("S/1", "Sade", "Obi", klass=cls.south_class)
        cls.owner = User.objects.create_user(
            "owner", password="pw", role=Role.SCHOOL_OWNER, school=cls.school
        )
        cls.principal = User.objects.create_user(
            "principal", password="pw", role=Role.PRINCIPAL, school=cls.school, branch=cls.north
        )
        cls.bursar = User.objects.create_user(
            "bursar", password="pw", role=Role.BURSAR, school=cls.school, branch=cls.north
        )
        cls.platform = User.objects.create_user(
            "platform", password="pw", role=Role.PLATFORM_OWNER
        )

    @classmethod
    def student(cls, number, first, last, klass=None, **extra):
        klass = klass or cls.klass
        return Student.all_objects.create(
            branch=klass.branch, school_class=klass, admission_number=number,
            first_name=first, last_name=last, sex=Sex.FEMALE, parent_name="P",
            parent_phone="08031234567", **extra,
        )


class BottomNavTests(ResponsiveTestCase):
    def labels(self, role, capabilities=frozenset(), modules=frozenset()):
        sections = nav_for(role, "", capabilities, modules)
        return [item.label for item in bottom_nav_for(role, sections)]

    def test_each_role_gets_the_screens_it_lives_on(self):
        self.assertEqual(
            self.labels(Role.BURSAR), ["Dashboard", "Payments", "Outstanding", "Students"]
        )
        self.assertEqual(
            self.labels(Role.SCHOOL_OWNER), ["Dashboard", "Students", "Payments"]
        )
        self.assertEqual(
            self.labels(Role.PRINCIPAL), ["Dashboard", "Students", "Payments"]
        )

    def test_a_platform_account_gets_its_own_screens(self):
        labels = self.labels(Role.PLATFORM_OWNER, capabilities={"view_staff"})

        self.assertEqual(labels, ["Dashboard", "Schools", "Users", "Students"])

    def test_it_never_offers_what_the_sidebar_does_not(self):
        """A shortcut has to pass the same gates, or it is a door that slams."""
        for role in Role.values:
            sections = nav_for(role, "", frozenset(), frozenset())
            hrefs = {item.href for section in sections for item in section.items}
            for item in bottom_nav_for(role, sections):
                self.assertIn(item.href, hrefs)

    def test_it_is_on_the_page_with_the_way_to_everything_else(self):
        self.client.force_login(self.bursar)
        response = self.client.get(reverse("core:bursar_dashboard"))

        self.assertContains(response, 'class="bottom-nav')
        self.assertContains(response, 'id="mobile-nav-open"')
        self.assertContains(response, "More")

    def test_the_current_screen_is_marked(self):
        self.client.force_login(self.bursar)
        response = self.client.get(reverse("payments:outstanding"))

        active = [i.label for i in response.context["bottom_nav"] if i.active]
        self.assertEqual(active, ["Outstanding"])

    def test_signed_out_pages_do_not_have_it(self):
        self.assertNotContains(self.client.get(reverse("login")), 'class="bottom-nav')


class StudentSearchTests(ResponsiveTestCase):
    url = reverse("payments:student_search")

    def search(self, user, q):
        self.client.force_login(user)
        return self.client.get(self.url, {"q": q})

    def results(self, user, q):
        return self.search(user, q).json()["results"]

    def test_finds_by_name_or_number(self):
        self.assertTrue(any("Ada Obi" in r for r in self.results(self.bursar, "ada")))
        self.assertTrue(any("Ada Obi" in r for r in self.results(self.bursar, "N/1")))

    def test_every_word_has_to_match(self):
        results = self.results(self.bursar, "obi ada")

        self.assertEqual(len(results), 1)
        self.assertIn("Ada Obi", results[0])

    def test_only_children_who_are_here(self):
        self.assertFalse(any("Gone" in r for r in self.results(self.bursar, "obi")))

    def test_another_campus_is_not_searched(self):
        self.assertFalse(any("Sade" in r for r in self.results(self.bursar, "obi")))
        self.assertTrue(any("Sade" in r for r in self.results(self.owner, "obi")))

    def test_another_school_is_not_searched(self):
        other = School.all_objects.create(name="Bravo", status=SchoolStatus.ACTIVE)
        stranger = User.objects.create_user(
            "stranger", password="pw", role=Role.SCHOOL_OWNER, school=other
        )

        self.assertEqual(self.results(stranger, "obi"), [])

    def test_a_shared_number_comes_back_naming_the_campus(self):
        self.student("N/1", "Twin", "Eze", klass=self.south_class)

        results = self.results(self.owner, "N/1")

        self.assertEqual(len(results), 2)
        self.assertTrue(all("North" in r or "South" in r for r in results))

    def test_a_result_is_accepted_by_the_form_as_that_child(self):
        self.student("N/1", "Twin", "Eze", klass=self.south_class)
        chosen = next(r for r in self.results(self.owner, "N/1") if "Twin" in r)

        response = self.client.post(
            reverse("payments:record"),
            {"student": chosen, "amount": "100", "date_paid": "2026-01-05",
             "label": "school_fees", "method": "cash", "confirm_now": "on"},
        )

        self.assertEqual(response.status_code, 302)
        from apps.payments.models import Payment

        self.assertEqual(Payment.all_objects.get().student.first_name, "Twin")

    def test_too_short_a_search_returns_nothing(self):
        self.assertEqual(self.results(self.bursar, "a"), [])

    def test_a_large_roster_is_capped(self):
        for number in range(30):
            self.student(f"X/{number}", "Same", "Name")

        self.assertLessEqual(len(self.results(self.bursar, "same")), 20)

    def test_signed_out_gets_nothing(self):
        self.assertEqual(self.client.get(self.url, {"q": "ada"}).status_code, 302)

    def test_it_needs_the_right_to_record_payments(self):
        with mock.patch("apps.core.permissions.capabilities_for", return_value=frozenset()):
            self.assertEqual(self.search(self.bursar, "ada").status_code, 403)

    def test_the_form_no_longer_ships_the_whole_roster(self):
        self.client.force_login(self.bursar)
        response = self.client.get(reverse("payments:record"))

        self.assertNotContains(response, "Ada Obi")
        self.assertContains(response, 'id="student-options"')
        self.assertContains(response, 'data-search-url="%s"' % self.url)


class ErrorPageTests(ResponsiveTestCase):
    def test_a_missing_page_is_ours(self):
        response = self.client.get("/no-such-page/")

        self.assertEqual(response.status_code, 404)
        self.assertContains(response, "SCHOOLCORD", status_code=404)
        self.assertContains(response, "name=\"viewport\"", status_code=404)

    def test_a_crash_is_ours_and_needs_no_database(self):
        client = Client(raise_request_exception=False)
        with mock.patch(
            "apps.core.views.LandingView.get", side_effect=RuntimeError("boom")
        ):
            response = client.get(reverse("core:landing"))

        self.assertEqual(response.status_code, 500)
        self.assertContains(response, "SCHOOLCORD", status_code=500)
        self.assertNotIn("boom", response.content.decode())

    def test_an_expired_form_explains_itself(self):
        client = Client(enforce_csrf_checks=True)
        response = client.post(reverse("login"), {"username": "x", "password": "y"})

        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "expired", status_code=403)
        self.assertContains(response, "SCHOOLCORD", status_code=403)


class SelfHostedFontTests(ResponsiveTestCase):
    def test_no_page_asks_a_third_party_for_fonts(self):
        templates = Path(settings.BASE_DIR, "templates")
        for path in templates.rglob("*.html"):
            if "email" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("fonts.googleapis.com", text, path.name)
            self.assertNotIn("fonts.gstatic.com", text, path.name)

    def test_every_font_the_stylesheet_names_is_shipped(self):
        import re

        source = Path(settings.BASE_DIR, "assets", "app.css").read_text(encoding="utf-8")
        names = re.findall(r"\.\./fonts/([\w.-]+\.woff2)", source)

        self.assertGreater(len(names), 5)
        for name in names:
            self.assertTrue(Path(settings.BASE_DIR, "static", "fonts", name).exists(), name)

    def test_the_naira_sign_is_inside_a_range_we_ship(self):
        import re

        source = Path(settings.BASE_DIR, "assets", "app.css").read_text(encoding="utf-8")
        for family in ("Public Sans", "IBM Plex Mono"):
            ranges = re.findall(
                r'font-family: "%s";.*?unicode-range: ([^;]+);' % family, source, re.S
            )
            self.assertTrue(any("U+20A0-20AB" in r for r in ranges), family)
