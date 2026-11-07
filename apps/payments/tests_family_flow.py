"""The bursar's family payment flow: receive, share, correct and print."""

from __future__ import annotations

import re
import shutil
import tempfile
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.core.roles import Role
from apps.families.models import Family
from apps.schools.models import SchoolModule
from apps.students import fees
from apps.students.models import Student

from . import allocation
from .models import Payment, PaymentStatus, PaymentTransaction, TransactionKind
from .tests_allocation import build_other_school, build_school

D = Decimal


def other_school():
    school = build_other_school().school
    SchoolModule.set_state(school, "families", True)
    return {
        "family": Family.all_objects.create(school=school, name="Elsewhere"),
        "owner": User.objects.create_user(
            "other-owner", password="pw", role=Role.SCHOOL_OWNER, school=school
        ),
    }


class FlowTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        for key, value in build_school().items():
            setattr(cls, key, value)
        SchoolModule.set_state(cls.school, "families", True)

    def receive(self, amount="150000", **extra):
        extra.setdefault("family", self.family)
        extra.setdefault("branch", self.north)
        return allocation.receive(
            amount=D(amount), date_paid=timezone.localdate(), method="transfer",
            actor=self.bursar, **extra,
        )

    def owed(self, student):
        return fees.position_for(Student.all_objects.get(pk=student.pk)).total_owed

    def details(self, **extra):
        data = {
            "family": self.family.pk,
            "amount": "150000",
            "date_paid": timezone.localdate().isoformat(),
            "method": "transfer",
            "reference": "T-1",
            "payer_name": "Mrs Obi",
            "confirm_now": "on",
        }
        data.update(extra)
        return data

    def record(self, **extra):
        return self.client.post(reverse("payments:shared_record"), self.details(**extra))

    def url(self, txn):
        return reverse("payments:shared_detail", args=[txn.pk])

    def share(self, txn, shares, **extra):
        """Post the sharing form as a person would, from the page they just loaded."""
        form = self.client.get(self.url(txn)).context["share_form"]
        data = {"seen": form["seen"].value(), "label": "school_fees"}
        data.update({f"share_{student.pk}": amount for student, amount in shares.items()})
        data.update(extra)
        return self.client.post(self.url(txn), data)


