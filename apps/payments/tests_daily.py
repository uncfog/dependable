"""The Daily payments page and the overview tile that opens it."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.urls import reverse
from django.utils import timezone

from apps.core.views import quick_access

from .models import PaymentStatus
from .tests import PaymentsTestCase


class DailyPaymentsTests(PaymentsTestCase):
    url = reverse("payments:daily")

    def setUp(self):
        self.today = timezone.localdate()
        self.yesterday = self.today - timedelta(days=1)

    def test_it_shows_todays_payments_and_totals(self):
        self.pay(self.ada, Decimal("20000"), recorded_by=self.bursar, date_paid=self.today)
        self.pay(self.bola, Decimal("5000"), status=PaymentStatus.PENDING,
                 recorded_by=self.bursar, date_paid=self.today)
        self.pay(self.chidi, Decimal("9000"), date_paid=self.yesterday)
        self.client.force_login(self.bursar)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["collected_total"], Decimal("25000"))
        self.assertEqual(response.context["confirmed_total"], Decimal("20000"))
        self.assertEqual(response.context["pending_total"], Decimal("5000"))
        self.assertEqual(response.context["payment_count"], 2)
        self.assertContains(response, "Ada Obi")
        self.assertNotContains(response, "Chidi Eze")

    def test_another_day_by_date(self):
        self.pay(self.chidi, Decimal("9000"), date_paid=self.yesterday)
        self.client.force_login(self.owner)
        response = self.client.get(self.url, {"date": self.yesterday.isoformat()})
        self.assertEqual(response.context["collected_total"], Decimal("9000"))
        self.assertFalse(response.context["is_today"])

    def test_a_bad_or_future_date_falls_back_to_today(self):
        self.client.force_login(self.owner)
        for value in ("not-a-date", (self.today + timedelta(days=5)).isoformat()):
            with self.subTest(value=value):
                response = self.client.get(self.url, {"date": value})
                self.assertEqual(response.context["day"], self.today)
                self.assertIsNone(response.context["next_day"])

    def test_voided_payments_are_listed_but_not_counted(self):
        payment = self.pay(self.ada, Decimal("20000"), date_paid=self.today)
        payment.void(by=self.owner, reason="Duplicate")
        self.client.force_login(self.owner)
        response = self.client.get(self.url)
        self.assertEqual(response.context["collected_total"], Decimal("0"))
        self.assertContains(response, "Ada Obi")

    def test_it_is_split_by_method_and_by_who_recorded(self):
        self.pay(self.ada, Decimal("20000"), recorded_by=self.bursar, date_paid=self.today)
        self.pay(self.bola, Decimal("7000"), recorded_by=self.principal, date_paid=self.today)
        self.client.force_login(self.owner)
        context = self.client.get(self.url).context
        self.assertEqual(sum(row["total"] for row in context["by_method"]), Decimal("27000"))
        self.assertEqual({row["label"] for row in context["by_recorder"]},
                         {"north.bursar", "north.principal"})

    def test_a_campus_sees_only_its_own_money(self):
        self.pay(self.ada, Decimal("20000"), date_paid=self.today)
        self.client.force_login(self.south_bursar)
        response = self.client.get(self.url)
        self.assertEqual(response.context["collected_total"], Decimal("0"))
        self.assertNotContains(response, "Ada Obi")

    def test_signed_out_visitors_are_sent_to_sign_in(self):
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_the_overview_tile_is_daily_payments(self):
        self.client.force_login(self.bursar)
        request = self.client.get(reverse("core:dashboard"), follow=True).wsgi_request
        tiles = quick_access(request)
        labels = [tile["label"] for tile in tiles]
        self.assertIn("Daily payments", labels)
        self.assertNotIn("Record payment", labels)
        daily = next(tile for tile in tiles if tile["label"] == "Daily payments")
        self.assertEqual(daily["href"], self.url)
