"""One sum of money received, shared across children.

``Payment`` stays what it always was: one child's money, and the only thing balances
read. A ``PaymentTransaction`` above it says how much actually came in. Nothing here
should be able to make those two disagree, or hand out more than was received.
"""

from __future__ import annotations

import threading
from decimal import Decimal
from importlib import import_module
from io import StringIO

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection
from django.db.models import ProtectedError
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature
from django.urls import reverse
from django.utils import timezone

from apps.academics.models import Class, Level
from apps.audit.models import AuditEvent
from apps.core.roles import Role
from apps.core.tenancy import scope_to
from apps.families.models import Family, FamilyMembership
from apps.families.services import link
from apps.fees.models import FeeComponent, FeeStructure, Term, TermSequence
from apps.schools import teardown
from apps.schools.models import Branch, School, SchoolStatus
from apps.students import fees
from apps.students.models import Sex, Student

from . import allocation
from .models import Payment, PaymentStatus, PaymentTransaction, TransactionKind

User = get_user_model()
D = Decimal


def build_school():
    """Two campuses, a family of three at North and a child of theirs at South."""
    world = {}
    school = School.all_objects.create(name="Alpha", status=SchoolStatus.ACTIVE)
    north = Branch.all_objects.create(school=school, name="North")
    south = Branch.all_objects.create(school=school, name="South")
    world.update(school=school, north=north, south=south)
    for tag, branch in (("n", north), ("s", south)):
        klass = Class.all_objects.create(
            branch=branch, name="JSS 1", level=Level.JUNIOR_SECONDARY, year_in_level=1
        )
        term = Term.all_objects.create(
            branch=branch, name="First", academic_year="2026/2027",
            sequence=TermSequence.FIRST, is_current=True,
        )
        structure = FeeStructure.all_objects.create(school_class=klass, term=term)
        FeeComponent.all_objects.create(fee_structure=structure, name="Fees", amount=D("50000"))
        world[f"{tag}_class"] = klass
        world[f"{tag}_term"] = term

    def child(number, first, klass):
        return Student.all_objects.create(
            branch=klass.branch, school_class=klass, admission_number=number,
            first_name=first, last_name="Obi", sex=Sex.FEMALE, parent_name="P",
            parent_phone="08031234567",
        )

    world["ada"] = child("A1", "Ada", world["n_class"])
    world["bola"] = child("A2", "Bola", world["n_class"])
    world["chidi"] = child("A3", "Chidi", world["n_class"])
    world["sade"] = child("S1", "Sade", world["s_class"])
    world["stranger"] = child("A9", "Stranger", world["n_class"])
    world["owner"] = User.objects.create_user(
        "owner", password="pw", role=Role.SCHOOL_OWNER, school=school
    )
    world["principal"] = User.objects.create_user(
        "principal", password="pw", role=Role.PRINCIPAL, school=school, branch=north
    )
    world["bursar"] = User.objects.create_user(
        "bursar", password="pw", role=Role.BURSAR, school=school, branch=north
    )
    family = Family.all_objects.create(school=school, name="The Obis", family_code="F-1")
    for key in ("ada", "bola", "chidi", "sade"):
        link(family, world[key], by=world["owner"])
    world["family"] = family
    return world


class AllocationTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        for key, value in build_school().items():
            setattr(cls, key, value)

    def receive(self, amount="150000", **extra):
        extra.setdefault("family", self.family)
        extra.setdefault("branch", self.north)
        return allocation.receive(
            amount=D(amount), date_paid=timezone.localdate(), method="transfer",
            actor=self.bursar, **extra,
        )

    def owed(self, student):
        return fees.position_for(Student.all_objects.get(pk=student.pk)).total_owed


