"""Tests for the student roster.

The rules that must never regress: a branch's roster is its own, admission
numbers collide within a branch and nowhere else, a bursar can look but not
touch, and what a student owes is always read from their class's live fee
structure rather than stored anywhere on them.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.db.models import ProtectedError
from django.db.utils import IntegrityError
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.academics.models import Class, Level
from apps.core.permissions import Capability, has_capability
from apps.core.roles import Role
from apps.core.tenancy import scope_to
from apps.fees.models import FeeComponent, FeeStructure, Term, TermSequence
from apps.schools.models import Branch, School

from . import fees as fee_position
from .forms import StudentForm
from .models import Student, StudentStatus
from .validators import format_phone, normalise_phone, validate_phone

User = get_user_model()


def student_post(**overrides) -> dict:
    """A complete, valid POST body for the student form."""
    data = {
        "admission_number": "FA/2025/099",
        "first_name": "Chinaza",
        "last_name": "Okonkwo",
        "other_names": "Adaeze",
        "sex": "female",
        "date_of_birth": "2019-04-12",
        "date_admitted": "2025-09-08",
        "status": StudentStatus.ACTIVE,
        "parent_name": "Mrs. Ngozi Okonkwo",
        "parent_phone": "08034129876",
        "parent_email": "ngozi.okonkwo@example.com",
        "address": "14 Adeniyi Jones Avenue, Ikeja, Lagos",
    }
    data.update(overrides)
    return data


class StudentTestCase(TestCase):
    """One school with two branches, plus a second school."""

    @classmethod
    def setUpTestData(cls):
        cls.alpha = School.all_objects.create(name="Alpha Schools")
        cls.north = Branch.all_objects.create(school=cls.alpha, name="North")
        cls.south = Branch.all_objects.create(school=cls.alpha, name="South")

        cls.alpha_owner = User.objects.create_user(
            "alpha.owner", password="pw", role=Role.SCHOOL_OWNER, school=cls.alpha
        )
        cls.north_principal = User.objects.create_user(
            "north.principal", password="pw", role=Role.PRINCIPAL,
            school=cls.alpha, branch=cls.north,
        )
        cls.north_bursar = User.objects.create_user(
            "north.bursar", password="pw", role=Role.BURSAR,
            school=cls.alpha, branch=cls.north,
        )

        cls.north_p1 = Class.all_objects.create(
            branch=cls.north, name="Primary 1", level=Level.PRIMARY, year_in_level=1
        )
        cls.north_jss1 = Class.all_objects.create(
            branch=cls.north, name="JSS 1", level=Level.JUNIOR_SECONDARY, year_in_level=1
        )
        cls.south_p1 = Class.all_objects.create(
            branch=cls.south, name="Primary 1", level=Level.PRIMARY, year_in_level=1
        )

        cls.north_term = Term.all_objects.create(
            branch=cls.north, name="First Term 2025/2026",
            academic_year="2025/2026", sequence=TermSequence.FIRST, is_current=True,
        )
        cls.north_p1_fees = FeeStructure.all_objects.create(
            school_class=cls.north_p1, term=cls.north_term
        )
        for name, amount in [("School Fees", 26000), ("Textbooks", 50000),
                             ("Development", 2000)]:
            FeeComponent.all_objects.create(
                fee_structure=cls.north_p1_fees, name=name, amount=Decimal(amount)
            )

        cls.chinaza = Student.all_objects.create(
            school_class=cls.north_p1,
            admission_number="FA/2025/001",
            first_name="Chinaza", last_name="Okonkwo", other_names="Adaeze",
            sex="female", date_of_birth=date(2019, 4, 12),
            date_admitted=date(2025, 9, 8), parent_name="Mrs. Ngozi Okonkwo",
            parent_phone="08034129876",
        )
        cls.emeka = Student.all_objects.create(
            school_class=cls.north_jss1,
            admission_number="FA/2020/007",
            first_name="Emeka", last_name="Obi", sex="male",
            date_admitted=date(2020, 9, 14), parent_name="Mr. Kelechi Obi",
            parent_phone="09062218843",
        )
        cls.south_student = Student.all_objects.create(
            school_class=cls.south_p1,
            admission_number="FA/2025/001",  # same number, other branch
            first_name="Tobiloba", last_name="Adeyemi", sex="male",
            date_admitted=date(2025, 9, 8), parent_name="Mr. Femi Adeyemi",
            parent_phone="08160034215",
        )

    def as_user(self, user):
        return scope_to(
            school_id=user.school_id, branch_id=user.branch_id, role=user.role
        )


class ModelTests(StudentTestCase):
    def test_branch_and_school_are_derived_from_the_class(self):
        self.assertEqual(self.chinaza.branch_id, self.north.pk)
        self.assertEqual(self.chinaza.school_id, self.alpha.pk)

    def test_admission_numbers_collide_within_a_branch(self):
        with self.assertRaises(IntegrityError):
            Student.all_objects.create(
                school_class=self.north_jss1, admission_number="FA/2025/001",
                first_name="Someone", last_name="Else", sex="male",
                parent_name="A Parent", parent_phone="08034129876",
            )

    def test_the_collision_is_case_insensitive(self):
        with self.assertRaises(IntegrityError):
            Student.all_objects.create(
                school_class=self.north_jss1, admission_number="fa/2025/001",
                first_name="Someone", last_name="Else", sex="male",
                parent_name="A Parent", parent_phone="08034129876",
            )

    def test_two_branches_may_both_use_the_same_number(self):
        """The point of scoping uniqueness to the branch."""
        self.assertEqual(
            self.chinaza.admission_number, self.south_student.admission_number
        )
        self.assertNotEqual(self.chinaza.branch_id, self.south_student.branch_id)

    def test_whitespace_around_an_admission_number_is_trimmed(self):
        student = Student.all_objects.create(
            school_class=self.north_jss1, admission_number="  FA/2025/500  ",
            first_name="Ada", last_name="Eze", sex="female",
            parent_name="A Parent", parent_phone="08034129876",
        )
        self.assertEqual(student.admission_number, "FA/2025/500")

    def test_a_class_with_students_cannot_be_deleted(self):
        """Deleting the class would leave the roster without an enrolment."""
        with self.assertRaises(ProtectedError):
            self.north_p1.delete()

    def test_a_class_from_another_branch_is_rejected(self):
        student = Student(
            school_class=self.south_p1, branch=self.north,
            admission_number="FA/2025/900", first_name="A", last_name="B",
            sex="male", parent_name="P", parent_phone="08034129876",
        )
        with self.assertRaises(ValidationError):
            student.full_clean()

    def test_a_birth_date_after_admission_is_rejected(self):
        student = Student(
            school_class=self.north_p1, admission_number="FA/2025/901",
            first_name="A", last_name="B", sex="male",
            date_of_birth=date(2026, 1, 1), date_admitted=date(2025, 9, 8),
            parent_name="P", parent_phone="08034129876",
        )
        with self.assertRaises(ValidationError):
            student.full_clean()

    def test_name_properties(self):
        self.assertEqual(self.chinaza.full_name, "Chinaza Okonkwo")
        self.assertEqual(self.chinaza.formal_name, "Okonkwo, Chinaza Adaeze")
        self.assertEqual(self.emeka.formal_name, "Obi, Emeka")
        self.assertEqual(self.chinaza.initials, "CO")

    def test_age_is_none_without_a_date_of_birth(self):
        self.assertIsNone(self.emeka.age)
        self.assertIsNotNone(self.chinaza.age)

    def test_students_order_by_the_admission_ladder(self):
        with self.as_user(self.north_principal):
            names = [s.first_name for s in Student.objects.all()]
        self.assertEqual(names, ["Chinaza", "Emeka"])

    def test_no_fee_columns_live_on_the_student(self):
        """Expected fees are derived, never copied onto the roster."""
        columns = {f.name for f in Student._meta.get_fields()}
        for forbidden in ("expected_fee", "total", "balance", "outstanding", "amount"):
            self.assertNotIn(forbidden, columns)


class PhoneTests(TestCase):
    def test_accepts_the_shapes_a_number_gets_written_in(self):
        for value in ("08034129876", "0803 412 9876", "0803-412-9876",
                      "+234 803 412 9876", "2348034129876"):
            self.assertEqual(normalise_phone(value), "08034129876", value)
            validate_phone(value)

    def test_rejects_a_number_that_is_not_a_nigerian_mobile(self):
        for value in ("0123456789", "080341298", "1234567890123", "0603412987"):
            with self.assertRaises(ValidationError, msg=value):
                validate_phone(value)

    def test_display_grouping(self):
        self.assertEqual(format_phone("08034129876"), "0803 412 9876")


class ScopingTests(StudentTestCase):
    def test_principal_sees_only_their_branchs_students(self):
        with self.as_user(self.north_principal):
            self.assertCountEqual(Student.objects.all(), [self.chinaza, self.emeka])

    def test_school_owner_sees_both_branches(self):
        with self.as_user(self.alpha_owner):
            self.assertEqual(Student.objects.count(), 3)

    def test_another_branchs_student_is_a_404(self):
        self.client.force_login(self.north_principal)
        for name in ("student_detail", "student_update", "student_remove"):
            response = self.client.get(
                reverse(f"students:{name}", args=[self.south_student.pk])
            )
            self.assertEqual(response.status_code, 404, name)


class CapabilityTests(TestCase):
    def test_leadership_roles_manage_students(self):
        for role in (Role.PLATFORM_OWNER, Role.SCHOOL_OWNER, Role.PRINCIPAL):
            self.assertTrue(
                has_capability(User(role=role), Capability.MANAGE_STUDENTS), role
            )

    def test_bursar_views_but_does_not_manage(self):
        """A bursar needs to find a student to take money from them."""
        bursar = User(role=Role.BURSAR)
        self.assertTrue(has_capability(bursar, Capability.VIEW_STUDENTS))
        self.assertFalse(has_capability(bursar, Capability.MANAGE_STUDENTS))


class ViewPermissionTests(StudentTestCase):
    def test_bursar_can_read_the_list_and_a_detail_page(self):
        self.client.force_login(self.north_bursar)
        self.assertEqual(
            self.client.get(reverse("students:student_list")).status_code, 200
        )
        self.assertEqual(
            self.client.get(
                reverse("students:student_detail", args=[self.chinaza.pk])
            ).status_code,
            200,
        )

    def test_bursar_cannot_reach_create_edit_or_delete(self):
        self.client.force_login(self.north_bursar)
        cases = [
            reverse("students:student_create"),
            reverse("students:student_update", args=[self.chinaza.pk]),
            reverse("students:student_remove", args=[self.chinaza.pk]),
        ]
        for url in cases:
            self.assertEqual(self.client.get(url).status_code, 403, url)

    def test_bursar_cannot_post_a_student(self):
        self.client.force_login(self.north_bursar)
        before = Student.all_objects.count()
        response = self.client.post(
            reverse("students:student_create"),
            student_post(school_class=self.north_p1.pk),
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Student.all_objects.count(), before)

    def test_bursar_cannot_remove_a_student(self):
        self.client.force_login(self.north_bursar)
        response = self.client.post(
            reverse("students:student_remove", args=[self.chinaza.pk])
        )
        self.assertEqual(response.status_code, 403)
        self.chinaza.refresh_from_db()
        self.assertEqual(self.chinaza.status, StudentStatus.ACTIVE)

    def test_bursar_list_hides_edit_controls(self):
        self.client.force_login(self.north_bursar)
        body = self.client.get(reverse("students:student_list")).content.decode()
        self.assertNotIn("New student", body)

    def test_principal_sees_edit_controls(self):
        self.client.force_login(self.north_principal)
        body = self.client.get(reverse("students:student_list")).content.decode()
        self.assertIn("Add student", body)

    def test_anonymous_is_redirected_to_login(self):
        response = self.client.get(reverse("students:student_list"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.headers["Location"])


class ListViewTests(StudentTestCase):
    def setUp(self):
        self.client.force_login(self.north_principal)

    def rows(self, **params):
        response = self.client.get(reverse("students:student_list"), params)
        return response, [row["student"] for row in response.context["rows"]]

    def test_lists_the_branchs_students(self):
        _, students = self.rows()
        self.assertCountEqual(students, [self.chinaza, self.emeka])

    def test_search_by_first_or_last_name(self):
        for term in ("Chinaza", "okonkwo", "adaeze"):
            _, students = self.rows(q=term)
            self.assertEqual(students, [self.chinaza], term)

    def test_search_by_admission_number_whole_or_fragment(self):
        for term in ("FA/2025/001", "2025/001", "001"):
            _, students = self.rows(q=term)
            self.assertEqual(students, [self.chinaza], term)

    def test_filter_by_class(self):
        _, students = self.rows(school_class=self.north_jss1.pk)
        self.assertEqual(students, [self.emeka])

    def test_filter_by_status(self):
        self.emeka.status = StudentStatus.GRADUATED
        self.emeka.save()
        _, students = self.rows(status=StudentStatus.GRADUATED)
        self.assertEqual(students, [self.emeka])

    def test_defaults_to_active_students_only(self):
        self.emeka.status = StudentStatus.WITHDRAWN
        self.emeka.save()
        _, students = self.rows()
        self.assertEqual(students, [self.chinaza])

    def test_an_explicitly_empty_status_shows_everyone(self):
        self.emeka.status = StudentStatus.WITHDRAWN
        self.emeka.save()
        _, students = self.rows(status="")
        self.assertCountEqual(students, [self.chinaza, self.emeka])

    def test_class_filter_choices_are_limited_to_the_branch(self):
        response, _ = self.rows()
        choices = response.context["filter_form"].fields["school_class"].queryset
        self.assertNotIn(self.south_p1, choices)

    def test_the_list_is_paginated(self):
        for index in range(30):
            Student.all_objects.create(
                school_class=self.north_jss1,
                admission_number=f"FA/2025/1{index:02d}",
                first_name=f"Student{index}", last_name="Test", sex="male",
                parent_name="A Parent", parent_phone="08034129876",
            )
        response, students = self.rows()
        self.assertEqual(len(students), 25)
        self.assertEqual(response.context["page_obj"].paginator.count, 32)

    def test_the_page_costs_the_same_however_many_students_are_on_it(self):
        """Fee positions must not become one lookup per student."""
        url = reverse("students:student_list")
        with CaptureQueriesContext(connection) as two_students:
            self.client.get(url)

        for index in range(10):
            Student.all_objects.create(
                school_class=self.north_p1,
                admission_number=f"FA/2025/2{index:02d}",
                first_name=f"Student{index}", last_name="Test", sex="male",
                parent_name="A Parent", parent_phone="08034129876",
            )
        with CaptureQueriesContext(connection) as twelve_students:
            self.client.get(url)

        self.assertEqual(len(twelve_students), len(two_students))


class FeePositionTests(StudentTestCase):
    def test_expected_is_the_total_of_the_classs_structure(self):
        position = fee_position.position_for(self.chinaza)
        self.assertEqual(position.expected, Decimal("78000"))
        self.assertEqual(position.term, self.north_term)

    def test_it_follows_a_repriced_class(self):
        """The whole reason the figure is not stored on the student."""
        self.north_p1_fees.components.filter(name="Textbooks").update(
            amount=Decimal("60000")
        )
        self.assertEqual(
            fee_position.position_for(self.chinaza).expected, Decimal("88000")
        )

    def test_a_class_with_no_structure_reads_as_unpriced(self):
        position = fee_position.position_for(self.emeka)
        self.assertFalse(position.is_priced)
        self.assertEqual(position.expected, Decimal("0"))
        self.assertEqual(position.state, fee_position.FeeState.UNPRICED)
        self.assertEqual(position.pill_label, "No fees set")

    def test_no_current_term_means_no_position(self):
        Term.all_objects.filter(pk=self.north_term.pk).update(is_current=False)
        position = fee_position.position_for(self.chinaza)
        self.assertIsNone(position.term)
        self.assertFalse(position.is_priced)

    def test_outstanding_is_the_whole_fee_until_payments_exist(self):
        position = fee_position.position_for(self.chinaza)
        self.assertEqual(position.paid, Decimal("0"))
        self.assertEqual(position.outstanding, Decimal("78000"))
        self.assertEqual(position.state, fee_position.FeeState.UNPAID)

    def test_the_schedule_handles_a_page_spanning_two_branches(self):
        """A school owner's list can mix branches with different terms."""
        south_term = Term.all_objects.create(
            branch=self.south, name="First Term 2025/2026",
            academic_year="2025/2026", sequence=TermSequence.FIRST, is_current=True,
        )
        structure = FeeStructure.all_objects.create(
            school_class=self.south_p1, term=south_term
        )
        FeeComponent.all_objects.create(
            fee_structure=structure, name="School Fees", amount=Decimal("99000")
        )
        with self.as_user(self.alpha_owner):
            students = list(Student.objects.all())
            schedule = fee_position.load(students)
            found = {
                s.admission_number + s.branch.name: schedule.position_for(s).expected
                for s in students
            }
        self.assertEqual(found["FA/2025/001North"], Decimal("78000"))
        self.assertEqual(found["FA/2025/001South"], Decimal("99000"))

    def test_the_detail_page_shows_the_line_items_behind_the_total(self):
        self.client.force_login(self.north_principal)
        response = self.client.get(
            reverse("students:student_detail", args=[self.chinaza.pk])
        )
        self.assertEqual(response.context["position"].expected, Decimal("78000"))
        self.assertEqual(
            [c.name for c in response.context["fee_components"]],
            ["School Fees", "Textbooks", "Development"],
        )


