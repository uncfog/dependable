"""The record of who changed money and access, which nobody gets to rewrite."""

from __future__ import annotations

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.urls import reverse
from django.utils import timezone

from apps.academics.models import Class, Level
from apps.core.roles import Role
from apps.core.tenancy import scope_to
from apps.fees.models import FeeComponent, FeeStructure, Term, TermSequence
from apps.payments.models import Payment
from apps.schools import teardown
from apps.schools.models import Branch, School, SchoolModule, SchoolStatus
from apps.students.models import Sex, Student

from .context import AuditContextMiddleware
from .models import AuditEvent, Category
from django.test import RequestFactory, TestCase

User = get_user_model()


class AuditTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = School.all_objects.create(name="Alpha", status=SchoolStatus.ACTIVE)
        cls.north = Branch.all_objects.create(school=cls.school, name="North")
        cls.south = Branch.all_objects.create(school=cls.school, name="South")
        cls.klass = Class.all_objects.create(
            branch=cls.north, name="JSS 1", level=Level.JUNIOR_SECONDARY, year_in_level=1
        )
        cls.term = Term.all_objects.create(
            branch=cls.north, name="First", academic_year="2026/2027",
            sequence=TermSequence.FIRST, is_current=True,
        )
        cls.student = Student.all_objects.create(
            branch=cls.north, school_class=cls.klass, admission_number="A1",
            first_name="Ada", last_name="Obi", sex=Sex.FEMALE, parent_name="P",
            parent_phone="08031234567",
        )
        cls.owner = User.objects.create_user(
            "owner", password="pw", role=Role.SCHOOL_OWNER, school=cls.school
        )
        cls.principal = User.objects.create_user(
            "principal", password="pw", role=Role.PRINCIPAL, school=cls.school,
            branch=cls.north,
        )
        cls.bursar = User.objects.create_user(
            "bursar", password="pw", role=Role.BURSAR, school=cls.school, branch=cls.north
        )
        cls.platform = User.objects.create_user(
            "platform", password="pw", role=Role.PLATFORM_OWNER
        )

    def setUp(self):
        AuditEvent.all_objects.all()._raw_delete(using="default")

    def pay(self, amount="1000", **extra):
        return Payment.all_objects.create(
            branch=self.north, student=self.student, term=self.term,
            amount=Decimal(amount), **extra,
        )

    def events(self, action=None):
        rows = AuditEvent.all_objects.order_by("pk")
        return rows.filter(action=action) if action else rows

    def payment_form(self, **overrides):
        data = {
            "student": "A1", "amount": "1000", "label": "school_fees", "method": "cash",
            "date_paid": timezone.localdate().isoformat(), "reference": "", "note": "",
            "confirm_now": "on",
        }
        data.update(overrides)
        return data


