"""One all-in enrolment payment, divided between the intake and term ledgers.

The pilot's case: a family pays the admission fee, the intake items and the
first term's fees in one lump. The term's share must land on the student -- in
their payment history, in the dashboard's recent payments, against their
balance -- while the intake share stays admission money and out of the term's
collection figure.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.urls import reverse
from django.utils import timezone

from apps.core.finance import collection_summary
from apps.fees.models import FeeComponent, FeeStructure, Term, TermSequence
from apps.payments.models import Payment, PaymentStatus
from apps.students import fees

from . import enrolment, split
from .models import (
    AdmissionFeeItem,
    AdmissionFeeKind,
    AdmissionFeeSchedule,
    AdmissionPayment,
    AdmissionPaymentStatus,
    ApplicantStatus,
)
from .tests import AdmissionsTestCase

D = Decimal


def bill(admission="3000", term="61000", one_offs="53000", *, has_term=True):
    """A bill built by hand, for the arithmetic tests."""
    return split.Bill(
        schedule=None,
        term=object() if has_term else None,
        admission_lines=(split.BillLine("Admission Fee", D(admission)),),
        one_off_lines=(split.BillLine("One-offs", D(one_offs)),),
        term_lines=(split.BillLine("School Fees", D(term)),) if has_term else (),
    )


class AllocationOrderTests(AdmissionsTestCase):
    """Admission fee, then term fees, then one-off items, then credit."""

    def test_the_pilot_case_clears_the_term(self):
        result = split.allocate(D("115000"), bill())
        self.assertEqual(result.admission_fee, D("3000"))
        self.assertEqual(result.term_fees, D("61000"))
        self.assertEqual(result.one_offs, D("51000"))
        self.assertEqual(result.excess, D("0"))
        self.assertEqual(result.total, D("115000"))

    def test_a_small_payment_goes_to_the_admission_fee_first(self):
        result = split.allocate(D("2000"), bill())
        self.assertEqual((result.admission_fee, result.term), (D("2000"), D("0")))

    def test_term_fees_come_before_the_one_off_items(self):
        result = split.allocate(D("40000"), bill())
        self.assertEqual(result.admission_fee, D("3000"))
        self.assertEqual(result.term_fees, D("37000"))
        self.assertEqual(result.one_offs, D("0"))

    def test_an_overpayment_stays_on_the_intake_side(self):
        result = split.allocate(D("130000"), bill())
        self.assertEqual(result.term, D("61000"))
        self.assertEqual(result.excess, D("13000"))
        self.assertEqual(result.intake, D("69000"))

    def test_no_current_term_means_nothing_is_term_money(self):
        result = split.allocate(D("100000"), bill(has_term=False))
        self.assertEqual(result.term, D("0"))
        self.assertEqual(result.intake, D("100000"))


class SplitTestCase(AdmissionsTestCase):
    """KG 2 at North, priced as the pilot is: 56,000 at the door, 61,000 a term."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.term = Term.all_objects.create(
            branch=cls.north, name="First Term 2026/2027", academic_year="2026/2027",
            sequence=TermSequence.FIRST, is_current=True,
        )
        structure = FeeStructure.all_objects.create(
            school_class=cls.north_kg2, term=cls.term
        )
        for name, amount in (("School Fees", 24000), ("Textbooks", 35000),
                             ("Development", 2000)):
            FeeComponent.all_objects.create(
                fee_structure=structure, name=name, amount=D(amount)
            )
        schedule = AdmissionFeeSchedule.all_objects.create(
            school_class=cls.north_kg2, academic_year="2026/2027"
        )
        for name, amount, kind in (
            ("Admission Fee", 3000, AdmissionFeeKind.ADMISSION),
            ("Uniform", 9000, AdmissionFeeKind.INTAKE),
            ("Sportswear", 16000, AdmissionFeeKind.INTAKE),
            ("Exam / Dossier", 3000, AdmissionFeeKind.INTAKE),
            ("Track suit", 25000, AdmissionFeeKind.INTAKE),
        ):
            AdmissionFeeItem.all_objects.create(
                schedule=schedule, name=name, amount=D(amount), kind=kind
            )

    def offered(self, **overrides):
        data = {"school_class": self.north_kg2, "status": ApplicantStatus.OFFERED}
        data.update(overrides)
        return self.make_applicant(**data)

    def pay_at_the_door(self, applicant, amount, *, days_ago=0,
                        status=AdmissionPaymentStatus.CONFIRMED):
        return AdmissionPayment.all_objects.create(
            applicant=applicant, amount=D(amount), status=status,
            received_on=timezone.localdate() - timedelta(days=days_ago),
        )

    def enrol(self, applicant, number="FA/2026/003"):
        return enrolment.enrol(
            applicant, admission_number=number, actor=self.north_principal
        )


class BillTests(SplitTestCase):
    def test_a_new_kg2_childs_bill_counts_nothing_twice(self):
        result = split.bill_for(self.north_kg2)
        self.assertEqual(result.admission_fee, D("3000"))
        self.assertEqual(result.one_offs, D("53000"))
        self.assertEqual(result.term_fees, D("61000"))
        self.assertEqual(result.total, D("117000"))