class EveryPaymentHasATransactionTests(AllocationTestCase):
    def pay(self, amount="20000", **extra):
        return Payment.all_objects.create(
            branch=self.north, student=self.ada, term=self.n_term, amount=D(amount), **extra
        )

    def test_a_payment_made_any_old_way_gets_one(self):
        payment = self.pay(reference="T-1", method="cash")

        txn = payment.transaction
        self.assertEqual(txn.kind, TransactionKind.INDIVIDUAL)
        self.assertEqual(txn.amount, D("20000"))
        self.assertEqual(txn.reference, "T-1")
        self.assertEqual(txn.method, "cash")
        self.assertEqual(txn.school, self.school)
        self.assertEqual(txn.branch, self.north)
        self.assertEqual(txn.unallocated, D("0"))

    def test_the_screen_gets_one_too(self):
        self.client.force_login(self.bursar)
        self.client.post(
            reverse("payments:record"),
            {"student": "A1", "amount": "5000", "label": "school_fees", "method": "cash",
             "date_paid": timezone.localdate().isoformat(), "confirm_now": "on"},
        )

        self.assertEqual(Payment.all_objects.get().transaction.amount, D("5000"))

    def test_correcting_the_amount_keeps_them_agreed(self):
        payment = self.pay()
        payment.amount = D("15000")
        payment.save()

        self.assertEqual(PaymentTransaction.all_objects.get().amount, D("15000"))

    def test_status_moves_together(self):
        payment = self.pay(status=PaymentStatus.PENDING)
        self.assertEqual(payment.transaction.status, PaymentStatus.PENDING)

        payment.confirm(by=self.bursar)
        self.assertEqual(
            PaymentTransaction.all_objects.get().status, PaymentStatus.CONFIRMED
        )
        payment.void(by=self.principal, reason="Mistake")
        self.assertEqual(PaymentTransaction.all_objects.get().status, PaymentStatus.VOID)

    def test_deleting_the_payment_takes_its_own_transaction(self):
        payment = self.pay()

        payment.delete()

        self.assertFalse(PaymentTransaction.all_objects.exists())

    def test_balances_are_exactly_what_they_were(self):
        self.pay()

        self.assertEqual(self.owed(self.ada), D("30000"))


class SharingAnAmountTests(AllocationTestCase):
    def test_one_sum_across_three_children(self):
        txn = self.receive()
        allocation.allocate(txn, self.ada, D("60000"), actor=self.bursar, allow_credit=True)
        allocation.allocate(txn, self.bola, D("50000"), actor=self.bursar)
        allocation.allocate(txn, self.chidi, D("30000"), actor=self.bursar)

        txn.refresh_from_db()
        self.assertEqual(txn.allocated, D("140000"))
        self.assertEqual(txn.unallocated, D("10000"))
        self.assertEqual(self.owed(self.bola), D("0"))
        self.assertEqual(self.owed(self.chidi), D("20000"))
        self.assertEqual(Payment.all_objects.count(), 3)

    def test_each_child_keeps_their_own_row_and_history(self):
        txn = self.receive()
        allocation.allocate(txn, self.bola, D("50000"), actor=self.bursar, label="uniform")

        payment = Payment.all_objects.get(student=self.bola)
        self.assertEqual(payment.transaction, txn)
        self.assertEqual(payment.label, "uniform")
        self.assertEqual(payment.method, "transfer")
        self.assertEqual(payment.term, self.n_term)
        self.assertEqual(payment.recorded_by, self.bursar)
        self.assertEqual(payment.status, PaymentStatus.CONFIRMED)

    def test_you_cannot_share_out_more_than_came_in(self):
        txn = self.receive("70000")
        allocation.allocate(txn, self.ada, D("50000"), actor=self.bursar)

        with self.assertRaises(allocation.ExceedsReceived):
            allocation.allocate(txn, self.bola, D("20000.01"), actor=self.bursar)

        self.assertEqual(Payment.all_objects.count(), 1)
        self.assertEqual(self.owed(self.bola), D("50000"))

    def test_the_whole_amount_can_be_used(self):
        txn = self.receive("70000")
        allocation.allocate(txn, self.ada, D("50000"), actor=self.bursar)
        allocation.allocate(txn, self.bola, D("20000"), actor=self.bursar)

        txn.refresh_from_db()
        self.assertEqual(txn.unallocated, D("0"))

    def test_nothing_or_less_is_refused(self):
        txn = self.receive()
        for amount in ("0", "-5"):
            with self.assertRaises(allocation.AllocationError):
                allocation.allocate(txn, self.ada, D(amount), actor=self.bursar)

    def test_unallocated_money_counts_toward_nobody(self):
        self.receive()

        self.assertEqual(self.owed(self.ada), D("50000"))
        self.assertFalse(Payment.all_objects.exists())

    def test_a_child_outside_the_family_is_refused(self):
        txn = self.receive()

        with self.assertRaises(allocation.NotInFamily):
            allocation.allocate(txn, self.stranger, D("1000"), actor=self.bursar)

    def test_a_child_who_has_left_the_family_is_refused(self):
        FamilyMembership.all_objects.filter(student=self.ada).update(ended_at=timezone.now())
        txn = self.receive()

        with self.assertRaises(allocation.NotInFamily):
            allocation.allocate(txn, self.ada, D("1000"), actor=self.bursar)

    def test_another_schools_child_is_refused(self):
        other = build_other_school()
        txn = allocation.receive(
            amount=D("1000"), date_paid=timezone.localdate(), method="cash",
            actor=self.owner, branch=self.north,
        )

        with self.assertRaises(allocation.AllocationError):
            allocation.allocate(txn, other, D("1000"), actor=self.owner)

    def test_a_transaction_for_one_child_cannot_be_shared(self):
        payment = Payment.all_objects.create(
            branch=self.north, student=self.ada, term=self.n_term, amount=D("1000")
        )

        with self.assertRaises(allocation.AllocationError):
            allocation.allocate(payment.transaction, self.bola, D("1"), actor=self.bursar)


