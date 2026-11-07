"""A child's balance across terms, and the rules that keep old figures honest.

The one that matters most: **no existing number moves on deploy.** Earlier terms only
count once a term has been recorded for the child, so nothing is invented for history
nobody can vouch for. After that, whatever was left unpaid follows the child.
"""

from __future__ import annotations

from decimal import Decimal
from importlib import import_module

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.db import connection
from django.db.models import ProtectedError
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.academics.models import Class, Level
from apps.audit.models import AuditEvent
from apps.core.roles import Role
from apps.core.tenancy import scope_to
from apps.messaging.audiences import owing_students
from apps.payments import balances
from apps.payments.models import Payment, PaymentStatus
from apps.schools import teardown
from apps.schools.models import Branch, School, SchoolStatus
from apps.students import fees
from apps.students.models import Sex, Student, StudentStatus

from .models import (
    AdjustmentKind,
    FeeComponent,
    FeeStructure,
    StudentFeeAdjustment,
    StudentTermEnrolment,
    Term,
    TermSequence,
)

User = get_user_model()
D = Decimal


class LedgerTestCase(TestCase):
    """A child in JSS 1 at ₦50,000 a term, JSS 2 at ₦60,000."""

    @classmethod
    def setUpTestData(cls):
        cls.school = School.all_objects.create(name="Alpha", status=SchoolStatus.ACTIVE)
        cls.north = Branch.all_objects.create(school=cls.school, name="North")
        cls.jss1 = Class.all_objects.create(
            branch=cls.north, name="JSS 1", level=Level.JUNIOR_SECONDARY, year_in_level=1
        )
        cls.jss2 = Class.all_objects.create(
            branch=cls.north, name="JSS 2", level=Level.JUNIOR_SECONDARY, year_in_level=2
        )
        cls.first = cls.make_term(TermSequence.FIRST, current=True)
        cls.second = cls.make_term(TermSequence.SECOND)
        cls.third = cls.make_term(TermSequence.THIRD)
        for term in (cls.first, cls.second, cls.third):
            cls.price(cls.jss1, term, "50000")
            cls.price(cls.jss2, term, "60000")
        cls.ada = cls.make_student("A1", "Ada")
        cls.owner = User.objects.create_user(
            "owner", password="pw", role=Role.SCHOOL_OWNER, school=cls.school
        )
        cls.principal = User.objects.create_user(
            "principal", password="pw", role=Role.PRINCIPAL, school=cls.school, branch=cls.north
        )
        cls.bursar = User.objects.create_user(
            "bursar", password="pw", role=Role.BURSAR, school=cls.school, branch=cls.north
        )

    @classmethod
    def make_term(cls, sequence, current=False, year="2026/2027", branch=None):
        return Term.all_objects.create(
            branch=branch or cls.north, name=f"Term {sequence} {year}", academic_year=year,
            sequence=sequence, is_current=current,
        )

    @classmethod
    def price(cls, klass, term, amount):
        structure = FeeStructure.all_objects.create(school_class=klass, term=term)
        FeeComponent.all_objects.create(fee_structure=structure, name="Fees", amount=D(amount))

    @classmethod
    def make_student(cls, number, first, klass=None, **extra):
        return Student.all_objects.create(
            branch=(klass or cls.jss1).branch, school_class=klass or cls.jss1,
            admission_number=number, first_name=first, last_name="Obi", sex=Sex.FEMALE,
            parent_name="P", parent_phone="08031234567", **extra,
        )

    def pay(self, amount, term=None, student=None, **extra):
        student = student or self.ada
        return Payment.all_objects.create(
            branch=student.branch, student=student, term=term or self.current_term(),
            amount=D(amount), **extra,
        )

    def current_term(self):
        return Term.all_objects.get(branch=self.north, is_current=True)

    def start(self, term):
        term = Term.all_objects.get(pk=term.pk)
        term.is_current = True
        term.save()

    def adjust(self, kind, amount, term=None, student=None, **extra):
        return StudentFeeAdjustment.all_objects.create(
            student=student or self.ada, term=term or self.current_term(), kind=kind,
            amount=D(amount), description=extra.pop("description", "Because"), **extra,
        )

    def position(self, student=None):
        return fees.position_for(Student.all_objects.get(pk=(student or self.ada).pk))


