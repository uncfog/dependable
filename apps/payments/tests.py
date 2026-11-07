"""Tests for payments and the balances derived from them.

The rules that must never regress, because every one of them is a way to tell a
parent the wrong thing about their money:

* a balance is expected minus *confirmed* payments -- pending money does not
  count until someone confirms it;
* confirming a pending payment moves the balance, and voiding a confirmed one
  moves it back;
* the status a student reads as (paid / partial / unpaid / overdue) and the
  colour it wears follow from the figures, not from anything stored;
* a voided payment is never deleted;
* tenant scoping holds -- no branch sees, records against, or reconciles
  another branch's money.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db.models import ProtectedError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.academics.models import Class, Level
from apps.core.permissions import Capability, has_capability
from apps.core.roles import Role
from apps.core.tenancy import scope_to
from apps.fees.models import FeeComponent, FeeStructure, Term, TermSequence
from apps.schools.models import Branch, School
from apps.students import fees
from apps.students.models import Sex, Student, StudentStatus

from . import balances
from .forms import PaymentForm, VoidForm
from .models import (
    Payment,
    PaymentLabel,
    PaymentMethod,
    PaymentSource,
    PaymentStatus,
)

User = get_user_model()

FIFTY_K = Decimal("50000")


class PaymentsTestCase(TestCase):
    """Two branches of one school. North is priced at 50,000 a term."""

    @classmethod
    def setUpTestData(cls):
        cls.alpha = School.all_objects.create(name="Alpha Schools")
        cls.north = Branch.all_objects.create(school=cls.alpha, name="North")
        cls.south = Branch.all_objects.create(school=cls.alpha, name="South")

        cls.owner = User.objects.create_user(
            "alpha.owner", password="pw", role=Role.SCHOOL_OWNER, school=cls.alpha
        )
        cls.principal = User.objects.create_user(
            "north.principal", password="pw", role=Role.PRINCIPAL,
            school=cls.alpha, branch=cls.north,
        )
        cls.bursar = User.objects.create_user(
            "north.bursar", password="pw", role=Role.BURSAR,
            school=cls.alpha, branch=cls.north,
        )
        cls.south_bursar = User.objects.create_user(
            "south.bursar", password="pw", role=Role.BURSAR,
            school=cls.alpha, branch=cls.south,
        )

        cls.jss1 = Class.all_objects.create(
            branch=cls.north, name="JSS 1", stream="A",
            level=Level.JUNIOR_SECONDARY, year_in_level=1,
        )
        # Deliberately never given a fee structure.
        cls.p1 = Class.all_objects.create(
            branch=cls.north, name="Primary 1", level=Level.PRIMARY, year_in_level=1
        )
        cls.south_p1 = Class.all_objects.create(
            branch=cls.south, name="Primary 1", level=Level.PRIMARY, year_in_level=1
        )

        cls.term = Term.all_objects.create(
            branch=cls.north, name="First Term 2025/2026", academic_year="2025/2026",
            sequence=TermSequence.FIRST, is_current=True,
        )
        cls.south_term = Term.all_objects.create(
            branch=cls.south, name="First Term 2025/2026", academic_year="2025/2026",
            sequence=TermSequence.FIRST, is_current=True,
        )

        structure = FeeStructure.all_objects.create(
            school_class=cls.jss1, term=cls.term
        )
        FeeComponent.all_objects.create(
            fee_structure=structure, name="School Fees", amount=FIFTY_K
        )
        south_structure = FeeStructure.all_objects.create(
            school_class=cls.south_p1, term=cls.south_term
        )
        FeeComponent.all_objects.create(
            fee_structure=south_structure, name="School Fees", amount=Decimal("30000")
        )

        cls.ada = cls.make_student("N/001", "Ada", "Obi", cls.jss1)
        cls.bola = cls.make_student("N/002", "Bola", "Ade", cls.jss1)
        cls.chidi = cls.make_student("N/003", "Chidi", "Eze", cls.jss1)
        cls.dami = cls.make_student("N/004", "Dami", "Ola", cls.p1)
        cls.south_kid = cls.make_student("S/001", "Sade", "Uche", cls.south_p1)

    @classmethod
    def make_student(cls, number, first, last, klass, **extra):
        return Student.all_objects.create(
            branch=klass.branch,
            school_class=klass,
            admission_number=number,
            first_name=first,
            last_name=last,
            sex=Sex.FEMALE,
            parent_name=f"Parent of {first}",
            parent_phone="08031234567",
            **extra,
        )

    @classmethod
    def pay(cls, student, amount, *, term=None, status=PaymentStatus.CONFIRMED,
            **extra) -> Payment:
        return Payment.all_objects.create(
            school=student.school,
            branch=student.branch,
            student=student,
            term=term or (cls.term if student.branch_id == cls.north.pk
                          else cls.south_term),
            amount=Decimal(amount),
            status=status,
            **extra,
        )

    def as_user(self, user):
        return scope_to(
            school_id=user.school_id, branch_id=user.branch_id, role=user.role
        )

    def position(self, student, user=None):
        with self.as_user(user or self.bursar):
            return fees.position_for(student)


# ===========================================================================
# The balance rule
# ===========================================================================


class BalanceTests(PaymentsTestCase):
    def test_nothing_paid_means_the_whole_fee_is_owed(self):
        position = self.position(self.ada)

        self.assertEqual(position.expected, FIFTY_K)
        self.assertEqual(position.paid, Decimal("0"))
        self.assertEqual(position.outstanding, FIFTY_K)
        self.assertEqual(position.state, fees.FeeState.UNPAID)

    def test_a_confirmed_payment_reduces_the_balance(self):
        self.pay(self.ada, "20000")
        position = self.position(self.ada)

        self.assertEqual(position.paid, Decimal("20000"))
        self.assertEqual(position.outstanding, Decimal("30000"))
        self.assertEqual(position.state, fees.FeeState.PARTIAL)

    def test_confirmed_payments_add_up(self):
        self.pay(self.ada, "20000")
        self.pay(self.ada, "15000")
        self.assertEqual(self.position(self.ada).outstanding, Decimal("15000"))

    def test_paying_in_full_settles_the_balance(self):
        self.pay(self.ada, FIFTY_K)
        position = self.position(self.ada)

        self.assertEqual(position.outstanding, Decimal("0"))
        self.assertEqual(position.state, fees.FeeState.PAID)

    def test_an_overpayment_is_a_credit_not_a_negative_balance(self):
        self.pay(self.ada, "60000")
        position = self.position(self.ada)

        self.assertEqual(position.paid, Decimal("60000"))
        self.assertEqual(position.outstanding, Decimal("0"))
        self.assertEqual(position.state, fees.FeeState.PAID)

    def test_a_pending_payment_does_not_count(self):
        """The single most important rule on this screen."""
        self.pay(self.ada, "20000", status=PaymentStatus.PENDING)
        position = self.position(self.ada)

        self.assertEqual(position.paid, Decimal("0"))
        self.assertEqual(position.outstanding, FIFTY_K)
        self.assertEqual(position.state, fees.FeeState.UNPAID)

    def test_confirming_a_pending_payment_moves_the_balance(self):
        payment = self.pay(self.ada, "20000", status=PaymentStatus.PENDING)
        self.assertEqual(self.position(self.ada).outstanding, FIFTY_K)

        payment.confirm(by=self.bursar)

        self.assertEqual(self.position(self.ada).outstanding, Decimal("30000"))
        self.assertIsNotNone(payment.confirmed_at)
        self.assertEqual(payment.confirmed_by, self.bursar)

    def test_a_voided_payment_stops_counting(self):
        payment = self.pay(self.ada, FIFTY_K)
        self.assertEqual(self.position(self.ada).outstanding, Decimal("0"))

        payment.void(by=self.principal, reason="Cheque bounced.")

        self.assertEqual(self.position(self.ada).outstanding, FIFTY_K)
        self.assertEqual(self.position(self.ada).state, fees.FeeState.UNPAID)

    def test_voiding_keeps_the_row_and_records_who_and_why(self):
        payment = self.pay(self.ada, FIFTY_K)
        payment.void(by=self.principal, reason="Duplicate entry.")

        payment.refresh_from_db()
        self.assertTrue(Payment.all_objects.filter(pk=payment.pk).exists())
        self.assertEqual(payment.status, PaymentStatus.VOID)
        self.assertEqual(payment.voided_by, self.principal)
        self.assertEqual(payment.void_reason, "Duplicate entry.")
        self.assertIsNotNone(payment.voided_at)

    def test_an_unpriced_class_has_no_balance_to_owe(self):
        position = self.position(self.dami)

        self.assertFalse(position.is_priced)
        self.assertEqual(position.outstanding, Decimal("0"))
        self.assertEqual(position.state, fees.FeeState.UNPRICED)

    def test_payments_for_another_term_do_not_count_toward_this_one(self):
        old_term = Term.all_objects.create(
            branch=self.north, name="Third Term 2024/2025",
            academic_year="2024/2025", sequence=TermSequence.THIRD,
        )
        self.pay(self.ada, FIFTY_K, term=old_term)

        self.assertEqual(self.position(self.ada).outstanding, FIFTY_K)

    def test_the_schedule_totals_a_whole_page_in_one_pass(self):
        self.pay(self.ada, FIFTY_K)
        self.pay(self.bola, "10000")

        with self.as_user(self.bursar):
            students = list(Student.objects.filter(school_class=self.jss1))
            schedule = fees.load(students)

        by_name = {
            s.first_name: schedule.position_for(s).outstanding for s in students
        }
        self.assertEqual(by_name["Ada"], Decimal("0"))
        self.assertEqual(by_name["Bola"], Decimal("40000"))
        self.assertEqual(by_name["Chidi"], FIFTY_K)


class OverdueTests(PaymentsTestCase):
    """Overdue is unpaid or partial past the term's own due date."""

    def set_due(self, days_ago: int | None):
        self.term.due_date = (
            None if days_ago is None
            else timezone.localdate() - timedelta(days=days_ago)
        )
        self.term.save()

    def test_no_due_date_means_nothing_is_overdue(self):
        """A deadline nobody stated is not one a parent can have missed."""
        self.set_due(None)
        self.assertEqual(self.position(self.ada).state, fees.FeeState.UNPAID)

    def test_a_future_due_date_is_not_overdue(self):
        self.set_due(-10)  # ten days from now
        self.assertEqual(self.position(self.ada).state, fees.FeeState.UNPAID)

    def test_unpaid_past_the_due_date_is_overdue(self):
        self.set_due(5)
        position = self.position(self.ada)

        self.assertTrue(position.is_overdue)
        self.assertEqual(position.state, fees.FeeState.OVERDUE)

    def test_part_paid_past_the_due_date_is_also_overdue(self):
        self.set_due(5)
        self.pay(self.ada, "20000")
        self.assertEqual(self.position(self.ada).state, fees.FeeState.OVERDUE)

    def test_paid_in_full_is_never_overdue(self):
        self.set_due(5)
        self.pay(self.ada, FIFTY_K)
        position = self.position(self.ada)

        self.assertFalse(position.is_overdue)
        self.assertEqual(position.state, fees.FeeState.PAID)


