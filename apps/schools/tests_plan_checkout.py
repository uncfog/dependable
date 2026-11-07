"""Paying for a plan by bank transfer.

What must hold:

* only the school's owner can see the plan screens or place an order; a
  principal gets a 403, not a price list;
* only the plans that can be bought from the screen create an order, and an
  order is never created without bank details to pay into;
* reporting a transfer changes the order and nothing else; the school's plan
  does not move until the platform owner confirms the money;
* one school cannot read another school's order;
* confirming an order in the admin is what moves the plan, and it does so once.
"""

from __future__ import annotations

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.messages.storage.cookie import CookieStorage
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from apps.core.roles import Role

from .admin import PlanOrderAdmin
from .models import Plan, PlanOrder, School, SchoolStatus

User = get_user_model()

BANK = {
    "BILLING_BANK_NAME": "Test Bank",
    "BILLING_ACCOUNT_NAME": "Dependable Ltd",
    "BILLING_ACCOUNT_NUMBER": "0123456789",
}


def _order(school, plan=Plan.STARTER, amount=45_000, reference="PLN-TESTORDER1", **extra):
    return PlanOrder.all_objects.create(
        school=school, plan=plan, amount_naira=amount, reference=reference, **extra
    )


@override_settings(**BANK)
class PlanCheckoutTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = School.all_objects.create(name="Sunrise Academy", plan=Plan.TRIAL)
        cls.owner = User.objects.create_user(
            "plan.owner", password="pw", role=Role.SCHOOL_OWNER, school=cls.school
        )
        cls.principal = User.objects.create_user(
            "plan.principal", password="pw", role=Role.PRINCIPAL, school=cls.school
        )
        cls.other_school = School.all_objects.create(name="Other Academy")
        cls.other_owner = User.objects.create_user(
            "other.owner", password="pw", role=Role.SCHOOL_OWNER, school=cls.other_school
        )

    def test_the_owner_sees_the_plans_with_their_prices(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("billing:checkout"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "₦45,000")
        self.assertContains(response, "₦180,000")

    def test_a_principal_cannot_open_the_plan_screens(self):
        self.client.force_login(self.principal)

        self.assertEqual(self.client.get(reverse("billing:checkout")).status_code, 403)
        self.assertFalse(PlanOrder.all_objects.exists())

    def test_a_paid_plan_creates_one_order_and_reuses_it(self):
        self.client.force_login(self.owner)

        response = self.client.post(reverse("billing:checkout"), {"plan": Plan.GROWTH})

        order = PlanOrder.all_objects.get()
        self.assertEqual(order.plan, Plan.GROWTH)
        self.assertEqual(order.amount_naira, 90_000)
        self.assertEqual(order.status, PlanOrder.Status.AWAITING_TRANSFER)
        self.assertRedirects(response, reverse("billing:order", args=[order.reference]))

        self.client.post(reverse("billing:checkout"), {"plan": Plan.GROWTH})
        self.assertEqual(PlanOrder.all_objects.count(), 1)

    def test_plans_that_cannot_be_bought_create_no_order(self):
        self.client.force_login(self.owner)

        for plan in (Plan.FREE, Plan.ENTERPRISE, Plan.TRIAL, "not-a-plan"):
            self.client.post(reverse("billing:checkout"), {"plan": plan})

        self.assertFalse(PlanOrder.all_objects.exists())

    def test_the_plan_the_school_is_already_on_cannot_be_bought_again(self):
        self.school.plan = Plan.STARTER
        self.school.save()
        self.client.force_login(self.owner)

        self.client.post(reverse("billing:checkout"), {"plan": Plan.STARTER})

        self.assertFalse(PlanOrder.all_objects.exists())

    @override_settings(BILLING_BANK_NAME="")
    def test_no_order_is_taken_without_bank_details(self):
        self.client.force_login(self.owner)

        self.client.post(reverse("billing:checkout"), {"plan": Plan.STARTER})

        self.assertFalse(PlanOrder.all_objects.exists())

    def test_the_order_page_shows_the_bank_details_and_the_reference(self):
        _order(self.school)
        self.client.force_login(self.owner)

        response = self.client.get(reverse("billing:order", args=["PLN-TESTORDER1"]))

        self.assertContains(response, "Test Bank")
        self.assertContains(response, "0123456789")
        self.assertContains(response, "PLN-TESTORDER1")

    def test_reporting_a_transfer_changes_only_the_order(self):
        _order(self.school)
        self.client.force_login(self.owner)

        self.client.post(reverse("billing:order_reported", args=["PLN-TESTORDER1"]))

        order = PlanOrder.all_objects.get()
        self.assertEqual(order.status, PlanOrder.Status.TRANSFER_REPORTED)
        self.assertIsNotNone(order.transfer_reported_at)
        self.school.refresh_from_db()
        self.assertEqual(self.school.plan, Plan.TRIAL)

    def test_reporting_twice_keeps_the_first_time(self):
        _order(self.school)
        self.client.force_login(self.owner)

        self.client.post(reverse("billing:order_reported", args=["PLN-TESTORDER1"]))
        first = PlanOrder.all_objects.get().transfer_reported_at
        self.client.post(reverse("billing:order_reported", args=["PLN-TESTORDER1"]))

        self.assertEqual(PlanOrder.all_objects.get().transfer_reported_at, first)

    def test_another_schools_order_is_not_found(self):
        _order(self.other_school)
        self.client.force_login(self.owner)

        response = self.client.get(reverse("billing:order", args=["PLN-TESTORDER1"]))

        self.assertEqual(response.status_code, 404)


class ConfirmPlanPaymentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = School.all_objects.create(name="Bright Future", plan=Plan.TRIAL)

    def _confirm(self, orders):
        request = RequestFactory().post("/admin/schools/planorder/")
        request._messages = CookieStorage(request)
        PlanOrderAdmin(PlanOrder, admin.site).mark_paid_and_activate(
            request, PlanOrder.all_objects.filter(pk__in=[o.pk for o in orders])
        )

    def test_confirming_moves_the_school_onto_the_plan_it_paid_for(self):
        order = _order(self.school, plan=Plan.PROFESSIONAL, amount=180_000)

        self._confirm([order])

        self.school.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(self.school.plan, Plan.PROFESSIONAL)
        self.assertEqual(self.school.status, SchoolStatus.ACTIVE)
        self.assertEqual(order.status, PlanOrder.Status.PAID)
        self.assertIsNotNone(order.paid_at)

    def test_confirming_twice_does_not_move_the_school_again(self):
        order = _order(self.school)
        self._confirm([order])
        first = PlanOrder.all_objects.get().paid_at

        self._confirm([order])

        self.assertEqual(PlanOrder.all_objects.get().paid_at, first)

    def test_a_cancelled_order_is_never_confirmed(self):
        order = _order(self.school, status=PlanOrder.Status.CANCELLED)

        self._confirm([order])

        self.school.refresh_from_db()
        self.assertEqual(self.school.plan, Plan.TRIAL)
        order.refresh_from_db()
        self.assertEqual(order.status, PlanOrder.Status.CANCELLED)

    def test_the_admin_cannot_add_an_order_by_hand(self):
        self.assertFalse(PlanOrderAdmin(PlanOrder, admin.site).has_add_permission(None))