class MoneyIsRecordedTests(AuditTestCase):
    def test_recording_a_payment_says_who_and_how_much(self):
        self.client.force_login(self.bursar)
        self.client.post(reverse("payments:record"), self.payment_form())

        event = self.events("payment.created").get()
        payment = Payment.all_objects.get()
        self.assertEqual(event.actor, self.bursar)
        self.assertEqual(event.actor_name, "bursar")
        self.assertEqual(event.school, self.school)
        self.assertEqual(event.branch, self.north)
        self.assertEqual(event.category, Category.MONEY)
        self.assertEqual(event.target_type, "payments.Payment")
        self.assertEqual(event.target_id, str(payment.pk))
        self.assertEqual(event.target_label, payment.receipt_number)
        self.assertEqual(event.changes["amount"], [None, "1000.00"])
        self.assertEqual(event.source, "web")

    def test_correcting_the_amount_keeps_both_figures(self):
        payment = self.pay()
        self.client.force_login(self.principal)
        self.client.post(
            reverse("payments:payment_update", args=[payment.pk]),
            self.payment_form(amount="400"),
        )

        event = self.events("payment.changed").get()
        self.assertEqual(event.actor, self.principal)
        self.assertEqual(event.changes["amount"], ["1000.00", "400.00"])

    def test_confirming_has_its_own_action(self):
        payment = self.pay(status="pending")
        payment.confirm(by=self.bursar)

        self.assertEqual(
            self.events("payment.confirmed").get().changes["status"],
            ["pending", "confirmed"],
        )

    def test_voiding_records_the_reason(self):
        payment = self.pay()
        self.client.force_login(self.principal)
        self.client.post(
            reverse("payments:payment_void", args=[payment.pk]),
            {"reason": "Wrong child", "confirm": "on"},
        )

        event = self.events("payment.voided").get()
        self.assertEqual(event.actor, self.principal)
        self.assertEqual(event.changes["void_reason"], ["", "Wrong child"])

    def test_going_back_to_pending_is_called_what_it_is(self):
        payment = self.pay()
        self.client.force_login(self.principal)
        data = self.payment_form()
        data.pop("confirm_now")
        self.client.post(reverse("payments:payment_update", args=[payment.pk]), data)

        self.assertTrue(self.events("payment.unconfirmed").exists())

    def test_saving_without_a_change_records_nothing(self):
        payment = self.pay()
        before = self.events().count()

        payment.save()

        self.assertEqual(self.events().count(), before)

    def test_a_fee_change_is_recorded(self):
        structure = FeeStructure.all_objects.create(school_class=self.klass, term=self.term)
        component = FeeComponent.all_objects.create(
            fee_structure=structure, name="Fees", amount=Decimal("50000")
        )
        component.amount = Decimal("60000")
        component.save()

        self.assertEqual(
            self.events("feecomponent.changed").get().changes["amount"],
            ["50000.00", "60000.00"],
        )

    def test_a_change_outside_a_request_is_marked_as_the_system(self):
        self.pay()

        event = self.events("payment.created").get()
        self.assertIsNone(event.actor)
        self.assertEqual(event.source, "system")

    def test_a_change_in_the_admin_is_marked_as_the_admin(self):
        request = RequestFactory().post(reverse("admin:index") + "schools/school/1/change/")
        request.user = User.objects.create_superuser("root", password="pw")

        def change_the_status(request):
            school = School.all_objects.get(pk=self.school.pk)
            school.status = SchoolStatus.SUSPENDED
            school.save()

        AuditContextMiddleware(change_the_status)(request)

        event = self.events("school.changed").get()
        self.assertEqual(event.source, "admin")
        self.assertEqual(event.actor, request.user)
        self.assertEqual(event.changes["status"], ["active", "suspended"])


class AccessIsRecordedTests(AuditTestCase):
    def test_a_role_change_is_recorded(self):
        self.bursar.role = Role.PRINCIPAL
        self.bursar.save()

        event = self.events("user.changed").get()
        self.assertEqual(event.category, Category.ACCESS)
        self.assertEqual(event.changes["role"], ["bursar", "principal"])
        self.assertEqual(event.school, self.school)
        self.assertEqual(event.target_label, "bursar")

    def test_deactivating_an_account_is_recorded(self):
        self.bursar.is_active = False
        self.bursar.save()

        self.assertEqual(
            self.events("user.changed").get().changes["is_active"], [True, False]
        )

    def test_a_password_change_is_noted_but_never_stored(self):
        old_hash = self.bursar.password
        self.bursar.set_password("a-new-password-41")
        self.bursar.save()

        event = self.events("user.changed").get()
        self.assertEqual(event.changes["password"], ["(hidden)", "(hidden)"])
        self.assertNotIn(old_hash, str(event.changes))
        self.assertNotIn(self.bursar.password, str(event.changes))

    def test_signing_in_is_not_an_event(self):
        self.client.post(reverse("login"), {"username": "bursar", "password": "pw"})

        self.assertFalse(self.events("user.changed").exists())

    def test_switching_a_module_is_recorded(self):
        SchoolModule.all_objects.create(school=self.school, key="messaging", enabled=True)

        self.assertEqual(
            self.events("schoolmodule.created").get().category, Category.ACCESS
        )

    def test_a_platform_account_has_no_school(self):
        self.platform.is_active = False
        self.platform.save()

        self.assertIsNone(self.events("user.changed").get().school)