class NothingMovesOnDeployTests(LedgerTestCase):
    def test_an_ordinary_position_reads_exactly_as_before(self):
        self.pay("20000")

        position = self.position()

        self.assertEqual(position.expected, D("50000"))
        self.assertEqual(position.paid, D("20000"))
        self.assertEqual(position.outstanding, D("30000"))
        self.assertEqual(position.arrears, D("0"))
        self.assertEqual(position.total_owed, D("30000"))
        self.assertEqual(position.state, fees.FeeState.PARTIAL)

    def test_an_earlier_term_with_no_record_of_the_child_adds_no_debt(self):
        """History nobody can vouch for is not turned into a debt."""
        StudentTermEnrolment.all_objects.filter(student=self.ada).delete()
        self.start(self.second)
        StudentTermEnrolment.all_objects.filter(student=self.ada, term=self.first).delete()

        self.assertEqual(self.position().arrears, D("0"))

    def test_money_in_an_unrecorded_term_is_not_turned_into_a_credit(self):
        self.start(self.second)
        StudentTermEnrolment.all_objects.filter(student=self.ada, term=self.first).delete()
        self.pay("50000", term=self.first)

        self.assertEqual(self.position().arrears, D("0"))
        self.assertEqual(self.position().outstanding, D("50000"))

    def test_the_backfill_records_only_the_term_each_campus_is_in(self):
        StudentTermEnrolment.all_objects.all().delete()
        withdrawn = self.make_student("A2", "Gone", status=StudentStatus.WITHDRAWN)
        migration = import_module("apps.fees.migrations.0004_backfill_current_enrolments")

        migration.backfill(django_apps, None)

        rows = StudentTermEnrolment.all_objects.all()
        self.assertEqual({r.student_id for r in rows}, {self.ada.pk})
        self.assertEqual({r.term_id for r in rows}, {self.first.pk})
        self.assertEqual(rows.first().school_class, self.jss1)
        self.assertEqual(rows.first().school_id, self.school.pk)
        self.assertNotIn(withdrawn.pk, {r.student_id for r in rows})


class EnrolmentTests(LedgerTestCase):
    def enrolment(self, term):
        return StudentTermEnrolment.all_objects.get(student=self.ada, term=term)

    def test_a_new_student_is_recorded_for_the_current_term(self):
        self.assertEqual(self.enrolment(self.first).school_class, self.jss1)

    def test_starting_a_term_records_everyone_active_there(self):
        gone = self.make_student("A2", "Gone", status=StudentStatus.WITHDRAWN)

        self.start(self.second)

        self.assertEqual(self.enrolment(self.second).school_class, self.jss1)
        self.assertFalse(
            StudentTermEnrolment.all_objects.filter(student=gone, term=self.second).exists()
        )

    def test_moving_class_changes_this_term_and_leaves_the_last_one_alone(self):
        self.start(self.second)
        self.ada.school_class = self.jss2
        self.ada.save()

        self.assertEqual(self.enrolment(self.second).school_class, self.jss2)
        self.assertEqual(self.enrolment(self.first).school_class, self.jss1)

    def test_a_returning_student_is_recorded_again(self):
        gone = self.make_student("A2", "Back", status=StudentStatus.WITHDRAWN)
        gone.status = StudentStatus.ACTIVE
        gone.save()

        self.assertTrue(
            StudentTermEnrolment.all_objects.filter(student=gone, term=self.first).exists()
        )

    def test_another_campus_starting_a_term_does_not_touch_this_one(self):
        south = Branch.all_objects.create(school=self.school, name="South")
        term = self.make_term(TermSequence.FIRST, current=True, branch=south)

        self.assertFalse(StudentTermEnrolment.all_objects.filter(term=term).exists())