class PayingMoreThanOwedTests(AllocationTestCase):
    def test_it_is_refused_without_confirmation(self):
        txn = self.receive()

        with self.assertRaises(allocation.WouldOverpay) as caught:
            allocation.allocate(txn, self.ada, D("60000"), actor=self.bursar)

        self.assertEqual(caught.exception.owed, D("50000"))
        self.assertFalse(Payment.all_objects.exists())

    def test_with_confirmation_it_becomes_credit(self):
        txn = self.receive()
        allocation.allocate(txn, self.ada, D("60000"), actor=self.bursar, allow_credit=True)

        position = fees.position_for(Student.all_objects.get(pk=self.ada.pk))
        self.assertEqual(position.credit, D("10000"))

    def test_exactly_what_is_owed_needs_no_confirmation(self):
        allocation.allocate(self.receive(), self.ada, D("50000"), actor=self.bursar)

        self.assertEqual(self.owed(self.ada), D("0"))

    def test_money_already_allocated_to_them_counts(self):
        txn = self.receive()
        allocation.allocate(txn, self.ada, D("30000"), actor=self.bursar)

        with self.assertRaises(allocation.WouldOverpay):
            allocation.allocate(txn, self.ada, D("30000"), actor=self.bursar)

    def test_pending_money_allocated_to_them_counts_too(self):
        txn = self.receive(status=PaymentStatus.PENDING)
        allocation.allocate(txn, self.ada, D("30000"), actor=self.bursar)

        with self.assertRaises(allocation.WouldOverpay):
            allocation.allocate(txn, self.ada, D("30000"), actor=self.bursar)


class CampusRulesTests(AllocationTestCase):
    def test_a_bursar_cannot_allocate_to_another_campus(self):
        txn = self.receive()

        with self.assertRaises(allocation.CampusNotAllowed):
            allocation.allocate(txn, self.sade, D("1000"), actor=self.bursar)

    def test_a_principal_cannot_either(self):
        txn = self.receive()

        with self.assertRaises(allocation.CampusNotAllowed):
            allocation.allocate(txn, self.sade, D("1000"), actor=self.principal)

    def test_the_owner_can(self):
        txn = self.receive()
        allocation.allocate(txn, self.sade, D("1000"), actor=self.owner)

        payment = Payment.all_objects.get(student=self.sade)
        self.assertEqual(payment.branch, self.south)
        self.assertEqual(payment.term, self.s_term)