class StatusColourTests(PaymentsTestCase):
    """The four payment-status colours, and nothing invented alongside them."""

    def test_each_state_maps_to_its_design_system_pill(self):
        expected = {
            fees.FeeState.PAID: ("status-paid", "Paid"),
            fees.FeeState.PARTIAL: ("status-partial", "Part paid"),
            fees.FeeState.UNPAID: ("status-unpaid", "Unpaid"),
            fees.FeeState.OVERDUE: ("status-overdue", "Overdue"),
            fees.FeeState.UNPRICED: ("status-unpaid", "No fees set"),
        }
        self.assertEqual(fees.STATE_DISPLAY, expected)

    def test_a_position_wears_the_pill_for_its_state(self):
        cases = [
            (Decimal("0"), None, "status-unpaid", "Unpaid"),
            (Decimal("20000"), None, "status-partial", "Part paid"),
            (FIFTY_K, None, "status-paid", "Paid"),
            (Decimal("20000"), 5, "status-overdue", "Overdue"),
        ]
        for amount, overdue_days, pill, label in cases:
            with self.subTest(paid=amount, overdue=overdue_days):
                Payment.all_objects.filter(student=self.ada).delete()
                self.term.due_date = (
                    None if overdue_days is None
                    else timezone.localdate() - timedelta(days=overdue_days)
                )
                self.term.save()
                if amount:
                    self.pay(self.ada, amount)

                position = self.position(self.ada)
                self.assertEqual(position.pill_class, pill)
                self.assertEqual(position.pill_label, label)

    def test_payment_rows_wear_the_status_colours_too(self):
        self.assertEqual(
            self.pay(self.ada, "1000").pill_class, "status-paid"
        )
        self.assertEqual(
            self.pay(self.bola, "1000", status=PaymentStatus.PENDING).pill_class,
            "status-pending",
        )
        voided = self.pay(self.chidi, "1000")
        voided.void(reason="x")
        self.assertEqual(voided.pill_class, "status-void")