class NobodyRewritesItTests(AuditTestCase):
    def setUp(self):
        super().setUp()
        self.pay()
        self.event = self.events().first()

    def test_a_saved_event_cannot_be_saved_again(self):
        self.event.action = "tampered"
        with self.assertRaises(PermissionDenied):
            self.event.save()

    def test_it_cannot_be_deleted(self):
        with self.assertRaises(PermissionDenied):
            self.event.delete()

    def test_it_cannot_be_deleted_in_bulk(self):
        with self.assertRaises(PermissionDenied):
            AuditEvent.all_objects.all().delete()

    def test_it_cannot_be_updated_in_bulk(self):
        with self.assertRaises(PermissionDenied):
            AuditEvent.all_objects.update(action="tampered")

    def test_it_outlives_the_person_who_did_it(self):
        self.client.force_login(self.bursar)
        self.client.post(reverse("payments:record"), self.payment_form(amount="77"))
        Payment.all_objects.filter(recorded_by=self.bursar).delete()

        self.bursar.delete()

        event = AuditEvent.all_objects.filter(actor_name="bursar").first()
        self.assertIsNotNone(event)
        self.assertIsNone(event.actor)

    def test_it_outlives_a_closed_campus_row(self):
        branch = Branch.all_objects.create(school=self.school, name="Closing")
        User.objects.create_user(
            "temp", password="pw", role=Role.BURSAR, school=self.school, branch=branch
        )
        event = self.events("user.created").get()

        branch.delete()

        event.refresh_from_db()
        self.assertIsNone(event.branch)

    def test_the_admin_offers_no_way_to_change_it(self):
        admin = User.objects.create_superuser("root", password="pw")
        self.client.force_login(admin)

        self.assertEqual(self.client.get(reverse("admin:audit_auditevent_changelist")).status_code, 200)
        self.assertEqual(
            self.client.post(
                reverse("admin:audit_auditevent_delete", args=[self.event.pk]),
                {"post": "yes"},
            ).status_code,
            403,
        )
        self.assertTrue(AuditEvent.all_objects.filter(pk=self.event.pk).exists())

    def test_closing_a_school_takes_its_events_with_it_and_finishes(self):
        teardown.remove(self.school)

        self.assertFalse(School.all_objects.filter(pk=self.school.pk).exists())
        self.assertFalse(AuditEvent.all_objects.filter(school_id=self.school.pk).exists())


class WhoCanReadItTests(AuditTestCase):
    def setUp(self):
        super().setUp()
        self.payment = self.pay()
        other = School.all_objects.create(name="Bravo")
        self.other_user = User.objects.create_user(
            "other", password="pw", role=Role.SCHOOL_OWNER, school=other
        )

    def test_another_school_sees_nothing(self):
        with scope_to(school_id=self.other_user.school_id, role=Role.SCHOOL_OWNER):
            self.assertFalse(
                AuditEvent.objects.filter(target_id=str(self.payment.pk)).exists()
            )

    def test_another_campus_sees_nothing(self):
        with scope_to(school_id=self.school.pk, branch_id=self.south.pk, role=Role.PRINCIPAL):
            self.assertFalse(
                AuditEvent.objects.filter(target_id=str(self.payment.pk)).exists()
            )

    def test_the_owner_sees_it(self):
        with scope_to(school_id=self.school.pk, role=Role.SCHOOL_OWNER):
            self.assertTrue(
                AuditEvent.objects.filter(target_id=str(self.payment.pk)).exists()
            )


class PaymentHistoryScreenTests(AuditTestCase):
    def test_the_payment_page_shows_what_happened_to_it(self):
        payment = self.pay(amount="1000")
        self.client.force_login(self.principal)
        self.client.post(
            reverse("payments:payment_update", args=[payment.pk]),
            self.payment_form(amount="400"),
        )

        response = self.client.get(reverse("payments:payment_detail", args=[payment.pk]))

        self.assertContains(response, "History")
        self.assertContains(response, "1000.00")
        self.assertContains(response, "400.00")
        self.assertContains(response, "principal")