class PendingTransactionTests(AllocationTestCase):
    def test_pending_money_counts_for_nobody_until_confirmed(self):
        txn = self.receive(status=PaymentStatus.PENDING)
        allocation.allocate(txn, self.ada, D("50000"), actor=self.bursar)
        self.assertEqual(self.owed(self.ada), D("50000"))

        allocation.confirm(txn, actor=self.bursar)

        self.assertEqual(self.owed(self.ada), D("0"))
        payment = Payment.all_objects.get()
        self.assertEqual(payment.status, PaymentStatus.CONFIRMED)
        self.assertEqual(payment.confirmed_by, self.bursar)
        txn.refresh_from_db()
        self.assertEqual(txn.status, PaymentStatus.CONFIRMED)

    def test_a_later_allocation_follows_the_transaction(self):
        txn = self.receive(status=PaymentStatus.PENDING)
        allocation.confirm(txn, actor=self.bursar)
        allocation.allocate(txn, self.ada, D("1000"), actor=self.bursar)

        self.assertEqual(Payment.all_objects.get().status, PaymentStatus.CONFIRMED)


class CorrectionsTests(AllocationTestCase):
    def test_voiding_an_allocation_returns_the_money_to_unallocated(self):
        txn = self.receive()
        allocation.allocate(txn, self.ada, D("50000"), actor=self.bursar)

        allocation.void_allocation(
            Payment.all_objects.get(), actor=self.principal, reason="Wrong child"
        )

        txn.refresh_from_db()
        self.assertEqual(txn.unallocated, D("150000"))
        self.assertEqual(txn.status, PaymentStatus.CONFIRMED)
        self.assertEqual(self.owed(self.ada), D("50000"))
        payment = Payment.all_objects.get()
        self.assertEqual(payment.status, PaymentStatus.VOID)
        self.assertEqual(payment.void_reason, "Wrong child")

    def test_a_reason_is_required(self):
        txn = self.receive()
        allocation.allocate(txn, self.ada, D("1000"), actor=self.bursar)

        for reason in ("", "   "):
            with self.assertRaises(allocation.AllocationError):
                allocation.void_allocation(
                    Payment.all_objects.get(), actor=self.principal, reason=reason
                )

    def test_moving_money_to_a_sibling_is_a_logged_void_and_a_new_allocation(self):
        txn = self.receive()
        allocation.allocate(txn, self.ada, D("20000"), actor=self.bursar)
        original = Payment.all_objects.get()

        moved = allocation.reallocate(
            original, self.bola, actor=self.principal, reason="Meant for Bola"
        )

        original.refresh_from_db()
        self.assertEqual(original.status, PaymentStatus.VOID)
        self.assertEqual(moved.student, self.bola)
        self.assertEqual(moved.amount, D("20000"))
        self.assertEqual(self.owed(self.ada), D("50000"))
        self.assertEqual(self.owed(self.bola), D("30000"))
        self.assertTrue(AuditEvent.all_objects.filter(action="payment.voided").exists())

    def test_a_refused_move_changes_nothing(self):
        txn = self.receive()
        allocation.allocate(txn, self.ada, D("20000"), actor=self.bursar)
        original = Payment.all_objects.get()

        with self.assertRaises(allocation.NotInFamily):
            allocation.reallocate(
                original, self.stranger, actor=self.principal, reason="Oops"
            )

        original.refresh_from_db()
        self.assertEqual(original.status, PaymentStatus.CONFIRMED)

    def test_credit_never_moves_to_a_sibling_by_itself(self):
        txn = self.receive()
        allocation.allocate(txn, self.ada, D("60000"), actor=self.bursar, allow_credit=True)

        self.assertEqual(self.owed(self.bola), D("50000"))

    def test_voiding_the_whole_transaction_voids_every_allocation(self):
        txn = self.receive()
        allocation.allocate(txn, self.ada, D("50000"), actor=self.bursar)
        allocation.allocate(txn, self.bola, D("50000"), actor=self.bursar)

        allocation.void_transaction(txn, actor=self.principal, reason="Bounced")

        txn.refresh_from_db()
        self.assertEqual(txn.status, PaymentStatus.VOID)
        self.assertEqual(txn.unallocated, D("0"))
        self.assertEqual(self.owed(self.ada), D("50000"))
        self.assertEqual(
            set(Payment.all_objects.values_list("status", flat=True)), {PaymentStatus.VOID}
        )

    def test_a_voided_transaction_takes_no_more_allocations(self):
        txn = self.receive()
        allocation.void_transaction(txn, actor=self.principal, reason="Bounced")

        with self.assertRaises(allocation.AllocationError):
            allocation.allocate(txn, self.ada, D("1"), actor=self.bursar)