class ArrearsTests(LedgerTestCase):
    def test_what_was_unpaid_follows_the_child(self):
        self.pay("10000")
        self.start(self.second)

        position = self.position()

        self.assertEqual(position.arrears, D("40000"))
        self.assertEqual(position.expected, D("50000"))
        self.assertEqual(position.outstanding, D("50000"))
        self.assertEqual(position.arrears_outstanding, D("40000"))
        self.assertEqual(position.total_owed, D("90000"))

    def test_money_settles_the_oldest_debt_first(self):
        self.pay("10000")
        self.start(self.second)
        self.pay("50000")

        position = self.position()

        self.assertEqual(position.arrears_outstanding, D("0"))
        self.assertEqual(position.outstanding, D("40000"))
        self.assertEqual(position.total_owed, D("40000"))
        self.assertEqual(position.state, fees.FeeState.PARTIAL)

    def test_a_term_paid_while_arrears_remain_does_not_read_as_paid(self):
        self.start(self.second)
        self.pay("50000")

        position = self.position()

        self.assertEqual(position.total_owed, D("50000"))
        self.assertNotEqual(position.state, fees.FeeState.PAID)

    def test_a_term_with_nothing_left_owing_reads_as_paid(self):
        self.pay("50000")
        self.start(self.second)
        self.pay("50000")

        self.assertEqual(self.position().state, fees.FeeState.PAID)

    def test_arrears_use_the_class_they_were_in_then(self):
        self.start(self.second)
        self.ada.school_class = self.jss2
        self.ada.save()

        position = self.position()

        self.assertEqual(position.arrears, D("50000"))
        self.assertEqual(position.expected, D("60000"))

    def test_several_terms_add_up(self):
        self.pay("20000")
        self.start(self.second)
        self.pay("10000")
        self.start(self.third)

        self.assertEqual(self.position().arrears, D("30000") + D("40000"))

    def test_pending_and_void_money_does_not_settle_anything(self):
        self.pay("50000", status=PaymentStatus.PENDING)
        self.pay("50000", status=PaymentStatus.VOID)
        self.start(self.second)

        self.assertEqual(self.position().arrears, D("50000"))

    def test_going_back_a_term_takes_later_terms_out_of_arrears(self):
        self.start(self.second)
        self.start(self.first)

        self.assertEqual(self.position().arrears, D("0"))

    def test_a_child_owing_only_arrears_is_on_the_chase_list_and_gets_a_reminder(self):
        structure = FeeStructure.all_objects.get(school_class=self.jss1, term=self.second)
        structure.delete()
        self.start(self.second)

        rows = balances.outstanding(Student.all_objects.filter(pk=self.ada.pk))
        self.assertEqual([row.owed for row in rows], [D("50000")])
        self.assertEqual(owing_students([Student.all_objects.get(pk=self.ada.pk)]),
                         [Student.all_objects.get(pk=self.ada.pk)])

    def test_the_receipt_balance_includes_what_was_already_owed(self):
        self.start(self.second)
        payment = self.pay("20000")

        self.assertEqual(balances.running_balance(payment), D("80000"))

    def test_a_child_withdrawn_later_still_has_the_debt_on_record(self):
        self.start(self.second)
        Student.all_objects.filter(pk=self.ada.pk).update(status=StudentStatus.WITHDRAWN)

        self.assertEqual(self.position().arrears, D("50000"))


