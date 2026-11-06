"""Three ways money could land in the wrong place, and the report on past ones.

* a payment credited to the wrong child, because admission numbers are unique per
  campus and the picker used to take the first match;
* a bursar, who may not void, undoing confirmed money by editing it instead;
* one press of Record becoming two payments on a slow connection.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from apps.admissions.models import AdmissionPayment, Applicant
from apps.academics.models import Level
from apps.core.roles import Role
from apps.schools.models import SchoolModule
from apps.students.models import Sex

from .forms import picker_label, picker_options, student_from_picker
from .models import Payment, PaymentStatus
from .tests import PaymentsTestCase


def form_data(student_value, **overrides) -> dict:
    data = {
        "student": student_value,
        "amount": "5000",
        "date_paid": timezone.localdate().isoformat(),
        "label": "school_fees",
        "method": "cash",
        "reference": "",
        "note": "",
        "confirm_now": "on",
    }
    data.update(overrides)
    return data


class SharedAdmissionNumberTests(PaymentsTestCase):
    """North's Ada and South's Zed are both number N/001. Each campus may do that."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.zed = cls.make_student("N/001", "Zed", "Ife", cls.south_p1)

    def option_for(self, name: str, user=None) -> str:
        with self.as_user(user or self.owner):
            return next(o for o in picker_options() if name in o)

    def record_as_owner(self, value):
        self.client.force_login(self.owner)
        return self.client.post(reverse("payments:record"), form_data(value))

    def test_each_child_is_credited_when_their_own_option_is_chosen(self):
        for student, name in ((self.ada, "Ada"), (self.zed, "Zed")):
            response = self.record_as_owner(self.option_for(name))
            self.assertEqual(response.status_code, 302)
            payment = Payment.all_objects.order_by("-pk").first()
            self.assertEqual(payment.student, student)
            self.assertEqual(payment.branch, student.branch)
            Payment.all_objects.filter(pk=payment.pk).delete()

    def test_the_options_name_the_campus_only_when_it_is_needed(self):
        ada, zed = self.option_for("Ada"), self.option_for("Zed")
        self.assertIn("North", ada)
        self.assertIn("South", zed)
        self.assertNotIn("North", self.option_for("Bola"))

    def test_a_bare_shared_number_is_refused_rather_than_guessed(self):
        response = self.record_as_owner("N/001")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Payment.all_objects.exists())
        self.assertIn("student", response.context["form"].errors)
        self.assertContains(response, "share")

    def test_the_balance_panel_will_not_guess_either(self):
        with self.as_user(self.owner):
            self.assertIsNone(student_from_picker("N/001"))
            self.assertEqual(student_from_picker(self.option_for("Zed")), self.zed)

    def test_a_campus_that_can_see_one_of_them_still_types_the_bare_number(self):
        self.client.force_login(self.bursar)
        self.client.post(reverse("payments:record"), form_data("N/001"))

        self.assertEqual(Payment.all_objects.get().student, self.ada)

    def test_a_platform_account_is_not_given_another_schools_child(self):
        from apps.schools.models import Branch, School
        from apps.academics.models import Class
        from django.contrib.auth import get_user_model

        other = School.all_objects.create(name="Bravo")
        branch = Branch.all_objects.create(school=other, name="North")
        klass = Class.all_objects.create(
            branch=branch, name="JSS 1", level=Level.JUNIOR_SECONDARY, year_in_level=1
        )
        stranger = self.make_student("B/7", "Tola", "Ama", klass)
        twin = self.make_student("B/7", "Uju", "Obi", self.jss1)
        platform = get_user_model().objects.create_user(
            "platform", password="pw", role=Role.PLATFORM_OWNER
        )

        with self.as_user(platform):
            options = [o for o in picker_options() if o.startswith("B/7")]
            self.assertEqual(len(options), 2)
            chosen = next(o for o in options if "Uju" in o)
            self.assertIn("Bravo", next(o for o in options if "Tola" in o))
            self.assertEqual(student_from_picker(chosen), twin)
            self.assertNotEqual(student_from_picker(chosen), stranger)


class NumericAdmissionNumberTests(PaymentsTestCase):
    def test_a_typed_number_is_never_read_as_a_database_id(self):
        """Refusing the form must not swap the typed child for another one."""
        typed = self.make_student(str(self.bola.pk), "Efe", "Ona", self.jss1)
        self.client.force_login(self.bursar)

        response = self.client.post(
            reverse("payments:record"),
            form_data(typed.admission_number, amount="-1"),
        )

        self.assertEqual(response.status_code, 200)
        value = response.context["form"]["student"].value()
        self.assertEqual(value, typed.admission_number)
        self.assertNotIn("Bola", value)