class FamilyAllocationScreenRulesTests(AllocationTestCase):
    def test_a_shared_payment_cannot_be_corrected_as_if_it_stood_alone(self):
        txn = self.receive()
        allocation.allocate(txn, self.ada, D("50000"), actor=self.bursar)
        payment = Payment.all_objects.get()
        self.client.force_login(self.principal)

        response = self.client.post(
            reverse("payments:payment_update", args=[payment.pk]),
            {"student": "A1", "amount": "99999999", "label": "school_fees", "method": "cash",
             "date_paid": timezone.localdate().isoformat(), "confirm_now": "on"},
        )

        self.assertEqual(response.status_code, 302)
        payment.refresh_from_db()
        self.assertEqual(payment.amount, D("50000"))

    def test_the_admin_cannot_change_its_amount(self):
        from apps.payments.admin import PaymentAdmin
        from django.contrib import admin as django_admin
        from django.test import RequestFactory

        txn = self.receive()
        allocation.allocate(txn, self.ada, D("50000"), actor=self.bursar)
        request = RequestFactory().get("/admin/")
        request.user = User.objects.create_superuser("root", password="pw")

        readonly = PaymentAdmin(Payment, django_admin.site).get_readonly_fields(
            request, Payment.all_objects.get()
        )

        self.assertIn("amount", readonly)
        self.assertIn("student", readonly)


class AuditTests(AllocationTestCase):
    def test_the_money_received_and_each_share_are_recorded(self):
        txn = self.receive()
        allocation.allocate(txn, self.ada, D("50000"), actor=self.bursar)

        created = AuditEvent.all_objects.get(action="paymenttransaction.created")
        self.assertEqual(created.changes["amount"], [None, "150000.00"])
        share = AuditEvent.all_objects.get(action="payment.created")
        self.assertEqual(share.changes["transaction_id"], [None, txn.pk])


class ReportTests(AllocationTestCase):
    def report(self):
        self.client.force_login(self.owner)
        return self.client.get(reverse("reports:index"))

    def test_money_not_yet_shared_out_is_shown_and_not_counted_as_collected(self):
        txn = self.receive()
        allocation.allocate(txn, self.ada, D("50000"), actor=self.bursar)

        response = self.report()

        self.assertContains(response, "Not yet allocated")
        self.assertContains(response, "₦100,000")
        collected = next(s for s in response.context["report"].stats if s.label == "Collected")
        self.assertIn("50,000", collected.value.text)

    def test_it_is_absent_when_there_is_none(self):
        self.assertNotContains(self.report(), "Not yet allocated")

    def test_one_family_payment_counts_as_one_payment(self):
        txn = self.receive()
        allocation.allocate(txn, self.ada, D("50000"), actor=self.bursar)
        allocation.allocate(txn, self.bola, D("50000"), actor=self.bursar)

        from apps.reports.reporting import _ledger

        ledger = _ledger([self.north.pk], [self.n_term])
        self.assertEqual(ledger["by_method"][0]["payments"], 1)
        self.assertEqual(ledger["by_method"][0]["amount"], D("100000"))