class CreditTests(LedgerTestCase):
    def test_paying_too_much_shows_as_credit(self):
        self.pay("70000")

        position = self.position()

        self.assertEqual(position.outstanding, D("0"))
        self.assertEqual(position.credit, D("20000"))
        self.assertEqual(position.state, fees.FeeState.PAID)

    def test_credit_carries_into_the_next_term(self):
        self.pay("70000")
        self.start(self.second)

        position = self.position()

        self.assertEqual(position.arrears, D("-20000"))
        self.assertEqual(position.arrears_outstanding, D("0"))
        self.assertEqual(position.outstanding, D("30000"))
        self.assertEqual(position.total_owed, D("30000"))
        self.assertEqual(position.credit, D("0"))

    def test_a_big_credit_can_cover_a_whole_term(self):
        self.pay("120000")
        self.start(self.second)

        position = self.position()

        self.assertEqual(position.total_owed, D("0"))
        self.assertEqual(position.credit, D("20000"))


class AdjustmentTests(LedgerTestCase):
    def test_a_discount_lowers_what_this_term_expects(self):
        self.adjust(AdjustmentKind.DISCOUNT, "5000")

        position = self.position()

        self.assertEqual(position.expected, D("45000"))
        self.assertEqual(position.adjustment, D("-5000"))

    def test_an_extra_charge_raises_it(self):
        self.adjust(AdjustmentKind.CHARGE, "3000")

        self.assertEqual(self.position().expected, D("53000"))

    def test_a_scholarship_cannot_push_a_term_below_nothing(self):
        self.adjust(AdjustmentKind.SCHOLARSHIP, "90000")

        position = self.position()

        self.assertEqual(position.expected, D("0"))
        self.assertEqual(position.state, fees.FeeState.PAID)

    def test_an_opening_balance_counts_even_with_no_fee_structure(self):
        FeeStructure.all_objects.filter(school_class=self.jss1, term=self.first).delete()
        self.adjust(AdjustmentKind.OPENING_BALANCE, "12000")

        position = self.position()

        self.assertTrue(position.is_priced)
        self.assertEqual(position.total_owed, D("12000"))

    def test_an_earlier_term_adjustment_counts_with_no_enrolment(self):
        """How a school brings in a balance from before it joined."""
        self.start(self.second)
        StudentTermEnrolment.all_objects.filter(student=self.ada, term=self.first).delete()
        self.adjust(AdjustmentKind.OPENING_BALANCE, "12000", term=self.first)

        self.assertEqual(self.position().arrears, D("12000"))

    def test_a_voided_adjustment_stops_counting_and_stays_on_record(self):
        adjustment = self.adjust(AdjustmentKind.DISCOUNT, "5000")

        adjustment.void(by=self.owner, reason="Wrong child")

        self.assertEqual(self.position().expected, D("50000"))
        adjustment.refresh_from_db()
        self.assertEqual(adjustment.void_reason, "Wrong child")
        self.assertIsNotNone(adjustment.voided_at)

    def test_each_child_keeps_their_own_figures(self):
        bola = self.make_student("A2", "Bola")
        self.adjust(AdjustmentKind.DISCOUNT, "5000")

        self.assertEqual(self.position(bola).expected, D("50000"))

    def test_it_is_recorded_in_the_audit_log_with_the_void(self):
        adjustment = self.adjust(AdjustmentKind.DISCOUNT, "5000")
        adjustment.void(by=self.owner, reason="Wrong child")

        created = AuditEvent.all_objects.get(action="studentfeeadjustment.created")
        voided = AuditEvent.all_objects.get(action="studentfeeadjustment.voided")
        self.assertEqual(created.changes["amount"], [None, "5000.00"])
        self.assertEqual(voided.changes["void_reason"], ["", "Wrong child"])

    def test_a_student_with_an_adjustment_cannot_be_deleted(self):
        self.adjust(AdjustmentKind.DISCOUNT, "5000")

        with self.assertRaises(ProtectedError):
            Student.all_objects.filter(pk=self.ada.pk).delete()