class ReceivingTests(FlowTestCase):
    def test_a_bursar_records_money_and_goes_on_to_share_it(self):
        self.client.force_login(self.bursar)

        response = self.record()

        txn = PaymentTransaction.all_objects.get(kind=TransactionKind.FAMILY)
        self.assertRedirects(response, self.url(txn))
        self.assertEqual(txn.amount, D("150000"))
        self.assertEqual(txn.family, self.family)
        self.assertEqual(txn.branch, self.north)
        self.assertEqual(txn.payer_name, "Mrs Obi")
        self.assertEqual(txn.status, PaymentStatus.CONFIRMED)
        self.assertEqual(txn.recorded_by, self.bursar)
        self.assertEqual(txn.confirmed_by, self.bursar)

    def test_recording_changes_no_childs_balance(self):
        self.client.force_login(self.bursar)
        self.record()
        self.assertEqual(self.owed(self.ada), D("50000"))
        self.assertFalse(Payment.all_objects.exists())

    def test_it_can_be_held_as_pending(self):
        self.client.force_login(self.bursar)
        self.record(confirm_now="")
        txn = PaymentTransaction.all_objects.get()
        self.assertEqual(txn.status, PaymentStatus.PENDING)
        self.assertIsNone(txn.confirmed_by)

    def test_pressing_twice_records_it_once(self):
        self.client.force_login(self.bursar)
        self.record()
        response = self.record()
        self.assertEqual(PaymentTransaction.all_objects.count(), 1)
        self.assertRedirects(response, self.url(PaymentTransaction.all_objects.get()))

    def test_a_different_payment_from_the_same_family_is_recorded(self):
        self.client.force_login(self.bursar)
        self.record()
        self.record(amount="20000", reference="T-2")
        self.assertEqual(PaymentTransaction.all_objects.count(), 2)

    def test_campus_staff_cannot_record_at_another_campus(self):
        self.client.force_login(self.bursar)
        self.record(branch=self.south.pk)
        self.assertEqual(PaymentTransaction.all_objects.get().branch, self.north)

    def test_a_school_owner_says_which_campus_took_it(self):
        self.client.force_login(self.owner)

        response = self.record()
        self.assertEqual(response.status_code, 200)
        self.assertIn("branch", response.context["form"].errors)
        self.assertFalse(PaymentTransaction.all_objects.exists())

        self.record(branch=self.south.pk)
        self.assertEqual(PaymentTransaction.all_objects.get().branch, self.south)

    def test_nothing_or_less_is_refused(self):
        self.client.force_login(self.bursar)
        for amount in ("0", "-5"):
            response = self.record(amount=amount)
            self.assertEqual(response.status_code, 200)
        self.assertFalse(PaymentTransaction.all_objects.exists())

    def test_another_schools_family_cannot_be_named(self):
        other = other_school()
        self.client.force_login(self.bursar)
        response = self.record(family=other["family"].pk)
        self.assertEqual(response.status_code, 200)
        self.assertIn("family", response.context["form"].errors)

    def test_arriving_from_a_family_fills_it_in(self):
        self.client.force_login(self.bursar)
        response = self.client.get(
            reverse("payments:shared_record"), {"family": self.family.pk}
        )
        self.assertEqual(response.context["form"]["family"].value(), self.family.pk)

    def test_it_is_not_available_when_the_school_has_families_off(self):
        SchoolModule.set_state(self.school, "families", False)
        self.client.force_login(self.bursar)
        response = self.client.get(reverse("payments:shared_record"))
        self.assertContains(response, "not switched on", status_code=403)

    def test_individual_payments_still_work_with_families_off(self):
        SchoolModule.set_state(self.school, "families", False)
        self.client.force_login(self.bursar)
        self.assertEqual(self.client.get(reverse("payments:record")).status_code, 200)


class AttachedSlipTests(FlowTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.media = tempfile.mkdtemp()
        cls.override = override_settings(
            MEDIA_ROOT=cls.media,
            STORAGES={
                **settings.STORAGES,
                "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
            },
        )
        cls.override.enable()

    @classmethod
    def tearDownClass(cls):
        cls.override.disable()
        shutil.rmtree(cls.media, ignore_errors=True)
        super().tearDownClass()

    def slip(self, name="slip.png"):
        return SimpleUploadedFile(name, b"\x89PNG fake", content_type="image/png")

    def test_confirming_on_a_slip_needs_someone_to_have_checked_it(self):
        self.client.force_login(self.bursar)
        response = self.record(receipt=self.slip())
        self.assertIn("receipt_checked", response.context["form"].errors)
        self.assertFalse(PaymentTransaction.all_objects.exists())

    def test_a_checked_slip_is_kept_and_served_behind_the_gate(self):
        self.client.force_login(self.bursar)
        self.record(receipt=self.slip(), receipt_checked="on")
        txn = PaymentTransaction.all_objects.get()
        url = reverse("payments:shared_receipt_file", args=[txn.pk])

        self.assertEqual(self.client.get(url).status_code, 200)

        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 302)

    def test_a_file_that_is_not_a_receipt_is_refused(self):
        self.client.force_login(self.bursar)
        response = self.record(
            receipt=SimpleUploadedFile("run.exe", b"MZ", content_type="image/png"),
            receipt_checked="on",
        )
        self.assertIn("receipt", response.context["form"].errors)

    def test_confirming_a_held_slip_needs_the_check_too(self):
        self.client.force_login(self.bursar)
        self.record(receipt=self.slip(), confirm_now="")
        txn = PaymentTransaction.all_objects.get()
        url = reverse("payments:shared_confirm", args=[txn.pk])

        self.client.post(url)
        txn.refresh_from_db()
        self.assertEqual(txn.status, PaymentStatus.PENDING)

        self.client.post(url, {"receipt_checked": "on"})
        txn.refresh_from_db()
        self.assertEqual(txn.status, PaymentStatus.CONFIRMED)