class BackfillTests(AllocationTestCase):
    def migration(self):
        return import_module("apps.payments.migrations.0003_backfill_transactions")

    def old_payment(self, amount, **extra):
        """A payment as it stands before this ships, with no transaction."""
        payment = Payment.all_objects.create(
            branch=self.north, student=self.ada, term=self.n_term, amount=D(amount), **extra
        )
        txn = payment.transaction
        Payment.all_objects.filter(pk=payment.pk).update(transaction=None)
        PaymentTransaction.all_objects.filter(pk=txn.pk).delete()
        return Payment.all_objects.get(pk=payment.pk)

    def test_each_old_payment_gets_a_matching_transaction(self):
        first = self.old_payment("1000", reference="R1", method="cash", recorded_by=self.bursar)
        void = self.old_payment("2000", status=PaymentStatus.VOID, void_reason="x")

        self.migration().backfill(django_apps, None)

        first.refresh_from_db()
        void.refresh_from_db()
        self.assertEqual(first.transaction.amount, D("1000"))
        self.assertEqual(first.transaction.reference, "R1")
        self.assertEqual(first.transaction.recorded_by, self.bursar)
        self.assertEqual(first.transaction.kind, TransactionKind.INDIVIDUAL)
        self.assertEqual(first.transaction.created_at, first.created_at)
        self.assertEqual(void.transaction.status, PaymentStatus.VOID)

    def test_receipt_numbers_and_balances_do_not_move(self):
        payment = self.old_payment("1000")
        number = payment.receipt_number
        before = self.owed(self.ada)

        self.migration().backfill(django_apps, None)

        payment.refresh_from_db()
        self.assertEqual(payment.receipt_number, number)
        self.assertEqual(self.owed(self.ada), before)

    def test_running_it_twice_adds_nothing(self):
        self.old_payment("1000")

        self.migration().backfill(django_apps, None)
        self.migration().backfill(django_apps, None)

        self.assertEqual(PaymentTransaction.all_objects.count(), 1)


class ReconciliationCommandTests(AllocationTestCase):
    def run_it(self):
        out = StringIO()
        try:
            call_command("check_payment_transactions", stdout=out, stderr=out)
        except SystemExit as stopped:
            return out.getvalue(), stopped.code
        return out.getvalue(), 0

    def test_a_clean_ledger_passes(self):
        Payment.all_objects.create(
            branch=self.north, student=self.ada, term=self.n_term, amount=D("1000")
        )

        output, code = self.run_it()

        self.assertEqual(code, 0)
        self.assertIn("agree", output)

    def test_it_catches_money_shared_out_beyond_what_came_in(self):
        txn = self.receive("1000")
        allocation.allocate(txn, self.ada, D("1000"), actor=self.bursar)
        PaymentTransaction.all_objects.filter(pk=txn.pk).update(amount=D("500"))

        output, code = self.run_it()

        self.assertEqual(code, 1)
        self.assertIn(txn.number, output)

    def test_it_catches_a_payment_with_no_transaction(self):
        payment = Payment.all_objects.create(
            branch=self.north, student=self.ada, term=self.n_term, amount=D("1000")
        )
        Payment.all_objects.filter(pk=payment.pk).update(transaction=None)

        output, code = self.run_it()

        self.assertEqual(code, 1)
        self.assertIn(payment.receipt_number, output)

    def test_it_catches_a_single_transaction_that_disagrees(self):
        payment = Payment.all_objects.create(
            branch=self.north, student=self.ada, term=self.n_term, amount=D("1000")
        )
        PaymentTransaction.all_objects.filter(pk=payment.transaction_id).update(amount=D("9"))

        _, code = self.run_it()

        self.assertEqual(code, 1)


class ClosingASchoolTests(AllocationTestCase):
    def test_a_school_with_shared_payments_can_be_closed(self):
        txn = self.receive()
        allocation.allocate(txn, self.ada, D("50000"), actor=self.bursar)

        teardown.remove(self.school)

        self.assertFalse(School.all_objects.filter(pk=self.school.pk).exists())
        self.assertFalse(PaymentTransaction.all_objects.exists())
        self.assertFalse(Family.all_objects.exists())