class AdjustmentScreenTests(LedgerTestCase):
    def data(self, **overrides):
        data = {"term": self.first.pk, "kind": "discount", "amount": "5000",
                "description": "Staff child"}
        data.update(overrides)
        return data

    def url(self, student=None):
        return reverse("fees:adjustment_create", args=[(student or self.ada).pk])

    def test_the_owner_can_add_one(self):
        self.client.force_login(self.owner)

        response = self.client.post(self.url(), self.data())

        self.assertEqual(response.status_code, 302)
        adjustment = StudentFeeAdjustment.all_objects.get()
        self.assertEqual(adjustment.amount, D("5000"))
        self.assertEqual(adjustment.created_by, self.owner)
        self.assertEqual(adjustment.branch, self.north)

    def test_nobody_below_the_owner_can(self):
        for user in (self.principal, self.bursar):
            self.client.force_login(user)
            self.assertEqual(self.client.post(self.url(), self.data()).status_code, 403)
        self.assertFalse(StudentFeeAdjustment.all_objects.exists())

    def test_a_reason_is_required(self):
        self.client.force_login(self.owner)

        self.assertEqual(self.client.post(self.url(), self.data(description="")).status_code, 200)
        self.assertFalse(StudentFeeAdjustment.all_objects.exists())

    def test_the_amount_has_to_be_more_than_nothing(self):
        self.client.force_login(self.owner)

        for amount in ("0", "-5"):
            self.assertEqual(self.client.post(self.url(), self.data(amount=amount)).status_code, 200)
        self.assertFalse(StudentFeeAdjustment.all_objects.exists())

    def test_another_campus_term_is_refused(self):
        south = Branch.all_objects.create(school=self.school, name="South")
        term = self.make_term(TermSequence.FIRST, branch=south)
        self.client.force_login(self.owner)

        self.assertEqual(self.client.post(self.url(), self.data(term=term.pk)).status_code, 200)
        self.assertFalse(StudentFeeAdjustment.all_objects.exists())

    def test_another_schools_student_is_a_404(self):
        other = School.all_objects.create(name="Bravo", status=SchoolStatus.ACTIVE)
        stranger = User.objects.create_user(
            "stranger", password="pw", role=Role.SCHOOL_OWNER, school=other
        )
        self.client.force_login(stranger)

        self.assertEqual(self.client.post(self.url(), self.data()).status_code, 404)

    def test_voiding_needs_the_owner_and_a_reason(self):
        adjustment = self.adjust(AdjustmentKind.DISCOUNT, "5000")
        url = reverse("fees:adjustment_void", args=[adjustment.pk])

        self.client.force_login(self.bursar)
        self.assertEqual(self.client.post(url, {"reason": "x"}).status_code, 403)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.post(url, {"reason": ""}).status_code, 200)
        self.assertIsNone(StudentFeeAdjustment.all_objects.get().voided_at)
        self.assertEqual(self.client.post(url, {"reason": "Mistake"}).status_code, 302)
        self.assertIsNotNone(StudentFeeAdjustment.all_objects.get().voided_at)
        self.assertEqual(StudentFeeAdjustment.all_objects.get().voided_by, self.owner)

    def test_voiding_twice_changes_nothing(self):
        adjustment = self.adjust(AdjustmentKind.DISCOUNT, "5000")
        url = reverse("fees:adjustment_void", args=[adjustment.pk])
        self.client.force_login(self.owner)
        self.client.post(url, {"reason": "First"})
        self.client.post(url, {"reason": "Second"})

        self.assertEqual(StudentFeeAdjustment.all_objects.get().void_reason, "First")

    def test_another_schools_adjustment_is_a_404(self):
        adjustment = self.adjust(AdjustmentKind.DISCOUNT, "5000")
        other = School.all_objects.create(name="Bravo", status=SchoolStatus.ACTIVE)
        stranger = User.objects.create_user(
            "stranger", password="pw", role=Role.SCHOOL_OWNER, school=other
        )
        self.client.force_login(stranger)

        self.assertEqual(
            self.client.post(
                reverse("fees:adjustment_void", args=[adjustment.pk]), {"reason": "x"}
            ).status_code,
            404,
        )


