"""In-app notifications and reminders.

Built on the payments fixtures: Alpha Schools with North and South campuses, an
owner, North's principal and bursar, and South's bursar.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
from io import StringIO
from decimal import Decimal

from django.core.management import call_command
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.audit import context as audit_context
from apps.audit.recording import record
from apps.audit.models import AuditEvent, Category
from apps.core.roles import Role
from apps.payments.models import Payment, PaymentStatus
from apps.payments.tests import PaymentsTestCase

from .models import ALWAYS_ON, Kind, Notification, NotificationPreference, Reminder
from .reminders import run


@contextmanager
def acting_as(user):
    """What the audit middleware does for a request: name who is acting."""
    request = RequestFactory().post("/payments/record/")
    request.user = user
    token = audit_context._request.set(request)
    try:
        yield
    finally:
        audit_context._request.reset(token)


def told(user, kind=None):
    rows = Notification.objects.filter(recipient=user)
    return rows.filter(kind=kind) if kind else rows


class NotificationCase(PaymentsTestCase):
    def setUp(self):
        Notification.objects.all().delete()

    def record_pending(self, by, student=None, amount="20000"):
        with acting_as(by):
            return self.pay(student or self.ada, Decimal(amount), status=PaymentStatus.PENDING,
                            recorded_by=by)


class PaymentNotificationTests(NotificationCase):
    def test_a_pending_payment_tells_the_campus_and_the_owner_but_not_the_recorder(self):
        payment = self.record_pending(self.bursar)
        for person in (self.owner, self.principal):
            with self.subTest(person=person.username):
                notice = told(person, Kind.PAYMENT_WAITING).get()
                self.assertIn("Ada Obi", notice.title)
                self.assertIn("₦20,000", notice.title)
                self.assertEqual(notice.url, reverse("payments:payment_detail", args=[payment.pk]))
        self.assertFalse(told(self.bursar).exists())
        # Another campus hears nothing about North's money.
        self.assertFalse(told(self.south_bursar).exists())

    def test_one_payment_is_told_once_not_once_per_audit_event(self):
        self.record_pending(self.bursar)
        self.assertEqual(told(self.owner).count(), 1)

    def test_checking_it_tells_whoever_recorded_it(self):
        payment = self.record_pending(self.bursar)
        Notification.objects.all().delete()
        with acting_as(self.principal):
            payment.confirm(by=self.principal)
        self.assertEqual(told(self.bursar, Kind.PAYMENT_CHECKED).count(), 1)
        self.assertEqual(told(self.owner, Kind.PAYMENT_CHECKED).count(), 1)
        self.assertFalse(told(self.principal).exists())

    def test_voiding_says_why(self):
        payment = self.record_pending(self.bursar)
        with acting_as(self.owner):
            payment.void(by=self.owner, reason="Duplicate slip")
        notice = told(self.bursar, Kind.PAYMENT_VOIDED).get()
        self.assertIn("Duplicate slip", notice.body)

    def test_a_payment_recorded_already_checked_tells_the_whole_campus(self):
        with acting_as(self.bursar):
            self.pay(self.ada, Decimal("5000"), recorded_by=self.bursar)
        for person in (self.owner, self.principal):
            with self.subTest(person=person.username):
                self.assertEqual(told(person, Kind.PAYMENT_RECORDED).count(), 1)
        self.assertFalse(told(self.bursar).exists())
        self.assertFalse(told(self.south_bursar).exists())

    def test_a_muted_kind_is_not_written(self):
        NotificationPreference.objects.create(user=self.owner, muted=[Kind.PAYMENT_WAITING])
        self.record_pending(self.bursar)
        self.assertFalse(told(self.owner).exists())
        self.assertTrue(told(self.principal).exists())

    def test_security_cannot_be_muted(self):
        pref = NotificationPreference(user=self.owner, muted=list(ALWAYS_ON))
        for kind in ALWAYS_ON:
            self.assertFalse(pref.is_muted(kind))

    def test_a_switched_off_account_is_not_told(self):
        self.principal.is_active = False
        self.principal.save()
        Notification.objects.all().delete()
        self.record_pending(self.bursar)
        self.assertFalse(told(self.principal).exists())

    def test_a_broken_rule_never_undoes_the_payment(self):
        from . import rules

        def explode(event):
            raise RuntimeError("boom")

        original = rules.RULES["payment.created"]
        rules.RULES["payment.created"] = explode
        try:
            with self.assertLogs("apps.notifications.rules", level="ERROR"):
                payment = self.record_pending(self.bursar)
        finally:
            rules.RULES["payment.created"] = original
        self.assertTrue(Payment.all_objects.filter(pk=payment.pk).exists())


class AccessNotificationTests(NotificationCase):
    def test_a_role_change_tells_the_owner_and_the_person(self):
        with acting_as(self.owner):
            self.bursar.role = Role.PRINCIPAL
            self.bursar.save()
        self.assertIn("now Principal", told(self.bursar, Kind.STAFF).get().title)
        self.assertFalse(told(self.owner).exists())  # the owner did it

    def test_a_new_staff_account_tells_the_owner(self):
        from django.contrib.auth import get_user_model

        with acting_as(self.principal):
            get_user_model().objects.create_user(
                "new.bursar", password="pw", role=Role.BURSAR,
                school=self.alpha, branch=self.north,
            )
        self.assertIn("new.bursar", told(self.owner, Kind.STAFF).get().title)
        # The campus meets its new colleague; the other campus does not.
        self.assertIn("new.bursar", told(self.bursar, Kind.STAFF).get().title)
        self.assertFalse(told(self.south_bursar).exists())

    def test_a_recovery_code_tells_the_person_even_though_they_used_it(self):
        with acting_as(self.bursar):
            record(action="mfa.recovery_code_used", category=Category.ACCESS,
                   target_type="accounts.User", target_id=self.bursar.pk,
                   target_label=self.bursar.username, school_id=self.alpha.pk)
        self.assertEqual(told(self.bursar, Kind.SECURITY).count(), 1)
        self.assertEqual(told(self.owner, Kind.SECURITY).count(), 1)

    def test_every_role_at_the_campus_is_told_and_nobody_elsewhere(self):
        from django.contrib.auth import get_user_model

        User = get_user_model()
        second_bursar = User.objects.create_user(
            "north.bursar2", password="pw", role=Role.BURSAR,
            school=self.alpha, branch=self.north,
        )
        switched_off = User.objects.create_user(
            "north.gone", password="pw", role=Role.BURSAR,
            school=self.alpha, branch=self.north, is_active=False,
        )
        Notification.objects.all().delete()
        self.record_pending(self.bursar)
        for person in (self.owner, self.principal, second_bursar):
            with self.subTest(person=person.username):
                self.assertEqual(told(person, Kind.PAYMENT_WAITING).count(), 1)
        for person in (self.bursar, switched_off, self.south_bursar):
            with self.subTest(left_out=person.username):
                self.assertFalse(told(person).exists())

    def test_checking_and_voiding_tell_the_whole_campus(self):
        payment = self.record_pending(self.bursar)
        Notification.objects.all().delete()
        with acting_as(self.owner):
            payment.void(by=self.owner, reason="Duplicate slip")
        for person in (self.bursar, self.principal):
            with self.subTest(person=person.username):
                self.assertEqual(told(person, Kind.PAYMENT_VOIDED).count(), 1)
        self.assertFalse(told(self.owner).exists())
        self.assertFalse(told(self.south_bursar).exists())

    def test_an_import_tells_everyone_at_the_school(self):
        with acting_as(self.principal):
            record(action="importbatch.committed", category=Category.ACCESS,
                   target_type="onboarding.ImportBatch", target_id=7,
                   target_label="records.xlsx", school_id=self.alpha.pk)
        for person in (self.owner, self.bursar, self.south_bursar):
            with self.subTest(person=person.username):
                self.assertIn("records.xlsx", told(person, Kind.IMPORT).get().title)
        self.assertFalse(told(self.principal).exists())

    def test_an_import_tells_the_owner(self):
        with acting_as(self.principal):
            record(action="importbatch.committed", category=Category.ACCESS,
                   target_type="onboarding.ImportBatch", target_id=7,
                   target_label="records.xlsx", school_id=self.alpha.pk)
        self.assertIn("records.xlsx", told(self.owner, Kind.IMPORT).get().title)

    def test_platform_events_with_no_school_tell_nobody(self):
        record(action="mfa.reset", category=Category.ACCESS, target_type="accounts.User",
               target_id=self.owner.pk, school_id=None)
        self.assertFalse(Notification.objects.exists())


class ReminderRunTests(NotificationCase):
    def age(self, payment, hours):
        Payment.all_objects.filter(pk=payment.pk).update(
            created_at=timezone.now() - timedelta(hours=hours)
        )

    def test_payments_left_a_day_remind_the_campus(self):
        self.age(self.record_pending(self.bursar), 30)
        self.age(self.record_pending(self.bursar, self.bola), 30)
        self.record_pending(self.bursar, self.chidi)  # fresh: not counted
        Notification.objects.all().delete()
        run()
        notice = told(self.bursar, Kind.REMINDER_UNCHECKED).get()
        self.assertIn("2 payments have waited", notice.title)
        self.assertEqual(notice.url, reverse("payments:pending"))
        self.assertTrue(told(self.principal, Kind.REMINDER_UNCHECKED).exists())
        self.assertFalse(told(self.south_bursar).exists())

    def test_running_twice_in_a_day_tells_nobody_twice(self):
        self.age(self.record_pending(self.bursar), 30)
        run()
        run()
        call_command("run_reminders", stdout=StringIO())
        self.assertEqual(told(self.bursar, Kind.REMINDER_UNCHECKED).count(), 1)

    def test_fees_due_in_a_week_with_money_owed(self):
        today = timezone.localdate()
        self.term.due_date = today + timedelta(days=7)
        self.term.save()
        run(today)
        notice = told(self.owner, Kind.REMINDER_DUE).get()
        self.assertIn(self.term.name, notice.title)
        self.assertIn("still owe", notice.body)

    def test_a_personal_reminder_arrives_on_its_day_and_only_once(self):
        today = timezone.localdate()
        Reminder.objects.create(
            user=self.bursar, school=self.alpha, due_on=today + timedelta(days=1),
            note="Call Mrs Obi", target_type="student", target_id=self.ada.pk,
            target_label="Ada Obi", url=reverse("students:student_detail", args=[self.ada.pk]),
        )
        run(today)
        self.assertFalse(told(self.bursar, Kind.REMINDER_PERSONAL).exists())
        run(today + timedelta(days=1))
        run(today + timedelta(days=2))
        notice = told(self.bursar, Kind.REMINDER_PERSONAL).get()
        self.assertEqual(notice.body, "Call Mrs Obi")
        self.assertFalse(told(self.owner, Kind.REMINDER_PERSONAL).exists())

    def test_old_notifications_are_cleared(self):
        self.record_pending(self.bursar)
        Notification.objects.update(created_at=timezone.now() - timedelta(days=91))
        run()
        self.assertFalse(Notification.objects.filter(
            created_at__lt=timezone.now() - timedelta(days=90)).exists())


class ScreenTests(NotificationCase):
    def test_the_bell_shows_the_unread_count(self):
        self.record_pending(self.bursar)
        self.client.force_login(self.owner)
        body = self.client.get(reverse("payments:index")).content.decode()
        self.assertIn('data-bell', body)
        self.assertIn("Notifications, 1 unread", body)

    def test_opening_marks_read_and_follows_the_link(self):
        payment = self.record_pending(self.bursar)
        notice = told(self.owner).get()
        self.client.force_login(self.owner)
        response = self.client.get(reverse("notifications:open", args=[notice.pk]))
        self.assertRedirects(response, reverse("payments:payment_detail", args=[payment.pk]),
                             fetch_redirect_response=False)
        notice.refresh_from_db()
        self.assertIsNotNone(notice.read_at)

    def test_nobody_can_open_someone_elses(self):
        self.record_pending(self.bursar)
        notice = told(self.owner).get()
        self.client.force_login(self.south_bursar)
        response = self.client.get(reverse("notifications:open", args=[notice.pk]))
        self.assertEqual(response.status_code, 404)
        notice.refresh_from_db()
        self.assertIsNone(notice.read_at)

    def test_a_link_out_of_the_app_is_never_followed(self):
        notice = Notification.objects.create(
            recipient=self.owner, kind=Kind.STAFF, title="x", url="https://evil.example/")
        self.client.force_login(self.owner)
        response = self.client.get(reverse("notifications:open", args=[notice.pk]))
        self.assertRedirects(response, reverse("notifications:index"),
                             fetch_redirect_response=False)

    def test_mark_all_read_is_only_mine(self):
        self.record_pending(self.bursar)
        self.client.force_login(self.owner)
        self.client.post(reverse("notifications:mark_all_read"))
        self.assertFalse(told(self.owner).filter(read_at__isnull=True).exists())
        self.assertTrue(told(self.principal).filter(read_at__isnull=True).exists())

    def test_the_page_and_the_panel_list_mine(self):
        self.record_pending(self.bursar)
        self.client.force_login(self.owner)
        for name in ("notifications:index", "notifications:panel"):
            with self.subTest(page=name):
                body = self.client.get(reverse(name)).content.decode()
                self.assertIn("Ada Obi", body)
        self.client.force_login(self.south_bursar)
        self.assertNotIn("Ada Obi", self.client.get(reverse("notifications:index")).content.decode())

    def test_settings_turn_a_kind_off_but_never_security(self):
        self.client.force_login(self.owner)
        self.client.post(reverse("notifications:settings"), {"on": [Kind.STAFF]})
        pref = NotificationPreference.objects.get(user=self.owner)
        self.assertIn(Kind.PAYMENT_WAITING, pref.muted)
        self.assertNotIn(Kind.STAFF, pref.muted)
        self.assertNotIn(Kind.SECURITY, pref.muted)

    def test_remind_me_on_a_student(self):
        self.client.force_login(self.bursar)
        due = timezone.localdate() + timedelta(days=3)
        response = self.client.post(reverse("notifications:remind"), {
            "target_type": "student", "target_id": self.ada.pk,
            "due_on": due.isoformat(), "note": "Balance call",
        })
        self.assertEqual(response.status_code, 302)
        reminder = Reminder.objects.get(user=self.bursar)
        self.assertEqual(reminder.target_label, "Ada Obi")
        self.assertEqual(reminder.due_on, due)

    def test_remind_me_cannot_reach_another_campus(self):
        self.client.force_login(self.south_bursar)
        response = self.client.post(reverse("notifications:remind"), {
            "target_type": "student", "target_id": self.ada.pk,
            "due_on": timezone.localdate().isoformat(),
        })
        self.assertEqual(response.status_code, 404)
        self.assertFalse(Reminder.objects.exists())

    def test_remind_me_refuses_a_past_day(self):
        self.client.force_login(self.bursar)
        self.client.post(reverse("notifications:remind"), {
            "target_type": "student", "target_id": self.ada.pk,
            "due_on": (timezone.localdate() - timedelta(days=1)).isoformat(),
        })
        self.assertFalse(Reminder.objects.exists())

    def test_the_remind_card_is_on_the_student_and_payment_pages(self):
        payment = self.record_pending(self.bursar)
        self.client.force_login(self.bursar)
        for url in (reverse("students:student_detail", args=[self.ada.pk]),
                    reverse("payments:payment_detail", args=[payment.pk])):
            with self.subTest(url=url):
                self.assertContains(self.client.get(url), "Remind me")

    def test_cancelling_is_only_for_my_own(self):
        reminder = Reminder.objects.create(
            user=self.owner, school=self.alpha, due_on=timezone.localdate(),
            target_type="student", target_id=self.ada.pk, target_label="Ada Obi")
        self.client.force_login(self.bursar)
        response = self.client.post(reverse("notifications:reminder_cancel", args=[reminder.pk]))
        self.assertEqual(response.status_code, 404)
        self.assertTrue(Reminder.objects.filter(pk=reminder.pk).exists())

    def test_the_pages_are_private_to_crawlers(self):
        from apps.core.seo import PRIVATE_PATHS

        self.assertIn("/notifications/", PRIVATE_PATHS)

    def test_audit_events_are_not_changed_by_notifying(self):
        before = AuditEvent.all_objects.count()
        self.record_pending(self.bursar)
        # The payment's own two events, and nothing written by notifications.
        self.assertEqual(AuditEvent.all_objects.count(), before + 2)


# --- Web Push -------------------------------------------------------------------

import base64
import json
from unittest import mock

from django.test import override_settings

from . import push
from .models import PushSubscription

FCM = "https://fcm.googleapis.com/fcm/send/abc123"


def _keys():
    out = StringIO()
    call_command("vapid_keys", stdout=out)
    pairs = dict(line.split("=", 1) for line in out.getvalue().splitlines() if "=" in line)
    return pairs["VAPID_PUBLIC_KEY"], pairs["VAPID_PRIVATE_KEY"]


PUBLIC, PRIVATE = _keys()
PUSH_ON = override_settings(
    WEBPUSH_VAPID_PUBLIC_KEY=PUBLIC, WEBPUSH_VAPID_PRIVATE_KEY=PRIVATE,
    WEBPUSH_VAPID_SUBJECT="mailto:test@example.test", PWA_ENABLED=True,
)


class FakeResponse:
    def __init__(self, status):
        self.status_code = status


def in_place(function, *args):
    return function(*args)


@PUSH_ON
class PushTests(NotificationCase):
    def subscribe(self, user, endpoint=FCM):
        return PushSubscription.objects.create(
            user=user, endpoint=endpoint, p256dh="p" * 87, auth="a" * 22)

    def subscription_json(self, endpoint=FCM):
        return json.dumps({"endpoint": endpoint, "keys": {"p256dh": "p" * 87, "auth": "a" * 22}})

    def test_the_key_command_makes_a_usable_pair(self):
        raw = base64.urlsafe_b64decode(PUBLIC + "=" * (-len(PUBLIC) % 4))
        self.assertEqual(len(raw), 65)  # an uncompressed P-256 point, as browsers want
        self.assertEqual(raw[0], 4)

    def test_a_notification_is_pushed_in_full_to_the_recipients_devices(self):
        self.subscribe(self.owner)
        sent = []
        with mock.patch.object(push, "_run", in_place), \
             mock.patch("pywebpush.webpush", side_effect=lambda **kw: sent.append(kw)):
            with self.captureOnCommitCallbacks(execute=True):
                self.record_pending(self.bursar)
        self.assertEqual(len(sent), 1)
        body = json.loads(sent[0]["data"])
        # Full details on the lock screen, as the owner chose.
        self.assertIn("Ada Obi", body["title"])
        self.assertIn("₦20,000", body["title"])
        notice = told(self.owner).get()
        self.assertEqual(body["url"], reverse("notifications:open", args=[notice.pk]))
        self.assertEqual(sent[0]["subscription_info"]["endpoint"], FCM)
        self.assertEqual(sent[0]["vapid_claims"], {"sub": "mailto:test@example.test"})

    def test_nothing_is_sent_without_keys(self):
        self.subscribe(self.owner)
        with override_settings(WEBPUSH_VAPID_PRIVATE_KEY=""), \
             mock.patch("pywebpush.webpush") as webpush:
            with self.captureOnCommitCallbacks(execute=True):
                self.record_pending(self.bursar)
        webpush.assert_not_called()

    def test_a_gone_device_is_forgotten(self):
        from pywebpush import WebPushException

        sub = self.subscribe(self.owner)
        error = WebPushException("gone", response=FakeResponse(410))
        with mock.patch.object(push, "_run", in_place), \
             mock.patch("pywebpush.webpush", side_effect=error):
            with self.captureOnCommitCallbacks(execute=True):
                self.record_pending(self.bursar)
        self.assertFalse(PushSubscription.objects.filter(pk=sub.pk).exists())

    def test_a_failing_push_service_never_undoes_the_notification(self):
        self.subscribe(self.owner)
        with mock.patch.object(push, "_run", in_place), \
             mock.patch("pywebpush.webpush", side_effect=RuntimeError("down")), \
             self.assertLogs("apps.notifications.push", level="ERROR"):
            with self.captureOnCommitCallbacks(execute=True):
                self.record_pending(self.bursar)
        self.assertEqual(told(self.owner).count(), 1)

    def test_turning_a_device_on(self):
        self.client.force_login(self.bursar)
        response = self.client.post(reverse("notifications:push_subscribe"),
                                    self.subscription_json(), content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(PushSubscription.objects.get().user, self.bursar)

    def test_only_real_push_services_are_accepted(self):
        self.client.force_login(self.bursar)
        for endpoint in ("https://evil.example/collect", "http://fcm.googleapis.com/x",
                         "https://127.0.0.1/x", "https://fcm.googleapis.com.evil.example/x"):
            with self.subTest(endpoint=endpoint):
                response = self.client.post(reverse("notifications:push_subscribe"),
                                            self.subscription_json(endpoint),
                                            content_type="application/json")
                self.assertEqual(response.status_code, 400)
        self.assertFalse(PushSubscription.objects.exists())
        for good in (FCM, "https://updates.push.services.mozilla.com/wpush/v2/x",
                     "https://web.push.apple.com/x",
                     "https://wns2-par02p.notify.windows.com/w/?token=x"):
            self.assertTrue(push.allowed_endpoint(good), good)

    def test_a_shared_browser_moves_to_whoever_signed_in(self):
        self.subscribe(self.owner)
        self.client.force_login(self.bursar)
        self.client.post(reverse("notifications:push_subscribe"), self.subscription_json(),
                         content_type="application/json")
        self.assertEqual(PushSubscription.objects.get().user, self.bursar)

    def test_signing_out_stops_this_browser(self):
        self.client.force_login(self.bursar)
        self.client.post(reverse("notifications:push_subscribe"), self.subscription_json(),
                         content_type="application/json")
        self.subscribe(self.bursar, "https://fcm.googleapis.com/fcm/send/other-phone")
        self.client.post(reverse("logout"))
        remaining = list(PushSubscription.objects.values_list("endpoint", flat=True))
        self.assertEqual(remaining, ["https://fcm.googleapis.com/fcm/send/other-phone"])

    def test_turning_off_is_only_mine(self):
        self.subscribe(self.owner)
        self.client.force_login(self.bursar)
        self.client.post(reverse("notifications:push_unsubscribe"),
                         json.dumps({"endpoint": FCM}), content_type="application/json")
        self.assertTrue(PushSubscription.objects.filter(user=self.owner).exists())

    def test_the_service_worker_shows_and_opens_them(self):
        body = self.client.get("/sw.js").content.decode()
        self.assertIn("addEventListener('push'", body)
        self.assertIn("showNotification", body)
        # And tells any open page, so it pops the notice up in the app at once.
        self.assertIn("schoolcord:notification", body)
        self.assertIn("postMessage", body)
        self.assertIn("addEventListener('notificationclick'", body)
        self.assertIn("target.origin !== self.location.origin", body)

    def test_the_settings_page_offers_the_switch(self):
        self.client.force_login(self.owner)
        body = self.client.get(reverse("notifications:settings")).content.decode()
        self.assertIn('data-push-ready="yes"', body)
        self.assertIn(PUBLIC, body)
        self.assertNotIn(PRIVATE, body)
        self.assertIn("names and amounts included", body)

    def test_without_keys_the_switch_says_so(self):
        self.client.force_login(self.owner)
        with override_settings(WEBPUSH_VAPID_PUBLIC_KEY=""):
            body = self.client.get(reverse("notifications:settings")).content.decode()
        self.assertIn('data-push-ready="no"', body)
        self.assertIn("Not available on this site yet", body)


class LiveTests(NotificationCase):
    """What an open page asks for, so it can pop a new notice up without a reload."""

    def live(self, user, after=0):
        self.client.force_login(user)
        return self.client.get(reverse("notifications:live"), {"after": after})

    def test_it_returns_only_what_is_newer_and_unread(self):
        self.record_pending(self.bursar)
        first = told(self.principal).get()
        data = self.live(self.principal, after=0).json()
        self.assertEqual(data["unread"], 1)
        self.assertEqual([i["id"] for i in data["items"]], [first.pk])
        self.assertEqual(data["items"][0]["url"], reverse("notifications:open", args=[first.pk]))
        self.assertIn("Ada Obi", data["items"][0]["title"])
        again = self.live(self.principal, after=data["latest"]).json()
        self.assertEqual(again["items"], [])
        self.assertEqual(again["unread"], 1)

    def test_it_never_shows_anyone_elses(self):
        self.record_pending(self.bursar)
        data = self.live(self.south_bursar).json()
        self.assertEqual((data["unread"], data["items"]), (0, []))

    def test_the_recorder_gets_nothing_live_for_their_own_payment(self):
        self.record_pending(self.bursar)
        self.assertEqual(self.live(self.bursar).json()["items"], [])

    def test_a_bad_after_is_treated_as_nothing_seen(self):
        self.record_pending(self.bursar)
        self.assertEqual(len(self.live(self.principal, after="x").json()["items"]), 1)

    def test_signed_out_is_sent_to_sign_in_not_given_json(self):
        response = self.client.get(reverse("notifications:live"))
        self.assertEqual(response.status_code, 302)

    def test_it_is_never_cached(self):
        self.assertEqual(self.live(self.owner)["Cache-Control"], "no-store")

    def test_the_bell_carries_the_newest_id_so_old_ones_do_not_pop_up(self):
        self.record_pending(self.bursar)
        newest = told(self.principal).get().pk
        self.client.force_login(self.principal)
        page = self.client.get(reverse("notifications:index")).content.decode()
        self.assertIn(f'data-latest="{newest}"', page)
        self.assertIn(f'data-live-url="{reverse("notifications:live")}"', page)