class SignedInAtOneCampusTests(AllocationTestCase):
    """Campus staff cannot see another campus's shares, but the money still counts."""

    def setUp(self):
        self.txn = self.receive()
        self.sades = allocation.allocate(self.txn, self.sade, D("50000"), actor=self.owner)

    def at_north(self):
        return scope_to(school_id=self.school.pk, branch_id=self.north.pk, role=Role.BURSAR)

    def test_what_is_left_counts_shares_at_other_campuses(self):
        with self.at_north():
            self.assertEqual(self.txn.allocated, D("50000"))
            self.assertEqual(self.txn.unallocated, D("100000"))

    def test_the_ceiling_counts_shares_at_other_campuses(self):
        with self.at_north():
            with self.assertRaises(allocation.ExceedsReceived):
                allocation.allocate(
                    self.txn, self.ada, D("100001"), actor=self.bursar, allow_credit=True
                )

    def test_voiding_the_payment_takes_every_share_with_it(self):
        with self.at_north():
            allocation.void_transaction(self.txn, actor=self.principal, reason="Bounced")
        self.assertEqual(Payment.all_objects.get(pk=self.sades.pk).status, PaymentStatus.VOID)

    def test_confirming_the_payment_confirms_every_share(self):
        pending = self.receive(status=PaymentStatus.PENDING)
        share = allocation.allocate(
            pending, self.sade, D("10000"), actor=self.owner, allow_credit=True
        )
        with self.at_north():
            allocation.confirm(pending, actor=self.bursar)
        self.assertEqual(Payment.all_objects.get(pk=share.pk).status, PaymentStatus.CONFIRMED)


class TenancyTests(AllocationTestCase):
    def test_another_school_cannot_see_the_transaction(self):
        from apps.core.tenancy import scope_to

        self.receive()
        other = School.all_objects.create(name="Bravo")

        with scope_to(school_id=other.pk, role=Role.SCHOOL_OWNER):
            self.assertFalse(PaymentTransaction.objects.exists())


def build_other_school():
    school = School.all_objects.create(name="Bravo", status=SchoolStatus.ACTIVE)
    branch = Branch.all_objects.create(school=school, name="Main")
    klass = Class.all_objects.create(
        branch=branch, name="JSS 1", level=Level.JUNIOR_SECONDARY, year_in_level=1
    )
    Term.all_objects.create(
        branch=branch, name="First", academic_year="2026/2027",
        sequence=TermSequence.FIRST, is_current=True,
    )
    return Student.all_objects.create(
        branch=branch, school_class=klass, admission_number="B1", first_name="Other",
        last_name="School", sex=Sex.FEMALE, parent_name="P", parent_phone="08031234567",
    )


@skipUnlessDBFeature("has_select_for_update")
class SimultaneousAllocationTests(TransactionTestCase):
    def test_two_people_cannot_share_out_the_same_money(self):
        world = build_school()
        txn = allocation.receive(
            amount=D("50000"), date_paid=timezone.localdate(), method="cash",
            actor=world["bursar"], branch=world["north"], family=world["family"],
        )
        children = [world["ada"], world["bola"], world["chidi"]]
        ready = threading.Barrier(len(children))
        outcomes = []

        def share(child):
            try:
                ready.wait()
                allocation.allocate(
                    PaymentTransaction.all_objects.get(pk=txn.pk), child, D("30000"),
                    actor=world["bursar"],
                )
                outcomes.append("ok")
            except allocation.ExceedsReceived:
                outcomes.append("refused")
            finally:
                connection.close()

        threads = [threading.Thread(target=share, args=(c,)) for c in children]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(sorted(outcomes), ["ok", "refused", "refused"])
        self.assertEqual(PaymentTransaction.all_objects.get(pk=txn.pk).allocated, D("30000"))


class ProtectionTests(AllocationTestCase):
    def test_a_family_with_money_against_it_cannot_be_deleted(self):
        txn = self.receive()
        with self.assertRaises(ProtectedError):
            txn.family.delete()

    def test_a_transaction_with_a_share_cannot_be_deleted(self):
        txn = self.receive()
        allocation.allocate(txn, self.ada, D("50000"), actor=self.bursar)
        with self.assertRaises(ProtectedError):
            txn.delete()