class StatementScreenTests(LedgerTestCase):
    def detail(self, user=None):
        self.client.force_login(user or self.owner)
        return self.client.get(reverse("students:student_detail", args=[self.ada.pk]))

    def test_each_term_is_listed_with_its_own_figures(self):
        self.pay("10000")
        self.start(self.second)
        self.ada.school_class = self.jss2
        self.ada.save()

        response = self.detail()

        lines = response.context["statement"]
        self.assertEqual([line.term for line in lines], [self.second, self.first])
        past = lines[1]
        self.assertEqual(past.school_class, self.jss1)
        self.assertEqual(past.fees, D("50000"))
        self.assertEqual(past.paid, D("10000"))
        self.assertEqual(past.balance, D("40000"))
        self.assertContains(response, "Fee history")

    def test_arrears_are_a_separate_line(self):
        self.start(self.second)

        response = self.detail()

        self.assertContains(response, "From earlier terms")
        self.assertContains(response, "₦50,000")

    def test_a_reduction_is_written_with_its_sign_in_front(self):
        self.adjust(AdjustmentKind.DISCOUNT, "5000")

        response = self.detail()

        self.assertContains(response, "&minus;₦5,000")
        self.assertNotContains(response, "₦-5,000")

    def test_credit_is_shown(self):
        self.pay("70000")

        self.assertContains(self.detail(), "Credit")

    def test_the_payment_panel_shows_both(self):
        self.start(self.second)
        self.client.force_login(self.bursar)

        response = self.client.get(
            reverse("payments:student_position"), {"student": "A1"}
        )

        self.assertContains(response, "From earlier terms")
        self.assertContains(response, "₦100,000")

    def test_only_the_owner_is_offered_adjustments(self):
        add = reverse("fees:adjustment_create", args=[self.ada.pk])

        self.assertContains(self.detail(self.owner), add)
        self.assertNotContains(self.detail(self.bursar), add)

    def test_a_voided_adjustment_is_shown_as_voided(self):
        self.adjust(AdjustmentKind.DISCOUNT, "5000", description="Staff child").void(
            by=self.owner, reason="Mistake"
        )

        response = self.detail()

        self.assertContains(response, "Staff child")
        self.assertContains(response, "Voided")

    def test_the_outstanding_screen_shows_arrears(self):
        self.start(self.second)
        self.client.force_login(self.bursar)

        response = self.client.get(reverse("payments:outstanding"))

        self.assertContains(response, "Arrears")
        self.assertEqual(response.context["owed_total"], D("100000"))

    def test_the_report_shows_arrears_only_when_there_are_some(self):
        self.client.force_login(self.owner)
        self.assertNotContains(self.client.get(reverse("reports:index")), "From earlier terms")

        self.start(self.second)
        self.assertContains(self.client.get(reverse("reports:index")), "From earlier terms")


