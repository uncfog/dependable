"""Plans & billing for the platform owner, and the plan section in account settings."""

from __future__ import annotations

from django.test import TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import User
from apps.core.navigation import nav_for
from apps.core.roles import Role
from apps.schools.models import Branch, Plan, PlanOrder, School, SchoolStatus
from apps.schools.plans import OFFERS


@override_settings(
    BILLING_BANK_NAME="Test Bank", BILLING_ACCOUNT_NAME="Test Ltd", BILLING_ACCOUNT_NUMBER="0123456789"
)
class PlatformBillingTests(TestCase):
    def setUp(self):
        self.school = School.objects.create(name="Riverbank", slug="riverbank", plan=Plan.TRIAL,
                                            status=SchoolStatus.TRIALING)
        Branch.objects.create(school=self.school, name="Main")
        self.platform = User.objects.create_user(
            username="platform", password="x-pass-123!", role=Role.PLATFORM_OWNER
        )
        self.owner = User.objects.create_user(
            username="owner", password="x-pass-123!", role=Role.SCHOOL_OWNER, school=self.school
        )
        self.order = PlanOrder.all_objects.create(
            school=self.school, plan=Plan.STARTER, amount_naira=45_000, reference="PLN-T1",
            status=PlanOrder.Status.TRANSFER_REPORTED,
        )

    def test_the_nav_item_is_live_for_the_platform_owner(self):
        items = {i.label: i for s in nav_for(Role.PLATFORM_OWNER, "/") for i in s.items}
        self.assertTrue(items["Plans & billing"].available)
        self.assertEqual(items["Plans & billing"].href, reverse("core:platform_billing"))

    def test_the_page_shows_every_plan_the_landing_page_sells_and_the_waiting_transfer(self):
        self.client.force_login(self.platform)
        response = self.client.get(reverse("core:platform_billing"))
        self.assertEqual(response.status_code, 200)
        for offer in OFFERS:
            self.assertContains(response, offer.name)
            self.assertContains(response, offer.price_display)
        self.assertContains(response, "PLN-T1")
        self.assertContains(response, "Riverbank")

    def test_a_school_owner_cannot_open_it_or_confirm(self):
        self.client.force_login(self.owner)
        self.assertNotEqual(self.client.get(reverse("core:platform_billing")).status_code, 200)
        self.client.post(reverse("core:platform_billing_confirm", args=[self.order.pk]))
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, PlanOrder.Status.TRANSFER_REPORTED)

    def test_confirming_switches_the_plan_once(self):
        self.client.force_login(self.platform)
        url = reverse("core:platform_billing_confirm", args=[self.order.pk])
        self.client.post(url)
        self.order.refresh_from_db()
        self.school.refresh_from_db()
        self.assertEqual(self.order.status, PlanOrder.Status.PAID)
        self.assertEqual(self.school.plan, Plan.STARTER)
        self.assertEqual(self.school.status, SchoolStatus.ACTIVE)
        paid_at = self.order.paid_at
        self.client.post(url)
        self.order.refresh_from_db()
        self.assertEqual(self.order.paid_at, paid_at)

    def test_the_platform_owner_can_preview_the_landing_page_and_nobody_else(self):
        self.client.force_login(self.platform)
        response = self.client.get(reverse("core:landing") + "?preview=1")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="pricing"')
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse("core:landing") + "?preview=1").status_code, 302)

    def test_a_school_owner_sees_their_plan_in_account_settings(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("accounts:settings"))
        self.assertContains(response, "Plan and billing")
        self.assertContains(response, f'href="{reverse("billing:checkout")}"')
        self.assertContains(response, "PLN-T1")

    def test_the_platform_owner_has_no_plan_section(self):
        self.client.force_login(self.platform)
        self.assertNotContains(self.client.get(reverse("accounts:settings")), "Plan and billing")

    def test_the_platform_cards_count_schools_instead_of_selling(self):
        self.client.force_login(self.platform)
        response = self.client.get(reverse("core:platform_billing"))
        self.assertContains(response, "0 schools on it")
        # Trial is not sold on the pricing page, so it is counted in a note instead.
        self.assertContains(response, "1 school on Trial")
        self.assertNotContains(response, "Start free")
        self.assertContains(response, "₦45,000")