# ===========================================================================
# The model itself
# ===========================================================================


class ModelTests(PaymentsTestCase):
    def test_a_new_payment_is_manual_by_default(self):
        """The field exists now so gateway rows are distinguishable later."""
        payment = self.pay(self.ada, "1000")
        self.assertEqual(payment.source, PaymentSource.MANUAL)
        self.assertEqual(payment.gateway_reference, "")

    def test_recording_straight_as_confirmed_stamps_the_time(self):
        payment = self.pay(self.ada, "1000")
        self.assertIsNotNone(payment.confirmed_at)

    def test_a_pending_payment_is_not_stamped_as_confirmed(self):
        payment = self.pay(self.ada, "1000", status=PaymentStatus.PENDING)
        self.assertIsNone(payment.confirmed_at)

    def test_the_receipt_number_comes_from_the_row_id(self):
        payment = self.pay(self.ada, "1000")
        self.assertEqual(payment.receipt_number, f"RCP-{payment.pk:06d}")

    def test_only_confirmed_counts_toward_the_balance(self):
        self.assertTrue(self.pay(self.ada, "1").counts_toward_balance)
        self.assertFalse(
            self.pay(self.bola, "1", status=PaymentStatus.PENDING)
            .counts_toward_balance
        )

    def test_the_branch_is_taken_from_the_student(self):
        payment = Payment(student=self.ada, term=self.term, amount=Decimal("100"))
        payment.save()
        self.assertEqual(payment.branch_id, self.north.pk)
        self.assertEqual(payment.school_id, self.alpha.pk)

    def test_a_term_from_another_branch_is_rejected(self):
        payment = Payment(
            student=self.ada, term=self.south_term, amount=Decimal("100")
        )
        with self.assertRaises(ValidationError):
            payment.full_clean()

    def test_a_future_payment_date_is_rejected(self):
        payment = Payment(
            student=self.ada, term=self.term, amount=Decimal("100"),
            date_paid=timezone.localdate() + timedelta(days=1),
        )
        with self.assertRaises(ValidationError):
            payment.full_clean()

    def test_a_student_with_payments_cannot_be_deleted(self):
        """Deleting them would strand the money outside every balance."""
        self.pay(self.ada, "1000")
        with self.assertRaises(ProtectedError):
            self.ada.delete()

    def test_the_label_does_not_split_the_balance(self):
        """Labelling is descriptive; the balance is one combined figure."""
        self.pay(self.ada, "20000", label=PaymentLabel.UNIFORM)
        self.pay(self.ada, "15000", label=PaymentLabel.BOOKS)

        self.assertEqual(self.position(self.ada).paid, Decimal("35000"))
        self.assertEqual(self.position(self.ada).outstanding, Decimal("15000"))