class ConfirmedMoneyIsLockedTests(PaymentsTestCase):
    def setUp(self):
        self.payment = self.pay(
            self.ada, "1000", recorded_by=self.bursar, reference="OLD", date_paid="2026-01-05"
        )
        self.url = reverse("payments:payment_update", args=[self.payment.pk])

    def post_as(self, user, **overrides):
        self.client.force_login(user)
        data = form_data(self.bola.admission_number, amount="1", **overrides)
        return self.client.post(self.url, data)

    def test_a_bursar_cannot_change_the_student_amount_or_date(self):
        self.post_as(self.bursar, date_paid="2026-01-01")
        self.payment.refresh_from_db()

        self.assertEqual(self.payment.student, self.ada)
        self.assertEqual(self.payment.amount, Decimal("1000"))
        self.assertEqual(self.payment.date_paid.isoformat(), "2026-01-05")

    def test_a_bursar_cannot_unconfirm_it_by_leaving_the_box_unticked(self):
        self.client.force_login(self.bursar)
        data = form_data(self.ada.admission_number, amount="1000")
        data.pop("confirm_now")
        self.client.post(self.url, data)
        self.payment.refresh_from_db()

        self.assertEqual(self.payment.status, PaymentStatus.CONFIRMED)
        self.assertIsNotNone(self.payment.confirmed_at)

    def test_a_bursar_can_still_correct_the_descriptive_fields(self):
        response = self.post_as(
            self.bursar, reference="TELLER-9", note="Teller retyped", label="uniform"
        )
        self.payment.refresh_from_db()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.payment.reference, "TELLER-9")
        self.assertEqual(self.payment.note, "Teller retyped")
        self.assertEqual(self.payment.label, "uniform")

    def test_the_bursars_screen_says_who_can_change_the_money(self):
        self.client.force_login(self.bursar)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "principal or the school owner")
        self.assertNotIn("amount", response.context["form"].fields)

    def test_a_principal_can_still_correct_the_money(self):
        self.post_as(self.principal)
        self.payment.refresh_from_db()

        self.assertEqual(self.payment.amount, Decimal("1"))

    def test_a_bursar_can_still_correct_a_pending_receipt_before_confirming(self):
        pending = self.pay(self.ada, "2000", status=PaymentStatus.PENDING)
        self.client.force_login(self.bursar)
        response = self.client.post(
            reverse("payments:payment_update", args=[pending.pk]),
            form_data(self.bola.admission_number, amount="1500"),
        )
        pending.refresh_from_db()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(pending.student, self.bola)
        self.assertEqual(pending.amount, Decimal("1500"))
        self.assertEqual(pending.status, PaymentStatus.CONFIRMED)


class DuplicateSubmissionTests(PaymentsTestCase):
    def record(self, **overrides):
        self.client.force_login(self.bursar)
        return self.client.post(
            reverse("payments:record"),
            form_data(self.ada.admission_number, **overrides),
        )

    def test_the_same_form_sent_twice_records_one_payment(self):
        first = self.record()
        second = self.record()

        self.assertEqual(Payment.all_objects.count(), 1)
        self.assertEqual(second.status_code, 302)
        self.assertEqual(second["Location"], first["Location"])

    def test_the_second_press_is_told_what_happened(self):
        self.record()
        response = self.client.post(
            reverse("payments:record"),
            form_data(self.ada.admission_number),
            follow=True,
        )

        self.assertContains(response, "already recorded")

    def test_a_different_amount_is_a_different_payment(self):
        self.record()
        self.record(amount="6000")

        self.assertEqual(Payment.all_objects.count(), 2)

    def test_a_different_reference_is_a_different_payment(self):
        self.record(reference="A")
        self.record(reference="B")

        self.assertEqual(Payment.all_objects.count(), 2)

    def test_the_same_payment_after_the_window_is_a_new_payment(self):
        self.record()
        Payment.all_objects.update(created_at=timezone.now() - timedelta(minutes=5))
        self.record()

        self.assertEqual(Payment.all_objects.count(), 2)

    def test_a_voided_payment_does_not_block_entering_it_again(self):
        self.record()
        Payment.all_objects.get().void(by=self.principal, reason="Wrong child")
        self.record()

        self.assertEqual(Payment.all_objects.count(), 2)

    def test_two_people_recording_the_same_sum_are_not_merged(self):
        self.record()
        self.client.force_login(self.principal)
        self.client.post(
            reverse("payments:record"), form_data(self.ada.admission_number)
        )

        self.assertEqual(Payment.all_objects.count(), 2)