class ClosedTermTests(LedgerTestCase):
    def setUp(self):
        self.start(self.second)
        self.old = FeeStructure.all_objects.get(school_class=self.jss1, term=self.first)
        self.client.force_login(self.owner)

    def test_a_term_knows_when_it_is_over(self):
        for term, past in ((self.first, True), (self.second, False), (self.third, False)):
            self.assertEqual(Term.all_objects.get(pk=term.pk).is_past, past)

    def test_an_earlier_terms_fees_cannot_be_edited(self):
        response = self.client.get(reverse("fees:structure_update", args=[self.old.pk]))

        self.assertEqual(response.status_code, 302)
        response = self.client.post(reverse("fees:structure_update", args=[self.old.pk]), {})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(FeeStructure.all_objects.get(pk=self.old.pk).total, D("50000"))

    def test_nor_deleted(self):
        response = self.client.post(reverse("fees:structure_delete", args=[self.old.pk]))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(FeeStructure.all_objects.filter(pk=self.old.pk).exists())

    def test_the_refusal_explains_what_to_do_instead(self):
        response = self.client.get(
            reverse("fees:structure_update", args=[self.old.pk]), follow=True
        )

        self.assertContains(response, "adjustment")

    def test_this_terms_fees_can_still_be_edited(self):
        current = FeeStructure.all_objects.get(school_class=self.jss1, term=self.second)

        self.assertEqual(
            self.client.get(reverse("fees:structure_update", args=[current.pk])).status_code, 200
        )

    def test_a_new_structure_cannot_be_priced_into_an_earlier_term(self):
        new_class = Class.all_objects.create(
            branch=self.north, name="JSS 3", level=Level.JUNIOR_SECONDARY, year_in_level=3
        )

        response = self.client.post(
            reverse("fees:structure_create"),
            {"school_class": new_class.pk, "term": self.first.pk,
             "components-TOTAL_FORMS": "0", "components-INITIAL_FORMS": "0"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(FeeStructure.all_objects.filter(school_class=new_class).exists())

    def test_the_import_does_not_offer_an_earlier_term(self):
        from apps.fees.views import FeeImportFlow

        with scope_to(school_id=self.school.pk, role=Role.SCHOOL_OWNER):
            self.assertNotIn(self.first, list(FeeImportFlow().scope_queryset()))


class ClassHistoryIsKeptTests(LedgerTestCase):
    def test_a_class_with_history_is_refused_with_an_answer_rather_than_a_crash(self):
        self.start(self.second)
        self.ada.school_class = self.jss2
        self.ada.save()
        self.client.force_login(self.owner)

        response = self.client.post(
            reverse("academics:class_delete", args=[self.jss1.pk]), follow=True
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(Class.all_objects.filter(pk=self.jss1.pk).exists())
        self.assertContains(response, "deactivate")


class TheCostOfAPageTests(LedgerTestCase):
    def test_a_long_page_costs_no_more_queries_than_a_short_one(self):
        self.start(self.second)
        one = list(Student.all_objects.filter(pk=self.ada.pk))
        with CaptureQueriesContext(connection) as small:
            schedule = fees.load(one)
            [schedule.position_for(s) for s in one]

        for number in range(12):
            self.make_student(f"B{number}", "More")
        self.start(self.third)
        self.start(self.second)
        many = list(Student.all_objects.all())
        with CaptureQueriesContext(connection) as large:
            schedule = fees.load(many)
            [schedule.position_for(s) for s in many]

        self.assertEqual(len(large), len(small))


class TenancyTests(LedgerTestCase):
    def test_a_scoped_user_sees_a_childs_whole_balance(self):
        self.start(self.second)

        with scope_to(school_id=self.school.pk, branch_id=self.north.pk, role=Role.BURSAR):
            self.assertEqual(fees.position_for(self.ada).arrears, D("50000"))

    def test_another_schools_adjustments_are_invisible(self):
        self.adjust(AdjustmentKind.DISCOUNT, "5000")
        other = School.all_objects.create(name="Bravo")

        with scope_to(school_id=other.pk, role=Role.SCHOOL_OWNER):
            self.assertFalse(StudentFeeAdjustment.objects.exists())
            self.assertFalse(StudentTermEnrolment.objects.exists())


class ClosingASchoolTests(LedgerTestCase):
    def test_a_school_with_a_full_ledger_can_still_be_closed(self):
        self.pay("10000")
        self.adjust(AdjustmentKind.DISCOUNT, "5000")
        self.start(self.second)

        teardown.remove(self.school)

        self.assertFalse(School.all_objects.filter(pk=self.school.pk).exists())
        self.assertFalse(StudentFeeAdjustment.all_objects.exists())
        self.assertFalse(StudentTermEnrolment.all_objects.exists())