class RunningBalanceTests(PaymentsTestCase):
    def test_a_receipt_shows_the_balance_at_the_time_it_was_taken(self):
        today = timezone.localdate()
        first = self.pay(self.ada, "20000", date_paid=today - timedelta(days=10))
        second = self.pay(self.ada, "10000", date_paid=today)

        with self.as_user(self.bursar):
            self.assertEqual(balances.running_balance(first), Decimal("30000"))
            self.assertEqual(balances.running_balance(second), Decimal("20000"))

    def test_same_day_payments_are_ordered_by_entry(self):
        today = timezone.localdate()
        first = self.pay(self.ada, "20000", date_paid=today)
        second = self.pay(self.ada, "20000", date_paid=today)

        with self.as_user(self.bursar):
            self.assertEqual(balances.running_balance(first), Decimal("30000"))
            self.assertEqual(balances.running_balance(second), Decimal("10000"))


class OutstandingTests(PaymentsTestCase):
    def test_lists_only_students_who_owe_most_owed_first(self):
        self.pay(self.ada, FIFTY_K)          # settled
        self.pay(self.bola, "40000")         # owes 10,000
        # Chidi owes the lot; Dami's class is unpriced.

        with self.as_user(self.bursar):
            rows = balances.outstanding()

        self.assertEqual(
            [row.student.pk for row in rows], [self.chidi.pk, self.bola.pk]
        )
        self.assertEqual(rows[0].owed, FIFTY_K)
        self.assertEqual(rows[1].owed, Decimal("10000"))

    def test_a_pending_payment_leaves_the_student_owing(self):
        self.pay(self.ada, FIFTY_K, status=PaymentStatus.PENDING)

        with self.as_user(self.bursar):
            rows = balances.outstanding()
        self.assertIn(self.ada.pk, [row.student.pk for row in rows])

    def test_it_matches_what_messaging_would_message(self):
        """The chase list and the reminder must never be different families."""
        from apps.messaging import audiences
        from apps.messaging.models import AudienceType

        self.pay(self.ada, FIFTY_K)
        self.pay(self.bola, "10000")

        with self.as_user(self.bursar):
            owing = {row.student.pk for row in balances.outstanding()}
            audience = audiences.resolve(AudienceType.OWING, branch=self.north)

        self.assertEqual(owing, {s.pk for s in audience.students})


# ===========================================================================
# Tenant scoping
# ===========================================================================