class DuplicateAdmissionPaymentTests(PaymentsTestCase):
    def test_the_same_admission_payment_sent_twice_records_one(self):
        SchoolModule.all_objects.create(
            school=self.alpha, key="admissions", enabled=True
        )
        applicant = Applicant.all_objects.create(
            branch=self.north, school=self.alpha, first_name="New", last_name="Child",
            sex=Sex.FEMALE, level=Level.JUNIOR_SECONDARY, school_class=self.jss1,
            parent_name="P", parent_phone="08031234567",
        )
        self.client.force_login(self.bursar)
        data = {
            "amount": "5000", "method": "cash", "status": "confirmed",
            "received_on": timezone.localdate().isoformat(),
            "reference": "", "note": "",
        }
        url = reverse("admissions:payment_create", args=[applicant.pk])

        self.client.post(url, data)
        self.client.post(url, data)

        self.assertEqual(AdmissionPayment.all_objects.count(), 1)


class WrongChildReportTests(PaymentsTestCase):
    def report(self, **options) -> str:
        out = StringIO()
        call_command("report_wrong_child_risk", stdout=out, stderr=StringIO(), **options)
        return out.getvalue()

    def test_a_clean_school_reports_nothing(self):
        self.pay(self.ada, "1000", recorded_by=self.owner)

        self.assertEqual(len(self.report().strip().splitlines()), 1)  # headings only

    def test_an_owner_recording_against_a_shared_number_is_flagged(self):
        self.make_student("N/001", "Zed", "Ife", self.south_p1)
        payment = self.pay(self.ada, "1000", recorded_by=self.owner)

        output = self.report()

        self.assertIn(payment.receipt_number, output)
        self.assertIn("shared_admission_number", output)

    def test_a_bursar_could_only_see_one_of_them_so_is_not_flagged(self):
        self.make_student("N/001", "Zed", "Ife", self.south_p1)
        payment = self.pay(self.ada, "1000", recorded_by=self.bursar)

        self.assertNotIn(payment.receipt_number, self.report())

    def test_whoever_confirmed_it_counts_too(self):
        self.make_student("N/001", "Zed", "Ife", self.south_p1)
        payment = self.pay(
            self.ada, "1000", recorded_by=self.bursar, confirmed_by=self.owner
        )

        self.assertIn(payment.receipt_number, self.report())

    def test_a_numeric_number_matching_a_database_id_is_flagged_as_possible(self):
        self.make_student(str(self.bola.pk), "Efe", "Ona", self.jss1)
        payment = self.pay(self.bola, "1000", recorded_by=self.bursar)

        output = self.report()

        self.assertIn(payment.receipt_number, output)
        self.assertIn("numeric_number_read_as_id", output)

    def test_a_voided_payment_needs_no_review(self):
        self.make_student("N/001", "Zed", "Ife", self.south_p1)
        payment = self.pay(
            self.ada, "1000", recorded_by=self.owner, status=PaymentStatus.VOID
        )

        self.assertNotIn(payment.receipt_number, self.report())

    def test_it_can_be_limited_to_one_school(self):
        self.make_student("N/001", "Zed", "Ife", self.south_p1)
        payment = self.pay(self.ada, "1000", recorded_by=self.owner)

        self.assertIn(payment.receipt_number, self.report(school=self.alpha.slug))
        self.assertNotIn(payment.receipt_number, self.report(school="nobody"))

    def test_it_changes_nothing_and_prints_no_family_contact_details(self):
        self.make_student("N/001", "Zed", "Ife", self.south_p1)
        payment = self.pay(self.ada, "1000", recorded_by=self.owner)
        before = Payment.all_objects.filter(pk=payment.pk).values().get()

        output = self.report()

        self.assertEqual(Payment.all_objects.filter(pk=payment.pk).values().get(), before)
        self.assertNotIn("08031234567", output)
        self.assertNotIn("Parent of", output)

    def test_labels_in_the_shared_case_do_not_leak_into_the_unique_label(self):
        with self.as_user(self.bursar):
            self.assertEqual(
                picker_label(self.bola),
                f"N/002 — Bola Ade ({self.jss1.display_name})",
            )