class SettleAtEnrolmentTests(SplitTestCase):
    def test_the_term_share_becomes_a_payment_on_the_student(self):
        applicant = self.offered()
        door = self.pay_at_the_door(applicant, "117000")
        student = self.enrol(applicant)

        term_payment = Payment.all_objects.get(student=student)
        self.assertEqual(term_payment.amount, D("61000"))
        self.assertEqual(term_payment.term, self.term)
        self.assertEqual(term_payment.status, PaymentStatus.CONFIRMED)
        self.assertEqual(term_payment.school_id, self.alpha.pk)
        self.assertEqual(term_payment.branch_id, self.north.pk)

        door.refresh_from_db()
        self.assertEqual(door.amount, D("56000"))
        self.assertEqual(door.term_payment, term_payment)
        # One sum handed over, one receipt number on both halves.
        self.assertEqual(door.receipt_number, term_payment.receipt_number)

    def test_it_reduces_the_term_balance_to_nothing(self):
        applicant = self.offered()
        self.pay_at_the_door(applicant, "117000")
        student = self.enrol(applicant)

        position = fees.position_for(student)
        self.assertEqual(position.expected, D("61000"))
        self.assertEqual(position.paid, D("61000"))
        self.assertEqual(position.outstanding, D("0"))

    def test_intake_money_stays_out_of_the_terms_collections(self):
        applicant = self.offered()
        self.pay_at_the_door(applicant, "117000")
        self.enrol(applicant)

        self.assertEqual(collection_summary(self.term)["collected_total"], D("61000"))
        applicant.refresh_from_db()
        self.assertEqual(applicant.admission_paid, D("56000"))

    def test_it_appears_in_the_payment_history_and_recent_payments(self):
        applicant = self.offered()
        self.pay_at_the_door(applicant, "117000")
        student = self.enrol(applicant)
        term_payment = Payment.all_objects.get(student=student)

        self.client.force_login(self.north_principal)
        history = self.client.get(
            reverse("payments:student_history", args=[student.pk])
        )
        self.assertContains(history, term_payment.receipt_number)

        dashboard = self.client.get(reverse("core:branch_dashboard"))
        self.assertIn(
            term_payment.pk, [p.pk for p in dashboard.context["recent_payments"]]
        )

    def test_the_receipt_shows_the_whole_sum_and_how_it_was_applied(self):
        applicant = self.offered()
        self.pay_at_the_door(applicant, "117000")
        student = self.enrol(applicant)
        term_payment = Payment.all_objects.get(student=student)

        self.client.force_login(self.north_principal)
        response = self.client.get(reverse("payments:receipt", args=[term_payment.pk]))
        self.assertEqual(response.context["received_total"], D("117000"))
        self.assertEqual(response.context["intake_portion"], D("56000"))
        self.assertContains(response, "Admission fee and intake items")

    def test_money_is_taken_from_the_newest_rows_first(self):
        applicant = self.offered()
        older = self.pay_at_the_door(applicant, "3000", days_ago=5)
        newer = self.pay_at_the_door(applicant, "114000")
        self.enrol(applicant)

        older.refresh_from_db()
        newer.refresh_from_db()
        self.assertEqual(older.amount, D("3000"))
        self.assertIsNone(older.term_payment_id)
        self.assertEqual(newer.amount, D("53000"))

    def test_a_row_moved_in_full_is_voided_not_zeroed(self):
        applicant = self.offered()
        older = self.pay_at_the_door(applicant, "56000", days_ago=5)
        newer = self.pay_at_the_door(applicant, "61000")
        self.enrol(applicant)

        newer.refresh_from_db()
        older.refresh_from_db()
        self.assertEqual(newer.status, AdmissionPaymentStatus.VOID)
        self.assertEqual(newer.amount, D("61000"))
        self.assertIn("moved to term fees", newer.note)
        self.assertEqual(older.amount, D("56000"))
        self.assertEqual(older.status, AdmissionPaymentStatus.CONFIRMED)

    def test_a_part_payment_pays_the_admission_fee_then_the_term(self):
        applicant = self.offered()
        self.pay_at_the_door(applicant, "40000")
        student = self.enrol(applicant)
        self.assertEqual(
            Payment.all_objects.get(student=student).amount, D("37000")
        )
        applicant.refresh_from_db()
        self.assertEqual(applicant.admission_paid, D("3000"))

    def test_a_pending_receipt_is_not_split(self):
        applicant = self.offered()
        self.pay_at_the_door(applicant, "3000")
        self.pay_at_the_door(
            applicant, "114000", status=AdmissionPaymentStatus.PENDING
        )
        student = self.enrol(applicant)
        self.assertFalse(Payment.all_objects.filter(student=student).exists())

    def test_with_no_current_term_the_money_stays_admission_money(self):
        Term.all_objects.filter(pk=self.term.pk).update(is_current=False)
        applicant = self.offered()
        self.pay_at_the_door(applicant, "117000")
        student = self.enrol(applicant)
        self.assertFalse(Payment.all_objects.filter(student=student).exists())
        applicant.refresh_from_db()
        self.assertEqual(applicant.admission_paid, D("117000"))