class ScopingTests(PaymentsTestCase):
    def test_a_branch_sees_only_its_own_payments(self):
        north = self.pay(self.ada, "1000")
        south = self.pay(self.south_kid, "1000")

        with self.as_user(self.bursar):
            self.assertEqual(
                [p.pk for p in Payment.objects.all()], [north.pk]
            )
        with self.as_user(self.south_bursar):
            self.assertEqual(
                [p.pk for p in Payment.objects.all()], [south.pk]
            )

    def test_a_branch_cannot_open_another_branchs_payment(self):
        south = self.pay(self.south_kid, "1000")

        self.client.force_login(self.bursar)
        for name in ("payments:payment_detail", "payments:receipt",
                     "payments:payment_update"):
            with self.subTest(url=name):
                self.assertEqual(
                    self.client.get(reverse(name, args=[south.pk])).status_code,
                    404,
                )

    def test_a_branchs_balance_ignores_another_branchs_money(self):
        self.pay(self.south_kid, "30000")
        with self.as_user(self.south_bursar):
            self.assertEqual(
                fees.position_for(self.south_kid).outstanding, Decimal("0")
            )
        # The same student, read by North, is not visible at all.
        with self.as_user(self.bursar):
            self.assertFalse(
                Student.objects.filter(pk=self.south_kid.pk).exists()
            )

    def test_the_outstanding_screen_stops_at_the_branch_boundary(self):
        with self.as_user(self.bursar):
            rows = balances.outstanding()
        self.assertNotIn(self.south_kid.pk, [row.student.pk for row in rows])

    def test_the_form_refuses_a_student_from_another_branch(self):
        with self.as_user(self.bursar):
            form = PaymentForm(
                data={
                    "student": self.south_kid.admission_number,
                    "amount": "1000",
                    "date_paid": timezone.localdate().isoformat(),
                    "label": PaymentLabel.SCHOOL_FEES,
                    "method": PaymentMethod.CASH,
                }
            )
            self.assertFalse(form.is_valid())
            self.assertIn("student", form.errors)


# ===========================================================================
# Permissions
# ===========================================================================


class CapabilityTests(TestCase):
    def test_a_bursar_records_and_views_but_does_not_void(self):
        bursar = User(role=Role.BURSAR)
        self.assertTrue(has_capability(bursar, Capability.VIEW_PAYMENTS))
        self.assertTrue(has_capability(bursar, Capability.RECORD_PAYMENTS))
        self.assertFalse(has_capability(bursar, Capability.VOID_PAYMENTS))

    def test_leadership_holds_every_payment_capability(self):
        for role in (Role.SCHOOL_OWNER, Role.PRINCIPAL, Role.PLATFORM_OWNER):
            user = User(role=role)
            with self.subTest(role=role):
                for capability in (
                    Capability.VIEW_PAYMENTS,
                    Capability.RECORD_PAYMENTS,
                    Capability.VOID_PAYMENTS,
                ):
                    self.assertTrue(has_capability(user, capability))


class ViewPermissionTests(PaymentsTestCase):
    def test_signed_out_visitors_are_sent_to_login(self):
        response = self.client.get(reverse("payments:record"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])

    def test_a_bursar_reaches_the_screens_they_work_on(self):
        self.client.force_login(self.bursar)
        for name in ("payments:index", "payments:record", "payments:pending",
                     "payments:outstanding"):
            with self.subTest(url=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)

    def test_a_bursar_cannot_reach_the_void_screen(self):
        payment = self.pay(self.ada, "1000")
        self.client.force_login(self.bursar)
        response = self.client.get(
            reverse("payments:payment_void", args=[payment.pk])
        )
        self.assertEqual(response.status_code, 403)

    def test_a_principal_can(self):
        payment = self.pay(self.ada, "1000")
        self.client.force_login(self.principal)
        response = self.client.get(
            reverse("payments:payment_void", args=[payment.pk])
        )
        self.assertEqual(response.status_code, 200)


# ===========================================================================
# Screens
# ===========================================================================


