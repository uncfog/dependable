"""The one-off correction for students enrolled before the split existed.

Shaped on the pilot's own record: a KG 2 child enrolled as FA/2026/003 after
paying 115,000 at the door, all of it recorded as admission money, so the
roster showed the term's 61,000 as owed.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from io import StringIO

from django.core.management import CommandError, call_command

from apps.payments.models import Payment
from apps.students import fees
from apps.students.models import Student

from .models import (
    AdmissionFeeItem,
    AdmissionFeeKind,
    AdmissionPayment,
    AdmissionPaymentStatus,
    ApplicantStatus,
)
from .tests_split import SplitTestCase

D = Decimal


class SettleEnrolmentPaymentCommandTests(SplitTestCase):
    def setUp(self):
        # Enrolled the old way: a student, and the whole lump as admission money.
        self.applicant = self.make_applicant(
            school_class=self.north_kg2, status=ApplicantStatus.ENROLLED,
            first_name="God's Choice", last_name="Edidiong",
        )
        self.student = Student.all_objects.create(
            school_class=self.north_kg2, admission_number="FA/2026/003",
            first_name="God's Choice", last_name="Edidiong", sex="female",
            parent_name="Mrs. Edidiong", parent_phone="08035550101",
        )
        self.applicant.student = self.student
        self.applicant.save()
        self.door = AdmissionPayment.all_objects.create(
            applicant=self.applicant, amount=D("115000"),
            status=AdmissionPaymentStatus.CONFIRMED, received_on=date(2026, 9, 16),
        )

    def run_command(self, *extra) -> str:
        out = StringIO()
        call_command(
            "settle_enrolment_payment", "FA/2026/003",
            "--school", "Alpha Schools", *extra, stdout=out,
        )
        return out.getvalue()

    def test_a_dry_run_reports_and_writes_nothing(self):
        output = self.run_command()
        self.assertIn("outstanding 61,000", output)   # before
        self.assertIn("outstanding 0", output)        # after
        self.assertIn("Dry run", output)
        self.assertFalse(Payment.all_objects.exists())
        self.door.refresh_from_db()
        self.assertEqual(self.door.amount, D("115000"))

    def test_commit_clears_the_term_and_keeps_the_rest_as_admission_money(self):
        self.run_command("--commit")

        term_payment = Payment.all_objects.get(student=self.student)
        self.assertEqual(term_payment.amount, D("61000"))
        self.assertEqual(term_payment.date_paid, date(2026, 9, 16))
        self.assertEqual(fees.position_for(self.student).outstanding, D("0"))

        self.door.refresh_from_db()
        self.assertEqual(self.door.amount, D("54000"))
        self.assertEqual(self.door.term_payment, term_payment)

    def test_running_it_twice_moves_nothing_more(self):
        self.run_command("--commit")
        self.run_command("--commit")
        self.assertEqual(Payment.all_objects.filter(student=self.student).count(), 1)

    def test_it_refuses_while_the_intake_sheet_still_charges_tuition(self):
        AdmissionFeeItem.all_objects.create(
            schedule=self.north_kg2.admission_fee_schedules.get(),
            name="Tuition", amount=D("24000"), kind=AdmissionFeeKind.INTAKE,
        )
        with self.assertRaisesMessage(CommandError, "seed_admission_fees"):
            self.run_command("--commit")
        self.assertFalse(Payment.all_objects.exists())
