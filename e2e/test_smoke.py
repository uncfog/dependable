"""Does the app actually work in a browser, at desk and at phone size?

The Django suite never runs the JavaScript or lays out a page, which is how page
overflow on phones went unnoticed. These sign in as each role, open the screens that
role lives on, and take a payment. They are deliberately few: a smoke test that
takes twenty minutes stops being run.

    pip install -r requirements-dev.txt
    playwright install chromium
    python manage.py test e2e

No browser download? ``E2E_BROWSER_CHANNEL=msedge`` or ``chrome`` uses an installed one.

Not part of ``manage.py test apps``, so the main suite does not need a browser.
"""

from __future__ import annotations

import os
import unittest
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase

from apps.academics.models import Class, Level
from apps.core.roles import Role
from apps.fees.models import FeeComponent, FeeStructure, Term, TermSequence
from apps.payments.models import Payment
from apps.families.models import Family
from apps.families.services import link
from apps.payments.models import PaymentTransaction
from apps.schools.models import Branch, School, SchoolModule, SchoolStatus
from apps.students.models import Sex, Student

# Playwright's sync API runs an event loop on this thread, which Django would
# otherwise take to mean its database calls are being made from async code.
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None

PASSWORD = "e2e-only-not-a-real-password-41"
DESKTOP = {"width": 1440, "height": 900}
PHONE = {"width": 375, "height": 812}

#: Who lands where, and the screens each of them uses most.
ROLES = {
    "platform": ("/platform/", ["/platform/schools/", "/staff/"]),
    "owner": ("/school/", ["/students/", "/payments/", "/reports/", "/fees/"]),
    "principal": ("/branch/", ["/students/", "/payments/", "/reports/"]),
    "bursar": ("/finance/", ["/payments/", "/payments/record/", "/payments/outstanding/",
                             "/payments/family/", "/payments/family/record/", "/families/"]),
}


#: Open to every role, and the screens most likely to be wide.
EVERY_ROLE_ALSO_OPENS = ["/students/", "/payments/", "/payments/record/",
                         "/payments/outstanding/", "/payments/pending/", "/fees/",
                         "/academics/classes/", "/academics/subjects/",
                         "/accounts/settings/"]