class RecordingTests(PaymentsTestCase):
    def post(self, **overrides):
        data = {
            "student": self.ada.admission_number,
            "amount": "20000",
            "date_paid": timezone.localdate().isoformat(),
            "label": PaymentLabel.SCHOOL_FEES,
            "method": PaymentMethod.TRANSFER,
            "reference": "FT123456789",
            "note": "",
            "confirm_now": "on",
        }
        data.update(overrides)
        return self.client.post(reverse("payments:record"), data)

    def test_a_bursar_records_a_confirmed_payment_and_lands_on_the_receipt(self):
        self.client.force_login(self.bursar)
        response = self.post()

        payment = Payment.all_objects.get()
        self.assertRedirects(
            response, reverse("payments:receipt", args=[payment.pk])
        )
        self.assertEqual(payment.status, PaymentStatus.CONFIRMED)
        self.assertEqual(payment.amount, Decimal("20000"))
        self.assertEqual(payment.student, self.ada)
        self.assertEqual(payment.term, self.term)
        self.assertEqual(payment.recorded_by, self.bursar)
        self.assertEqual(payment.confirmed_by, self.bursar)
        self.assertEqual(self.position(self.ada).outstanding, Decimal("30000"))

    def test_holding_a_payment_as_pending_leaves_the_balance_alone(self):
        self.client.force_login(self.bursar)
        response = self.post(confirm_now="")

        payment = Payment.all_objects.get()
        self.assertRedirects(response, reverse("payments:pending"))
        self.assertEqual(payment.status, PaymentStatus.PENDING)
        self.assertEqual(self.position(self.ada).outstanding, FIFTY_K)

    def test_the_picker_accepts_the_full_datalist_label(self):
        self.client.force_login(self.bursar)
        self.post(student=f"{self.ada.admission_number} — Ada Obi (JSS 1A)")

        self.assertEqual(Payment.all_objects.get().student, self.ada)

    def test_an_unknown_admission_number_is_a_field_error(self):
        self.client.force_login(self.bursar)
        response = self.post(student="NOPE/999")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Payment.all_objects.exists())
        self.assertIn("student", response.context["form"].errors)

    def test_a_receipt_can_be_attached(self):
        self.client.force_login(self.bursar)
        upload = SimpleUploadedFile(
            "slip.png", b"\x89PNG\r\n\x1a\n fake", content_type="image/png"
        )
        self.client.post(
            reverse("payments:record"),
            {
                "student": self.ada.admission_number,
                "amount": "20000",
                "date_paid": timezone.localdate().isoformat(),
                "label": PaymentLabel.SCHOOL_FEES,
                "method": PaymentMethod.TRANSFER,
                "receipt": upload,
                "confirm_now": "on",
                "receipt_checked": "on",
            },
        )

        payment = Payment.all_objects.get()
        self.assertTrue(payment.has_attachment)
        self.assertFalse(payment.attachment_is_pdf)
        payment.receipt.delete(save=False)

    def test_an_executable_masquerading_as_a_receipt_is_refused(self):
        self.client.force_login(self.bursar)
        response = self.client.post(
            reverse("payments:record"),
            {
                "student": self.ada.admission_number,
                "amount": "20000",
                "date_paid": timezone.localdate().isoformat(),
                "label": PaymentLabel.SCHOOL_FEES,
                "method": PaymentMethod.CASH,
                "receipt": SimpleUploadedFile(
                    "payload.exe", b"MZ", content_type="application/octet-stream"
                ),
                "confirm_now": "on",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Payment.all_objects.exists())
        self.assertIn("receipt", response.context["form"].errors)

    def test_a_zero_payment_is_refused(self):
        self.client.force_login(self.bursar)
        response = self.post(amount="0")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Payment.all_objects.exists())

    def test_recording_needs_a_current_term(self):
        self.term.is_current = False
        self.term.save()

        self.client.force_login(self.bursar)
        response = self.post()

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Payment.all_objects.exists())
        self.assertTrue(response.context["form"].non_field_errors())


class PendingQueueTests(PaymentsTestCase):
    def test_the_queue_lists_pending_payments_oldest_first(self):
        today = timezone.localdate()
        newer = self.pay(self.ada, "1000", status=PaymentStatus.PENDING,
                         date_paid=today)
        older = self.pay(self.bola, "1000", status=PaymentStatus.PENDING,
                         date_paid=today - timedelta(days=3))
        self.pay(self.chidi, "1000")  # confirmed: not in the queue

        self.client.force_login(self.bursar)
        response = self.client.get(reverse("payments:pending"))

        self.assertEqual(
            [p.pk for p in response.context["payments"]], [older.pk, newer.pk]
        )
        self.assertEqual(response.context["pending_total"], Decimal("2000"))

    def test_confirming_from_the_queue_corrects_and_credits(self):
        """The client's flow: the bursar fixes who and what, then confirms."""
        payment = self.pay(
            self.bola, "1000", status=PaymentStatus.PENDING,
            label=PaymentLabel.OTHER,
        )
        self.client.force_login(self.bursar)

        response = self.client.post(
            reverse("payments:payment_update", args=[payment.pk]),
            {
                "student": self.ada.admission_number,   # corrected
                "amount": "25000",                      # corrected
                "date_paid": payment.date_paid.isoformat(),
                "label": PaymentLabel.SCHOOL_FEES,      # corrected
                "method": PaymentMethod.TRANSFER,
                "reference": "FT99887766",
                "confirm_now": "on",
            },
        )

        payment.refresh_from_db()
        self.assertRedirects(
            response, reverse("payments:receipt", args=[payment.pk])
        )
        self.assertEqual(payment.status, PaymentStatus.CONFIRMED)
        self.assertEqual(payment.student, self.ada)
        self.assertEqual(payment.amount, Decimal("25000"))
        self.assertEqual(payment.label, PaymentLabel.SCHOOL_FEES)
        self.assertEqual(payment.confirmed_by, self.bursar)
        self.assertEqual(self.position(self.ada).outstanding, Decimal("25000"))
        self.assertEqual(self.position(self.bola).outstanding, FIFTY_K)