class SharingTests(FlowTestCase):
    def setUp(self):
        self.txn = self.receive()
        self.client.force_login(self.bursar)

    def test_one_payment_is_shared_between_children(self):
        response = self.share(self.txn, {self.ada: "50000", self.bola: "30000"})

        self.assertRedirects(response, self.url(self.txn))
        self.assertEqual(self.owed(self.ada), D("0"))
        self.assertEqual(self.owed(self.bola), D("20000"))
        self.assertEqual(self.txn.unallocated, D("70000"))

    def test_each_child_gets_their_own_receipt_number_on_the_same_payment(self):
        self.share(self.txn, {self.ada: "50000", self.bola: "30000"})
        rows = Payment.all_objects.filter(transaction=self.txn)
        self.assertEqual(rows.count(), 2)
        self.assertEqual(len({row.receipt_number for row in rows}), 2)
        self.assertEqual({row.method for row in rows}, {"transfer"})

    def test_more_than_was_received_saves_nothing(self):
        allocation.allocate(self.txn, self.chidi, D("50000"), actor=self.bursar)
        response = self.share(
            self.txn, {self.ada: "50000", self.bola: "50001"}, allow_credit="on"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.txn.allocated, D("50000"))

    def test_more_than_a_child_owes_asks_first_and_saves_nothing(self):
        response = self.share(self.txn, {self.bola: "10000", self.ada: "60000"})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["offer_credit"])
        self.assertContains(response, "Ada")
        self.assertEqual(self.txn.allocated, D("0"))

    def test_saying_so_keeps_the_difference_as_their_credit(self):
        self.share(self.txn, {self.ada: "60000"}, allow_credit="on")
        position = fees.position_for(Student.all_objects.get(pk=self.ada.pk))
        self.assertEqual(position.credit, D("10000"))

    def test_a_page_opened_before_someone_else_shared_is_refused(self):
        form = self.client.get(self.url(self.txn)).context["share_form"]
        seen = form["seen"].value()
        allocation.allocate(self.txn, self.ada, D("10000"), actor=self.owner)

        response = self.client.post(self.url(self.txn), {
            "seen": seen, "label": "school_fees", f"share_{self.bola.pk}": "10000",
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "changed")
        self.assertEqual(self.txn.allocated, D("10000"))

    def test_pressing_twice_shares_it_once(self):
        form = self.client.get(self.url(self.txn)).context["share_form"]
        data = {
            "seen": form["seen"].value(), "label": "school_fees",
            f"share_{self.ada.pk}": "20000",
        }
        self.client.post(self.url(self.txn), data)
        self.client.post(self.url(self.txn), data)
        self.assertEqual(self.txn.allocated, D("20000"))

    def test_sharing_nothing_is_refused(self):
        response = self.share(self.txn, {})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Payment.all_objects.exists())

    def test_a_negative_share_is_refused(self):
        response = self.share(self.txn, {self.ada: "-5"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Payment.all_objects.exists())

    def test_what_is_left_stays_on_screen(self):
        self.share(self.txn, {self.ada: "50000"})
        response = self.client.get(self.url(self.txn))
        self.assertContains(response, "₦100,000")

    def test_a_voided_payment_offers_no_sharing(self):
        allocation.void_transaction(self.txn, actor=self.principal, reason="Bounced")
        self.assertIsNone(self.client.get(self.url(self.txn)).context["share_form"])

    def test_every_share_is_in_the_audit_log(self):
        self.share(self.txn, {self.ada: "50000"})
        event = AuditEvent.all_objects.get(action="payment.created")
        self.assertEqual(event.actor_id, self.bursar.pk)
        self.assertEqual(event.changes["transaction_id"], [None, self.txn.pk])


class CampusTests(FlowTestCase):
    def setUp(self):
        self.txn = self.receive()

    def test_campus_staff_see_their_own_children_and_a_note(self):
        self.client.force_login(self.bursar)
        response = self.client.get(self.url(self.txn))
        self.assertContains(response, "Ada")
        self.assertNotContains(response, "Sade")
        self.assertContains(response, "children at another campus")

    def test_campus_staff_cannot_share_with_a_child_elsewhere(self):
        self.client.force_login(self.bursar)
        self.share(self.txn, {self.sade: "10000"})
        self.assertFalse(Payment.all_objects.exists())

    def test_a_school_owner_can_share_across_campuses(self):
        self.client.force_login(self.owner)
        self.share(self.txn, {self.ada: "10000", self.sade: "20000"})
        self.assertEqual(self.owed(self.sade), D("30000"))

    def test_a_child_elsewhere_is_named_nowhere_for_campus_staff(self):
        allocation.allocate(self.txn, self.sade, D("20000"), actor=self.owner)
        self.client.force_login(self.bursar)

        for url_name in ("payments:shared_detail", "payments:shared_receipt"):
            response = self.client.get(reverse(url_name, args=[self.txn.pk]))
            self.assertNotContains(response, "Sade")
            self.assertContains(response, "₦20,000")
            self.assertContains(response, "another campus")

    def test_ceiling_counts_them_on_screen_too(self):
        allocation.allocate(self.txn, self.sade, D("50000"), actor=self.owner)
        self.client.force_login(self.bursar)
        response = self.share(
            self.txn, {self.ada: "50000", self.bola: "50000", self.chidi: "1"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Payment.all_objects.filter(student=self.ada).exists())

    def test_another_campus_cannot_open_the_payment(self):
        south_txn = self.receive(branch=self.south)
        self.client.force_login(self.bursar)
        for url_name in (
            "payments:shared_detail", "payments:shared_receipt", "payments:shared_void",
        ):
            with self.subTest(url_name):
                self.client.force_login(self.principal)
                response = self.client.get(reverse(url_name, args=[south_txn.pk]))
                self.assertEqual(response.status_code, 404)

    def test_another_school_cannot_open_the_payment(self):
        other = other_school()
        self.client.force_login(other["owner"])
        self.assertEqual(self.client.get(self.url(self.txn)).status_code, 404)

    def test_a_one_child_payment_is_not_opened_here(self):
        payment = Payment.all_objects.create(
            branch=self.north, student=self.ada, term=self.n_term, amount=D("100")
        )
        self.client.force_login(self.bursar)
        response = self.client.get(self.url(payment.transaction))
        self.assertEqual(response.status_code, 404)


class ConfirmingTests(FlowTestCase):
    def setUp(self):
        self.txn = self.receive(status=PaymentStatus.PENDING)
        self.client.force_login(self.bursar)

    def test_shares_of_pending_money_do_not_count_until_it_is_confirmed(self):
        self.share(self.txn, {self.ada: "50000"})
        self.assertEqual(self.owed(self.ada), D("50000"))

        self.client.post(reverse("payments:shared_confirm", args=[self.txn.pk]))

        self.assertEqual(self.owed(self.ada), D("0"))
        self.txn.refresh_from_db()
        self.assertEqual(self.txn.confirmed_by, self.bursar)

    def test_its_history_reads_like_a_person_wrote_it(self):
        self.client.post(reverse("payments:shared_confirm", args=[self.txn.pk]))
        response = self.client.get(self.url(self.txn))
        labels = [event.action_label for event in response.context["history"]]
        self.assertEqual(labels, ["Confirmed", "Recorded"])

    def test_confirming_needs_a_post(self):
        response = self.client.get(reverse("payments:shared_confirm", args=[self.txn.pk]))
        self.assertEqual(response.status_code, 405)

    def test_waiting_money_is_flagged_on_the_pending_queue(self):
        response = self.client.get(reverse("payments:pending"))
        self.assertContains(response, "waiting to be checked")


class CorrectingTests(FlowTestCase):
    def setUp(self):
        self.txn = self.receive()
        self.ada_share = allocation.allocate(self.txn, self.ada, D("50000"), actor=self.bursar)

    def test_a_bursar_cannot_void_or_move(self):
        self.client.force_login(self.bursar)
        for url_name, pk in (
            ("payments:shared_void", self.txn.pk),
            ("payments:payment_void", self.ada_share.pk),
            ("payments:allocation_move", self.ada_share.pk),
        ):
            with self.subTest(url_name):
                self.assertEqual(
                    self.client.get(reverse(url_name, args=[pk])).status_code, 403
                )

    def test_voiding_a_share_returns_the_money_to_the_payment(self):
        self.client.force_login(self.principal)
        self.client.post(
            reverse("payments:payment_void", args=[self.ada_share.pk]),
            {"reason": "Wrong child", "confirm": "on"},
        )
        self.assertEqual(self.txn.unallocated, D("150000"))
        self.assertEqual(self.owed(self.ada), D("50000"))

    def test_moving_a_share_is_a_void_and_a_new_share(self):
        self.client.force_login(self.principal)

        self.client.post(
            reverse("payments:allocation_move", args=[self.ada_share.pk]),
            {"to_student": self.bola.pk, "reason": "Wrong child"},
        )

        self.ada_share.refresh_from_db()
        self.assertEqual(self.ada_share.status, PaymentStatus.VOID)
        self.assertEqual(self.ada_share.void_reason, "Wrong child")
        self.assertEqual(self.owed(self.ada), D("50000"))
        self.assertEqual(self.owed(self.bola), D("0"))
        self.assertEqual(self.txn.unallocated, D("100000"))
        self.assertTrue(AuditEvent.all_objects.filter(action="payment.voided").exists())

    def test_a_move_needs_a_reason(self):
        self.client.force_login(self.principal)
        response = self.client.post(
            reverse("payments:allocation_move", args=[self.ada_share.pk]),
            {"to_student": self.bola.pk, "reason": ""},
        )
        self.assertEqual(response.status_code, 200)
        self.ada_share.refresh_from_db()
        self.assertEqual(self.ada_share.status, PaymentStatus.CONFIRMED)

    def test_a_move_that_would_overpay_asks_first_and_changes_nothing(self):
        allocation.allocate(self.txn, self.bola, D("50000"), actor=self.bursar)
        self.client.force_login(self.principal)
        url = reverse("payments:allocation_move", args=[self.ada_share.pk])

        response = self.client.post(url, {"to_student": self.bola.pk, "reason": "Wrong"})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["offer_credit"])
        self.ada_share.refresh_from_db()
        self.assertEqual(self.ada_share.status, PaymentStatus.CONFIRMED)

        self.client.post(
            url, {"to_student": self.bola.pk, "reason": "Wrong", "allow_credit": "on"}
        )
        self.ada_share.refresh_from_db()
        self.assertEqual(self.ada_share.status, PaymentStatus.VOID)

    def test_a_child_outside_the_family_cannot_be_chosen(self):
        self.client.force_login(self.principal)
        response = self.client.post(
            reverse("payments:allocation_move", args=[self.ada_share.pk]),
            {"to_student": self.stranger.pk, "reason": "Wrong"},
        )
        self.assertEqual(response.status_code, 200)
        self.ada_share.refresh_from_db()
        self.assertEqual(self.ada_share.status, PaymentStatus.CONFIRMED)

    def test_a_one_child_payment_has_nothing_to_move(self):
        payment = Payment.all_objects.create(
            branch=self.north, student=self.ada, term=self.n_term, amount=D("100")
        )
        self.client.force_login(self.principal)
        response = self.client.get(reverse("payments:allocation_move", args=[payment.pk]))
        self.assertEqual(response.status_code, 404)

    def test_voiding_the_whole_payment_takes_every_share(self):
        self.client.force_login(self.principal)
        self.client.post(
            reverse("payments:shared_void", args=[self.txn.pk]),
            {"reason": "Bounced", "confirm": "on"},
        )
        self.txn.refresh_from_db()
        self.assertEqual(self.txn.status, PaymentStatus.VOID)
        self.assertEqual(self.txn.void_reason, "Bounced")
        self.assertEqual(self.owed(self.ada), D("50000"))
        self.assertEqual(self.txn.unallocated, D("0"))

    def test_voiding_the_whole_payment_needs_a_reason(self):
        self.client.force_login(self.principal)
        self.client.post(
            reverse("payments:shared_void", args=[self.txn.pk]), {"reason": "", "confirm": "on"}
        )
        self.txn.refresh_from_db()
        self.assertEqual(self.txn.status, PaymentStatus.CONFIRMED)

    def test_a_share_points_back_to_the_payment_and_cannot_be_edited(self):
        self.client.force_login(self.bursar)
        response = self.client.get(self.ada_share.get_absolute_url())
        self.assertContains(response, self.url(self.txn))
        self.assertContains(response, self.txn.number)
        self.assertNotContains(
            response, reverse("payments:payment_update", args=[self.ada_share.pk])
        )


class QueueTests(FlowTestCase):
    def queue(self, **query):
        return self.client.get(reverse("payments:shared_list"), query)

    def test_money_with_something_left_is_listed_with_the_total(self):
        waiting = self.receive("150000")
        allocation.allocate(waiting, self.ada, D("50000"), actor=self.bursar)
        self.client.force_login(self.bursar)

        response = self.queue()

        self.assertEqual(list(response.context["transactions"]), [waiting])
        self.assertEqual(response.context["unallocated_total"], D("100000"))
        self.assertContains(response, "₦100,000")

    def test_fully_shared_and_voided_money_leaves_the_queue(self):
        done = self.receive("50000")
        allocation.allocate(done, self.ada, D("50000"), actor=self.bursar)
        void = self.receive("10000")
        allocation.void_transaction(void, actor=self.principal, reason="Bounced")
        self.client.force_login(self.bursar)

        self.assertEqual(list(self.queue().context["transactions"]), [])
        self.assertEqual(len(self.queue(show="all").context["transactions"]), 2)

    def test_pending_money_stays_in_the_queue_even_when_fully_shared(self):
        waiting = self.receive("50000", status=PaymentStatus.PENDING)
        allocation.allocate(waiting, self.ada, D("50000"), actor=self.bursar)
        self.client.force_login(self.bursar)
        self.assertEqual(list(self.queue().context["transactions"]), [waiting])

    def test_a_share_at_another_campus_still_counts_as_used(self):
        done = self.receive("50000")
        allocation.allocate(done, self.sade, D("50000"), actor=self.owner)
        self.client.force_login(self.bursar)
        self.assertEqual(list(self.queue().context["transactions"]), [])

    def test_campus_staff_see_their_campus_and_an_owner_sees_all(self):
        north = self.receive(branch=self.north)
        south = self.receive("140000", branch=self.south)

        self.client.force_login(self.bursar)
        self.assertEqual(list(self.queue().context["transactions"]), [north])

        self.client.force_login(self.owner)
        self.assertEqual(set(self.queue().context["transactions"]), {north, south})

    def test_it_can_be_narrowed_to_one_family(self):
        self.receive()
        self.client.force_login(self.bursar)
        self.assertEqual(self.queue(family=self.family.pk + 999).context["transactions"].count(), 0)

    def test_one_child_payments_are_not_listed(self):
        Payment.all_objects.create(
            branch=self.north, student=self.ada, term=self.n_term, amount=D("100")
        )
        self.client.force_login(self.bursar)
        self.assertEqual(self.queue(show="all").context["transactions"].count(), 0)

    def test_the_payments_page_says_when_money_is_waiting(self):
        self.client.force_login(self.bursar)
        index = reverse("payments:index")
        self.assertNotContains(self.client.get(index), "not yet shared")

        self.receive()
        response = self.client.get(index)
        self.assertContains(response, "not yet shared")
        self.assertContains(response, "₦150,000")

    def test_it_is_in_the_sidebar_only_when_families_are_on(self):
        self.client.force_login(self.bursar)
        link = reverse("payments:shared_list")
        self.assertContains(self.client.get(reverse("payments:index")), link)

        SchoolModule.set_state(self.school, "families", False)
        self.assertNotContains(self.client.get(reverse("payments:index")), link)


class FamilyPageTests(FlowTestCase):
    def test_it_offers_to_record_a_payment_and_lists_them(self):
        txn = self.receive()
        self.client.force_login(self.bursar)
        response = self.client.get(self.family.get_absolute_url())
        self.assertContains(
            response, reverse("payments:shared_record") + f"?family={self.family.pk}"
        )
        self.assertContains(response, self.url(txn))

    def test_the_record_form_points_to_a_family_payment(self):
        self.client.force_login(self.bursar)
        self.assertContains(
            self.client.get(reverse("payments:record")), reverse("payments:shared_record")
        )


class ReceiptTests(FlowTestCase):
    def setUp(self):
        self.txn = self.receive(payer_name="Mrs Obi", reference="T-77")
        self.ada_share = allocation.allocate(self.txn, self.ada, D("50000"), actor=self.bursar)
        allocation.allocate(self.txn, self.bola, D("30000"), actor=self.bursar)
        self.client.force_login(self.bursar)

    def test_one_sheet_shows_the_sum_and_how_it_was_shared(self):
        response = self.client.get(reverse("payments:shared_receipt", args=[self.txn.pk]))
        self.assertContains(response, self.txn.number)
        self.assertContains(response, "Mrs Obi")
        self.assertContains(response, "The Obis")
        self.assertContains(response, "₦150,000")
        self.assertContains(response, self.ada_share.receipt_number)
        self.assertContains(response, "Bola")
        self.assertContains(response, "₦70,000")

    def test_a_voided_share_is_not_on_the_sheet(self):
        allocation.void_allocation(self.ada_share, actor=self.principal, reason="Wrong")
        response = self.client.get(reverse("payments:shared_receipt", args=[self.txn.pk]))
        self.assertNotContains(response, self.ada_share.receipt_number)

    def test_a_voided_payment_says_not_to_issue_it(self):
        allocation.void_transaction(self.txn, actor=self.principal, reason="Bounced")
        response = self.client.get(reverse("payments:shared_receipt", args=[self.txn.pk]))
        self.assertContains(response, "Do not issue")

    def test_a_childs_own_receipt_says_it_is_part_of_a_larger_payment(self):
        response = self.client.get(reverse("payments:receipt", args=[self.ada_share.pk]))
        self.assertContains(response, self.txn.number)
        self.assertContains(response, "₦150,000")


class HeaderLabelTests(TestCase):
    def test_no_button_shrinks_to_a_fragment_on_a_phone(self):
        """"Edit all" must not read "all", nor "One class" read "class", on a phone."""
        fragment = re.compile(
            r'<span class="hidden sm:inline">[^<]*</span>\s*(?:[^<\s]|</a>|</button>)'
        )
        offenders = []
        for path in Path(settings.BASE_DIR, "templates").rglob("*.html"):
            if fragment.search(path.read_text(encoding="utf-8")):
                offenders.append(path.name)
        self.assertEqual(offenders, [])
