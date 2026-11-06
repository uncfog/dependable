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
    "bursar": ("/finance/", ["/payments/", "/payments/record/", "/payments/outstanding/"]),
}


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
        for key in ("messaging", "admissions"):
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
        # No third-party fonts: CI should not depend on them, and text falls back.
        context.route("**/fonts.g*/**", lambda route: route.abort())
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
            f"try {{ localStorage.setItem('schoolcord-tour-seen:{user.pk}', '1'); }} catch (e) {{}}"
        )
        self.go(page, "/accounts/login/")
        page.fill('input[name="username"]', user.username)
        page.fill('input[name="password"]', PASSWORD)
        page.click('button[type="submit"]:has-text("Sign in")')
        page.wait_for_url(f"**{ROLES[role][0]}")


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
    """What a phone-sized page must not do.

    The expected failures are the known breaks recorded in the assessment, which
    Phase 2 fixes. When one is fixed this reports an "unexpected success": remove its
    decorator then, so it can never quietly break again.
    """

    def overflow(self, role, path) -> int:
        page = self.page_for(PHONE)
        self.sign_in(page, role)
        self.go(page, path)
        return page.evaluate(
            "document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )

    def test_the_dashboard_fits(self):
        self.assertLessEqual(self.overflow("owner", "/school/"), 0)

    def test_the_reports_page_fits(self):
        self.assertLessEqual(self.overflow("owner", "/reports/"), 0)

    @unittest.expectedFailure
    def test_the_students_page_fits(self):
        self.assertLessEqual(self.overflow("owner", "/students/"), 0)

    @unittest.expectedFailure
    def test_the_payments_page_fits(self):
        self.assertLessEqual(self.overflow("bursar", "/payments/"), 0)

    def test_the_record_payment_page_fits(self):
        self.assertLessEqual(self.overflow("owner", "/payments/record/"), 0)