class VoidingTests(PaymentsTestCase):
    def url(self, payment):
        return reverse("payments:payment_void", args=[payment.pk])

    def test_voiding_needs_a_reason_and_the_tickbox(self):
        payment = self.pay(self.ada, FIFTY_K)
        self.client.force_login(self.principal)

        response = self.client.post(self.url(payment), {"reason": "", "confirm": ""})

        payment.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payment.status, PaymentStatus.CONFIRMED)

    def test_a_confirmed_void_reverses_the_balance_and_is_logged(self):
        payment = self.pay(self.ada, FIFTY_K)
        self.assertEqual(self.position(self.ada).outstanding, Decimal("0"))

        self.client.force_login(self.principal)
        response = self.client.post(
            self.url(payment), {"reason": "Cheque bounced.", "confirm": "on"}
        )

        payment.refresh_from_db()
        self.assertRedirects(response, payment.get_absolute_url())
        self.assertEqual(payment.status, PaymentStatus.VOID)
        self.assertEqual(payment.voided_by, self.principal)
        self.assertEqual(payment.void_reason, "Cheque bounced.")
        self.assertEqual(self.position(self.ada).outstanding, FIFTY_K)

    def test_the_screen_previews_what_the_void_will_do(self):
        payment = self.pay(self.ada, "20000")
        self.client.force_login(self.principal)

        response = self.client.get(self.url(payment))

        self.assertEqual(response.context["position"].outstanding, Decimal("30000"))
        self.assertEqual(response.context["outstanding_after"], FIFTY_K)

    def test_a_voided_payment_cannot_be_edited(self):
        payment = self.pay(self.ada, FIFTY_K)
        payment.void(by=self.principal, reason="x")

        self.client.force_login(self.bursar)
        self.assertEqual(
            self.client.get(
                reverse("payments:payment_update", args=[payment.pk])
            ).status_code,
            404,
        )