class EditingTests(StudentTestCase):
    def setUp(self):
        self.client.force_login(self.north_principal)

    def test_the_add_and_edit_screens_render(self):
        for url in (
            reverse("students:student_create"),
            reverse("students:student_update", args=[self.chinaza.pk]),
        ):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)
            self.assertContains(response, "Parent / guardian")

    def test_the_add_screen_can_arrive_with_a_class_chosen(self):
        response = self.client.get(
            reverse("students:student_create"), {"school_class": self.north_jss1.pk}
        )
        self.assertEqual(
            response.context["form"].initial["school_class"], self.north_jss1
        )

    def test_principal_enrols_a_student(self):
        response = self.client.post(
            reverse("students:student_create"),
            student_post(school_class=self.north_jss1.pk, admission_number="FA/2025/050"),
        )
        self.assertEqual(response.status_code, 302)
        student = Student.all_objects.get(admission_number="FA/2025/050")
        self.assertEqual(student.branch_id, self.north.pk)
        self.assertEqual(student.school_id, self.alpha.pk)
        self.assertEqual(student.status, StudentStatus.ACTIVE)

    def test_a_duplicate_admission_number_is_a_field_error_not_a_500(self):
        response = self.client.post(
            reverse("students:student_create"),
            student_post(school_class=self.north_jss1.pk, admission_number="FA/2025/001"),
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("admission_number", response.context["form"].errors)

    def test_the_same_number_is_accepted_at_another_branch(self):
        self.client.force_login(self.alpha_owner)
        response = self.client.post(
            reverse("students:student_create"),
            student_post(school_class=self.south_p1.pk, admission_number="FA/2020/007"),
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            Student.all_objects.filter(admission_number="FA/2020/007").count(), 2
        )

    def test_a_phone_number_is_stored_in_one_canonical_form(self):
        self.client.post(
            reverse("students:student_create"),
            student_post(
                school_class=self.north_jss1.pk, admission_number="FA/2025/051",
                parent_phone="+234 803 412 9876",
            ),
        )
        student = Student.all_objects.get(admission_number="FA/2025/051")
        self.assertEqual(student.parent_phone, "08034129876")

    def test_an_invalid_phone_number_is_rejected(self):
        response = self.client.post(
            reverse("students:student_create"),
            student_post(
                school_class=self.north_jss1.pk, admission_number="FA/2025/052",
                parent_phone="12345",
            ),
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("parent_phone", response.context["form"].errors)
        self.assertFalse(Student.all_objects.filter(admission_number="FA/2025/052"))

    def test_editing_a_student_keeps_their_own_number(self):
        response = self.client.post(
            reverse("students:student_update", args=[self.chinaza.pk]),
            student_post(
                school_class=self.north_p1.pk, admission_number="FA/2025/001",
                first_name="Chinaza", last_name="Okonkwo",
            ),
        )
        self.assertEqual(response.status_code, 302)

    def test_moving_a_student_to_another_class_changes_what_they_owe(self):
        jss_structure = FeeStructure.all_objects.create(
            school_class=self.north_jss1, term=self.north_term
        )
        FeeComponent.all_objects.create(
            fee_structure=jss_structure, name="School Fees", amount=Decimal("112000")
        )
        self.client.post(
            reverse("students:student_update", args=[self.chinaza.pk]),
            student_post(
                school_class=self.north_jss1.pk, admission_number="FA/2025/001",
            ),
        )
        self.chinaza.refresh_from_db()
        self.assertEqual(
            fee_position.position_for(self.chinaza).expected, Decimal("112000")
        )

    def test_principal_removes_a_student_by_withdrawing_them(self):
        # EditingTests signs in as the principal. "Remove" is a status change:
        # the row stays, and only its status moves.
        response = self.client.post(
            reverse("students:student_remove", args=[self.emeka.pk])
        )
        self.assertRedirects(response, reverse("students:student_list"))
        self.emeka.refresh_from_db()
        self.assertEqual(self.emeka.status, StudentStatus.WITHDRAWN)
        self.assertTrue(Student.all_objects.filter(pk=self.emeka.pk).exists())

    def test_removing_keeps_the_payment_history(self):
        from apps.payments.models import Payment

        payment = Payment.all_objects.create(
            school=self.alpha, branch=self.north, student=self.chinaza,
            term=self.north_term, amount=Decimal("20000"),
        )
        response = self.client.post(
            reverse("students:student_remove", args=[self.chinaza.pk])
        )
        self.assertEqual(response.status_code, 302)
        self.chinaza.refresh_from_db()
        self.assertEqual(self.chinaza.status, StudentStatus.WITHDRAWN)
        payment.refresh_from_db()
        self.assertEqual(payment.student_id, self.chinaza.pk)

    def test_a_removed_student_leaves_the_default_roster(self):
        self.client.post(reverse("students:student_remove", args=[self.emeka.pk]))
        response = self.client.get(reverse("students:student_list"))
        self.assertNotContains(response, "FA/2020/007")

    def test_owner_removes_a_student_the_same_way(self):
        self.client.force_login(self.alpha_owner)
        self.client.post(reverse("students:student_remove", args=[self.emeka.pk]))
        self.emeka.refresh_from_db()
        self.assertEqual(self.emeka.status, StudentStatus.WITHDRAWN)

    def test_the_remove_page_says_it_keeps_the_record(self):
        response = self.client.get(
            reverse("students:student_remove", args=[self.emeka.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "withdrawn")
        self.assertContains(response, "payments and receipts are kept")
        self.assertNotContains(response, "cannot be undone")

    def test_principal_still_cannot_change_classes_or_fees(self):
        # Removing students does not widen the principal's read-only setup.
        principal = User(role=Role.PRINCIPAL)
        self.assertFalse(has_capability(principal, Capability.MANAGE_ACADEMICS))
        self.assertFalse(has_capability(principal, Capability.MANAGE_FEES))
        self.assertFalse(has_capability(principal, Capability.MANAGE_SCHOOL_PROFILE))


class FormScopingTests(StudentTestCase):
    def test_class_choices_are_limited_to_the_branch(self):
        with self.as_user(self.north_principal):
            form = StudentForm()
            self.assertNotIn(self.south_p1, form.fields["school_class"].queryset)

    def test_a_school_owner_cannot_post_another_schools_class(self):
        beta = School.all_objects.create(name="Beta College")
        beta_branch = Branch.all_objects.create(school=beta, name="Beta Main")
        beta_class = Class.all_objects.create(
            branch=beta_branch, name="Primary 1", level=Level.PRIMARY, year_in_level=1
        )
        self.client.force_login(self.alpha_owner)
        response = self.client.post(
            reverse("students:student_create"),
            student_post(school_class=beta_class.pk, admission_number="FA/2025/060"),
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("school_class", response.context["form"].errors)

    def test_an_inactive_class_stays_selectable_for_a_student_already_in_it(self):
        Class.all_objects.filter(pk=self.north_p1.pk).update(is_active=False)
        with self.as_user(self.north_principal):
            form = StudentForm(instance=self.chinaza)
            self.assertIn(self.north_p1, form.fields["school_class"].queryset)


class SeedCommandTests(TestCase):
    def _seed(self):
        out = StringIO()
        call_command("seed_academics", "--create-school", stdout=StringIO())
        call_command("seed_fees", stdout=StringIO())
        call_command("seed_students", stdout=out)
        return out.getvalue()

    def test_the_roster_lands_across_several_classes(self):
        self._seed()
        self.assertEqual(Student.all_objects.count(), 60)
        classes = {s.school_class.display_name for s in Student.all_objects.all()}
        self.assertEqual(len(classes), 9)

    def test_every_seeded_phone_number_is_valid(self):
        self._seed()
        for student in Student.all_objects.all():
            validate_phone(student.parent_phone)

    def test_admission_numbers_are_unique_within_the_branch(self):
        self._seed()
        numbers = list(
            Student.all_objects.values_list("admission_number", flat=True)
        )
        self.assertEqual(len(numbers), len(set(numbers)))

    def test_siblings_share_a_guardian(self):
        self._seed()
        okonkwo = Student.all_objects.filter(last_name="Okonkwo")
        self.assertEqual(okonkwo.count(), 2)
        self.assertEqual(len({s.parent_phone for s in okonkwo}), 1)
        # ...in different classes, which is what will make grouping a family
        # interesting later.
        self.assertEqual(len({s.school_class_id for s in okonkwo}), 2)

    def test_seeding_twice_does_not_duplicate_anyone(self):
        self._seed()
        call_command("seed_students", stdout=StringIO())
        self.assertEqual(Student.all_objects.count(), 60)

    def test_the_roster_carries_students_who_have_left(self):
        """So the status filter has something real to filter."""
        self._seed()
        statuses = set(Student.all_objects.values_list("status", flat=True))
        self.assertIn(StudentStatus.WITHDRAWN, statuses)
        self.assertIn(StudentStatus.INACTIVE, statuses)

    def test_seeded_students_have_a_fee_position(self):
        self._seed()
        student = Student.all_objects.filter(school_class__name="Primary 1").first()
        self.assertEqual(
            # School fees, development and textbooks: uniform is an intake item.
            fee_position.position_for(student).expected, Decimal("78000")
        )

    def test_ages_match_the_classes(self):
        """A KG child is not sixteen; an SSS 3 child is not four."""
        self._seed()
        for student in Student.all_objects.select_related("school_class"):
            age_at_admission = (
                student.date_admitted - student.date_of_birth
            ).days // 365
            self.assertGreaterEqual(age_at_admission, 2, student.admission_number)
            self.assertLessEqual(age_at_admission, 18, student.admission_number)

    def test_refuses_to_run_before_classes_exist(self):
        School.all_objects.create(name="Fulfilled Academy")
        with self.assertRaises(CommandError):
            call_command("seed_students", stdout=StringIO())

    def test_replace_drops_students_the_roster_no_longer_lists(self):
        self._seed()
        branch = Student.all_objects.first().branch
        klass = Student.all_objects.first().school_class
        Student.all_objects.create(
            branch=branch, school_class=klass, admission_number="FA/1999/001",
            first_name="Old", last_name="Record", sex="male",
            parent_name="A Parent", parent_phone="08034129876",
        )
        call_command("seed_students", "--replace", stdout=StringIO())
        self.assertFalse(
            Student.all_objects.filter(admission_number="FA/1999/001").exists()
        )


class NavigationTests(TestCase):
    def test_every_role_gets_the_students_link(self):
        from apps.core.navigation import nav_for

        for role in Role.values:
            items = [
                item
                for section in nav_for(role, "/")
                if section.label == "School"
                for item in section.items
            ]
            self.assertIn("Students", {item.label for item in items}, role)

    def test_the_students_link_resolves(self):
        from apps.core.navigation import nav_for

        sections = {s.label: s.items for s in nav_for(Role.BURSAR, "/")}
        item = next(i for i in sections["School"] if i.label == "Students")
        self.assertTrue(item.available)