@unittest.skipIf(sync_playwright is None, "Playwright is not installed.")
class BrowserTestCase(StaticLiveServerTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.playwright = sync_playwright().start()
        channel = os.environ.get("E2E_BROWSER_CHANNEL") or None
        cls.browser = cls.playwright.chromium.launch(channel=channel)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        super().tearDownClass()

    def setUp(self):
        User = get_user_model()
        school = School.all_objects.create(
            name="Bright Future Academy", status=SchoolStatus.ACTIVE
        )
        # With the paid modules on: they add buttons to the page headers, and that is
        # the layout real schools have to fit on a phone.
        for key in ("messaging", "admissions", "families"):
            SchoolModule.all_objects.create(school=school, key=key, enabled=True)
        branch = Branch.all_objects.create(school=school, name="Main Campus")
        klass = Class.all_objects.create(
            branch=branch, name="JSS 1", stream="A",
            level=Level.JUNIOR_SECONDARY, year_in_level=1,
        )
        term = Term.all_objects.create(
            branch=branch, name="First Term 2026/2027", academic_year="2026/2027",
            sequence=TermSequence.FIRST, is_current=True,
        )
        structure = FeeStructure.all_objects.create(school_class=klass, term=term)
        FeeComponent.all_objects.create(
            fee_structure=structure, name="School Fees", amount=Decimal("50000")
        )
        self.students = [
            Student.all_objects.create(
                branch=branch, school_class=klass,
                admission_number=f"BF/2026/{number:03d}",
                first_name="Oluwadamilola", last_name=f"Adeyemi{number}",
                sex=Sex.FEMALE, parent_name="Mrs Ngozi Adeyemi",
                parent_phone="08034129876",
            )
            for number in range(1, 13)
        ]
        Payment.all_objects.create(
            branch=branch, student=self.students[0], term=term, amount=Decimal("20000"),
            reference="TELLER-0001",
        )
        self.users = {
            "platform": User.objects.create_user(
                "e2e.platform", password=PASSWORD, role=Role.PLATFORM_OWNER
            ),
            "owner": User.objects.create_user(
                "e2e.owner", password=PASSWORD, role=Role.SCHOOL_OWNER, school=school
            ),
            "principal": User.objects.create_user(
                "e2e.principal", password=PASSWORD, role=Role.PRINCIPAL,
                school=school, branch=branch,
            ),
            "bursar": User.objects.create_user(
                "e2e.bursar", password=PASSWORD, role=Role.BURSAR,
                school=school, branch=branch,
            ),
        }

    def page_for(self, viewport):
        context = self.browser.new_context(viewport=viewport)
        self.addCleanup(context.close)
        page = context.new_page()
        page.script_errors = []
        page.on("pageerror", lambda error: page.script_errors.append(str(error)))
        return page

    def go(self, page, path):
        return page.goto(self.live_server_url + path, wait_until="domcontentloaded")

    def sign_in(self, page, role):
        user = self.users[role]
        # The tour would otherwise open over the first screen.
        page.add_init_script(
            f"try {{ localStorage.setItem('schoolcord-tour-seen:{user.pk}', 'yes'); }} catch (e) {{}}"
        )
        self.go(page, "/accounts/login/")
        page.fill('input[name="username"]', user.username)
        page.fill('input[name="password"]', PASSWORD)
        page.click('button[type="submit"]:has-text("Sign in")')
        page.wait_for_url(f"**{ROLES[role][0]}")


class FamilyPaymentTests(BrowserTestCase):
    def test_a_bursar_shares_a_payment_on_a_phone(self):
        school = self.users["bursar"].school
        family = Family.all_objects.create(school=school, name="The Adeyemis")
        for student in self.students[1:3]:
            link(family, student)
        page = self.page_for(PHONE)
        self.sign_in(page, "bursar")

        self.go(page, f"/payments/family/record/?family={family.pk}")
        page.fill('input[name="amount"]', "70000")
        page.click('button:has-text("Record and share")')
        page.wait_for_selector("[data-share-form]")
        txn = PaymentTransaction.all_objects.get(family=family)

        # The helper fills what each child owes, and never more than is left.
        page.locator("[data-fill]").nth(0).click()
        page.locator("[data-fill]").nth(1).click()
        boxes = page.locator('[data-share-form] input[type="number"]')
        self.assertEqual(boxes.nth(0).input_value(), "50000.00")
        self.assertEqual(boxes.nth(1).input_value(), "20000.00")
        self.assertIn("0", page.locator("[data-left-after]").inner_text())

        page.click('button:has-text("Share payment")')
        page.wait_for_selector("[data-share-form]", state="detached")

        self.assertEqual(txn.unallocated, Decimal("0"))
        self.assertEqual(
            page.evaluate(
                "document.documentElement.scrollWidth - document.documentElement.clientWidth"
            ),
            0,
        )
        self.assertEqual(page.script_errors, [])


class EveryRoleTests(BrowserTestCase):
    def check_role(self, role, viewport):
        page = self.page_for(viewport)
        self.sign_in(page, role)
        home, screens = ROLES[role]
        for path in [home, *screens]:
            with self.subTest(role=role, path=path):
                response = self.go(page, path)
                self.assertLess(response.status, 400)
                self.assertTrue(page.locator("main").is_visible())
        self.assertEqual(page.script_errors, [])

    def test_platform_at_a_desk(self):
        self.check_role("platform", DESKTOP)

    def test_platform_on_a_phone(self):
        self.check_role("platform", PHONE)

    def test_owner_at_a_desk(self):
        self.check_role("owner", DESKTOP)

    def test_owner_on_a_phone(self):
        self.check_role("owner", PHONE)

    def test_principal_at_a_desk(self):
        self.check_role("principal", DESKTOP)

    def test_principal_on_a_phone(self):
        self.check_role("principal", PHONE)

    def test_bursar_at_a_desk(self):
        self.check_role("bursar", DESKTOP)

    def test_bursar_on_a_phone(self):
        self.check_role("bursar", PHONE)


class TakingAPaymentTests(BrowserTestCase):
    def take_a_payment(self, viewport):
        page = self.page_for(viewport)
        self.sign_in(page, "bursar")
        self.go(page, "/payments/record/")

        page.fill('input[name="student"]', self.students[1].admission_number)
        page.fill('input[name="amount"]', "15000")
        page.click('button[type="submit"]:has-text("Record payment")')
        page.wait_for_url("**/receipt/")

        payment = Payment.all_objects.get(student=self.students[1])
        self.assertEqual(payment.amount, Decimal("15000"))
        self.assertIn(payment.receipt_number, page.content())
        self.assertEqual(page.script_errors, [])

    def test_at_a_desk(self):
        self.take_a_payment(DESKTOP)

    def test_on_a_phone(self):
        self.take_a_payment(PHONE)


class PhoneLayoutTests(BrowserTestCase):
    """What a phone-sized page must not do, on every screen each role uses."""

    def setUp(self):
        super().setUp()
        self.page = self.page_for(PHONE)

    def open(self, role, path):
        self.sign_in(self.page, role)
        self.go(self.page, path)
        return self.page

    def overflow(self) -> int:
        return self.page.evaluate(
            "document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )

    def check_every_screen(self, role):
        self.sign_in(self.page, role)
        home, screens = ROLES[role]
        for path in [home, *screens, *EVERY_ROLE_ALSO_OPENS]:
            with self.subTest(role=role, path=path):
                self.assertLess(self.go(self.page, path).status, 400)
                self.assertLessEqual(self.overflow(), 0)
                # The title used to be squeezed away to nothing by the buttons.
                self.assertGreater(
                    self.page.locator("header h1").bounding_box()["width"], 100
                )

    def test_nothing_overflows_for_the_platform(self):
        self.check_every_screen("platform")

    def test_nothing_overflows_for_the_owner(self):
        self.check_every_screen("owner")

    def test_nothing_overflows_for_the_principal(self):
        self.check_every_screen("principal")

    def test_nothing_overflows_for_the_bursar(self):
        self.check_every_screen("bursar")

    def test_a_table_becomes_labelled_cards(self):
        page = self.open("bursar", "/students/")

        # Kept for screen readers, in a one-pixel box, rather than removed.
        self.assertLessEqual(page.locator("#main thead").bounding_box()["height"], 1)
        cell = page.locator('#main td[data-label="Class"]').first
        self.assertTrue(cell.is_visible())
        self.assertEqual(
            page.evaluate(
                "getComputedStyle(document.querySelector('#main tbody tr')).display"
            ),
            "grid",
        )

    def row_height(self) -> float:
        return self.page.evaluate(
            """() => {
                const rows = [...document.querySelectorAll('#main tbody tr')];
                return rows.reduce((total, row) => total + row.getBoundingClientRect().height, 0)
                    / rows.length;
            }"""
        )

    def test_long_lists_stay_short_enough_to_work_through(self):
        self.sign_in(self.page, "bursar")
        for path, most in (("/payments/outstanding/", 220), ("/payments/", 160), ("/students/", 190)):
            with self.subTest(path=path):
                self.go(self.page, path)
                self.assertLessEqual(self.row_height(), most)

    def test_what_a_list_hides_on_a_phone_is_still_there_at_a_desk(self):
        page = self.page_for(DESKTOP)
        self.sign_in(page, "bursar")
        self.go(page, "/payments/")
        self.assertTrue(page.locator('th:has-text("Method")').is_visible())
        self.assertEqual(
            page.evaluate("getComputedStyle(document.querySelector('#main tbody tr')).display"),
            "table-row",
        )

    def test_links_in_a_list_are_big_enough_to_press(self):
        self.sign_in(self.page, "bursar")
        for path in ("/payments/outstanding/", "/payments/", "/students/"):
            with self.subTest(path=path):
                self.go(self.page, path)
                smallest = self.page.evaluate(
                    """() => Math.min(...[...document.querySelectorAll('#main td a')]
                        .filter(a => a.getBoundingClientRect().height > 0)
                        .map(a => a.getBoundingClientRect().height))"""
                )
                self.assertGreaterEqual(smallest, 44)

    def test_filters_start_closed_and_open_on_a_press(self):
        page = self.open("bursar", "/students/")
        search = page.locator('input[name="q"]')

        self.assertFalse(search.is_visible())
        page.get_by_role("button", name="Search and filter").click()
        self.assertTrue(search.is_visible())

    def test_filters_stay_open_when_a_search_is_applied(self):
        page = self.open("bursar", "/students/?q=Adeyemi1")

        self.assertTrue(page.locator('input[name="q"]').is_visible())

    def test_the_bottom_bar_is_there_and_more_opens_the_menu(self):
        page = self.open("bursar", "/finance/")

        self.assertTrue(page.locator(".bottom-nav").is_visible())
        page.locator("#mobile-nav-open").click()
        link = page.locator('#mobile-nav-content a[href="/fees/"]')
        link.wait_for(state="visible")
        self.assertTrue(link.is_visible())

    def test_the_bottom_bar_does_not_hide_the_end_of_a_page(self):
        page = self.open("bursar", "/payments/record/")
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        button = page.locator('button[type="submit"]:has-text("Record payment")')

        self.assertLessEqual(
            button.bounding_box()["y"] + button.bounding_box()["height"],
            page.locator(".bottom-nav").bounding_box()["y"],
        )


class DeskLayoutTests(BrowserTestCase):
    def test_the_phone_only_parts_are_gone_at_a_desk(self):
        page = self.page_for(DESKTOP)
        self.sign_in(page, "bursar")
        self.go(page, "/students/")

        self.assertFalse(page.locator(".bottom-nav").is_visible())
        self.assertTrue(page.locator("#main thead th").first.is_visible())
        self.assertTrue(page.locator('input[name="q"]').is_visible())
        self.assertFalse(page.get_by_role("button", name="Search and filter").is_visible())


class StudentSearchInTheBrowserTests(BrowserTestCase):
    def test_typing_a_name_offers_that_child(self):
        page = self.page_for(PHONE)
        self.sign_in(page, "bursar")
        self.go(page, "/payments/record/")

        page.locator('input[name="student"]').press_sequentially("Adeyemi3", delay=20)
        option = page.locator("#student-options option").first
        option.wait_for(state="attached")

        self.assertIn(self.students[2].admission_number, option.get_attribute("value"))


class FontTests(BrowserTestCase):
    def test_our_fonts_load_and_nothing_asks_a_third_party(self):
        page = self.page_for(PHONE)
        hosts = []
        page.on("request", lambda request: hosts.append(request.url.split("/")[2]))
        self.sign_in(page, "bursar")
        self.go(page, "/payments/")
        page.wait_for_load_state("networkidle")

        self.assertEqual(set(hosts), {self.live_server_url.split("/")[2]})
        self.assertTrue(page.evaluate("document.fonts.check('16px \"Public Sans\"')"))