class ScreenTests(PaymentsTestCase):
    def test_the_list_totals_confirmed_and_pending_separately(self):
        self.pay(self.ada, "20000")
        self.pay(self.bola, "5000", status=PaymentStatus.PENDING)
        voided = self.pay(self.chidi, "9000")
        voided.void(reason="x")

        self.client.force_login(self.bursar)
        response = self.client.get(reverse("payments:index"))

        self.assertEqual(response.context["confirmed_total"], Decimal("20000"))
        self.assertEqual(response.context["pending_total"], Decimal("5000"))
        self.assertEqual(response.context["pending_count"], 1)

    def test_the_list_filters_by_label_and_status(self):
        self.pay(self.ada, "1000", label=PaymentLabel.UNIFORM)
        self.pay(self.bola, "2000", label=PaymentLabel.BOOKS)

        self.client.force_login(self.bursar)
        response = self.client.get(
            reverse("payments:index"), {"label": PaymentLabel.UNIFORM}
        )

        self.assertEqual(
            [p.label for p in response.context["payments"]],
            [PaymentLabel.UNIFORM],
        )

    def test_the_list_filters_by_date_range(self):
        today = timezone.localdate()
        recent = self.pay(self.ada, "1000", date_paid=today)
        self.pay(self.bola, "1000", date_paid=today - timedelta(days=40))

        self.client.force_login(self.bursar)
        response = self.client.get(
            reverse("payments:index"),
            {"since": (today - timedelta(days=7)).isoformat()},
        )

        self.assertEqual(
            [p.pk for p in response.context["payments"]], [recent.pk]
        )

    def test_the_receipt_shows_the_balance_at_that_moment(self):
        payment = self.pay(self.ada, "20000")

        self.client.force_login(self.bursar)
        response = self.client.get(
            reverse("payments:receipt", args=[payment.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["running_balance"], Decimal("30000"))
        self.assertContains(response, payment.receipt_number)
        self.assertContains(response, self.ada.parent_name)

    def test_a_students_detail_page_lists_their_payments(self):
        payment = self.pay(self.ada, "20000", reference="FT12345")

        self.client.force_login(self.bursar)
        response = self.client.get(self.ada.get_absolute_url())

        self.assertEqual(
            [p.pk for p in response.context["payments"]], [payment.pk]
        )
        self.assertContains(response, payment.receipt_number)
        self.assertEqual(response.context["position"].outstanding, Decimal("30000"))

    def test_the_detail_page_flags_money_still_sitting_pending(self):
        self.pay(self.ada, "20000", status=PaymentStatus.PENDING)

        self.client.force_login(self.bursar)
        response = self.client.get(self.ada.get_absolute_url())

        self.assertEqual(response.context["pending_total"], Decimal("20000"))
        self.assertEqual(response.context["position"].outstanding, FIFTY_K)

    def test_the_outstanding_screen_lists_who_owes(self):
        self.pay(self.ada, FIFTY_K)

        self.client.force_login(self.bursar)
        response = self.client.get(reverse("payments:outstanding"))

        listed = [row.student.pk for row in response.context["rows"]]
        self.assertNotIn(self.ada.pk, listed)
        self.assertIn(self.bola.pk, listed)
        self.assertEqual(response.context["owed_total"], FIFTY_K * 2)

    def test_the_bursar_dashboard_reports_real_collections(self):
        self.pay(self.ada, "20000")
        self.pay(self.bola, "5000", status=PaymentStatus.PENDING)

        self.client.force_login(self.bursar)
        response = self.client.get(reverse("core:bursar_dashboard"))

        self.assertEqual(response.context["collected_total"], Decimal("20000"))
        self.assertEqual(response.context["pending_total"], Decimal("5000"))
        # Three JSS 1A students at 50,000 each, less the 20,000 confirmed.
        self.assertEqual(response.context["expected_total"], Decimal("150000"))
        self.assertEqual(response.context["outstanding_total"], Decimal("130000"))


class SeedCommandTests(PaymentsTestCase):
    """The seed has to make every state visible, or it demonstrates nothing."""

    def seed(self, *args):
        out = StringIO()
        call_command(
            "seed_payments", "--school", "Alpha Schools", "--branch", "North",
            *args, stdout=out, stderr=out,
        )
        return out.getvalue()

    def test_it_produces_confirmed_pending_and_voided_payments(self):
        self.seed()

        statuses = set(
            Payment.all_objects.filter(term=self.term)
            .values_list("status", flat=True)
        )
        self.assertIn(PaymentStatus.CONFIRMED, statuses)
        self.assertIn(PaymentStatus.PENDING, statuses)
        self.assertIn(PaymentStatus.VOID, statuses)

    def test_pending_receipts_carry_an_attachment(self):
        self.seed()

        pending = Payment.all_objects.filter(
            term=self.term, status=PaymentStatus.PENDING
        )
        self.assertTrue(pending.exists())
        for payment in pending:
            self.assertTrue(payment.has_attachment)
            payment.receipt.delete(save=False)

    def test_it_never_invents_a_payment_for_an_unpriced_class(self):
        self.seed()
        self.assertFalse(
            Payment.all_objects.filter(student__school_class=self.p1).exists()
        )

    def test_running_it_twice_changes_nothing(self):
        self.seed()
        before = Payment.all_objects.filter(term=self.term).count()

        output = self.seed()

        self.assertIn("Nothing done", output)
        self.assertEqual(
            Payment.all_objects.filter(term=self.term).count(), before
        )
        for payment in Payment.all_objects.filter(term=self.term):
            if payment.receipt:
                payment.receipt.delete(save=False)

    def test_replace_reseeds_cleanly(self):
        self.seed()
        self.seed("--replace")

        self.assertTrue(Payment.all_objects.filter(term=self.term).exists())
        for payment in Payment.all_objects.filter(term=self.term):
            if payment.receipt:
                payment.receipt.delete(save=False)


class NavigationTests(TestCase):
    def test_payments_is_reachable_from_the_sidebar_for_every_role(self):
        from apps.core.navigation import nav_for

        for role in (
            Role.SCHOOL_OWNER, Role.PRINCIPAL, Role.BURSAR, Role.PLATFORM_OWNER
        ):
            sections = {s.label: s.items for s in nav_for(role, "/payments/")}
            items = {i.label: i for i in sections["Finance"]}
            with self.subTest(role=role):
                self.assertTrue(items["Payments"].available)
                self.assertEqual(
                    items["Payments"].href, reverse("payments:index")
                )
                self.assertTrue(items["Outstanding"].available)


class VoidFormTests(TestCase):
    def test_a_reason_is_required(self):
        self.assertFalse(VoidForm(data={"confirm": "on"}).is_valid())

    def test_the_tickbox_is_required(self):
        self.assertFalse(VoidForm(data={"reason": "Duplicate"}).is_valid())

    def test_both_together_pass(self):
        self.assertTrue(
            VoidForm(data={"reason": "Duplicate", "confirm": "on"}).is_valid()
        )
