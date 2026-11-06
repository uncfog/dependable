"""Tests for closing a school from the Django admin.

The behaviour being fixed: four foreign keys in this codebase are ``PROTECT``,
and Django's delete view refuses to offer a button past one of them. Deleting a
school meant emptying its payments by hand, then its students, then its classes,
one changelist at a time.

What must hold now:

* a school with payments, students and classes behind it deletes in one
  confirmation, and every one of those rows goes;
* it is still **all or nothing** -- a teardown that cannot finish leaves the
  school exactly as it was;
* the ``PROTECT`` rules themselves are untouched, so deleting a *class* that has
  students on it is still refused. That is the rule the teardown is an exception
  to, not a rule it removes;
* one school's teardown cannot reach another school's rows;
* a staff account that may not delete payments does not acquire that power by
  going through a school.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db.models import ProtectedError
from django.test import TestCase
from django.urls import reverse

from apps.academics.models import Class, Level, Subject
from apps.core.roles import Role
from apps.fees.models import FeeComponent, FeeStructure, Term, TermSequence
from apps.payments.models import Payment
from apps.students.models import Student

from . import teardown
from .models import Branch, School

User = get_user_model()


class SchoolTeardownTestCase(TestCase):
    """Two fully populated schools, so the cross-tenant case has something real.

    "Fully populated" means down to a payment, because the payment is what every
    PROTECT in this schema is ultimately guarding.
    """

    @classmethod
    def setUpTestData(cls):
        cls.alpha, cls.alpha_bits = cls.build_school("Alpha Schools")
        cls.beta, cls.beta_bits = cls.build_school("Beta College")

        cls.superuser = User.objects.create_superuser(
            "root", email="root@example.com", password="pw"
        )

    @classmethod
    def build_school(cls, name):
        school = School.all_objects.create(name=name, slug=name.lower().replace(" ", "-"))
        branch = Branch.all_objects.create(school=school, name="Main")

        owner = User.objects.create_user(
            f"{school.slug}.owner", password="pw",
            role=Role.SCHOOL_OWNER, school=school,
        )
        klass = Class.all_objects.create(
            branch=branch, name="Primary 1", level=Level.PRIMARY, year_in_level=1
        )
        subject = Subject.all_objects.create(branch=branch, name="Mathematics")
        subject.classes.set([klass])

        term = Term.all_objects.create(
            branch=branch, name="First Term 2025/2026", academic_year="2025/2026",
            sequence=TermSequence.FIRST, is_current=True,
        )
        structure = FeeStructure.all_objects.create(school_class=klass, term=term)
        FeeComponent.all_objects.create(
            fee_structure=structure, name="Tuition", amount=Decimal("45000")
        )
        student = Student.all_objects.create(
            school_class=klass, admission_number="X/001",
            first_name="Ada", last_name="Obi", sex="female",
            parent_name="A Parent", parent_phone="08034129876",
        )
        payment = Payment.all_objects.create(
            student=student, term=term, amount=Decimal("20000"),
            date_paid=date(2026, 1, 12),
        )
        return school, {
            "branch": branch, "owner": owner, "class": klass, "subject": subject,
            "term": term, "structure": structure, "student": student,
            "payment": payment,
        }

    def signed_in_admin(self):
        self.client.force_login(self.superuser)
        return self.superuser


class ProtectionIsStillInPlaceTests(SchoolTeardownTestCase):
    """The rules the teardown is an exception to, not a replacement for."""

    def test_a_class_with_students_on_it_still_cannot_be_deleted(self):
        with self.assertRaises(ProtectedError):
            self.alpha_bits["class"].delete()

    def test_a_student_with_payments_still_cannot_be_deleted(self):
        with self.assertRaises(ProtectedError):
            self.alpha_bits["student"].delete()

    def test_a_term_with_payments_still_cannot_be_deleted(self):
        with self.assertRaises(ProtectedError):
            self.alpha_bits["term"].delete()

    def test_and_a_plain_school_delete_is_still_refused(self):
        """``School.delete()`` is unchanged. Only the teardown goes through, so
        nothing that calls delete() by accident takes a tenant with it."""
        with self.assertRaises(ProtectedError):
            self.alpha.delete()


class RemoveTests(SchoolTeardownTestCase):
    """The teardown itself."""

    def test_it_removes_the_school_and_everything_behind_it(self):
        teardown.remove(self.alpha)

        self.assertFalse(School.all_objects.filter(pk=self.alpha.pk).exists())
        for model, label in (
            (Branch, "branch"), (Class, "class"), (Subject, "subject"),
            (Term, "term"), (FeeStructure, "structure"), (Student, "student"),
            (Payment, "payment"),
        ):
            manager = getattr(model, "all_objects", model._default_manager)
            self.assertFalse(
                manager.filter(pk=self.alpha_bits[label].pk).exists(), label
            )

    def test_the_payment_goes_even_though_it_protects_two_things(self):
        """The payment is the row that used to stop all of this: it PROTECTs the
        student and the term at once."""
        teardown.remove(self.alpha)
        self.assertFalse(
            Payment.all_objects.filter(pk=self.alpha_bits["payment"].pk).exists()
        )

    def test_the_schools_accounts_go_with_it(self):
        teardown.remove(self.alpha)
        self.assertFalse(
            User.objects.filter(pk=self.alpha_bits["owner"].pk).exists()
        )

    def test_platform_accounts_are_untouched(self):
        """A platform account has no school, so nothing cascades to it."""
        teardown.remove(self.alpha)
        self.assertTrue(User.objects.filter(pk=self.superuser.pk).exists())

    def test_it_cannot_reach_another_school(self):
        teardown.remove(self.alpha)

        self.assertTrue(School.all_objects.filter(pk=self.beta.pk).exists())
        for model, label in (
            (Branch, "branch"), (Class, "class"), (Subject, "subject"),
            (Term, "term"), (Student, "student"), (Payment, "payment"),
        ):
            manager = getattr(model, "all_objects", model._default_manager)
            self.assertTrue(
                manager.filter(pk=self.beta_bits[label].pk).exists(), label
            )
        self.assertTrue(User.objects.filter(pk=self.beta_bits["owner"].pk).exists())

    def test_it_reports_what_it_removed(self):
        removed = teardown.remove(self.alpha)
        total = sum(removed.values())
        # The school, a branch, a class, a subject (and its m2m row), a term, a
        # structure, a component, a student, a payment and an owner, at least.
        self.assertGreaterEqual(total, 10)
        self.assertIn("schools.School", removed)
        self.assertIn("payments.Payment", removed)

    def test_an_empty_school_is_removed_without_ceremony(self):
        bare = School.all_objects.create(name="Bare", slug="bare")
        teardown.remove(bare)
        self.assertFalse(School.all_objects.filter(pk=bare.pk).exists())

    def test_a_teardown_that_cannot_finish_changes_nothing(self):
        """All or nothing. A school half closed -- students alive with no school
        to belong to -- is worse than either outcome."""
        before = Payment.all_objects.count()
        with self.settings():
            original = teardown.MAX_PASSES
            teardown.MAX_PASSES = 0
            try:
                with self.assertRaises(teardown.CouldNotRemove):
                    teardown.remove(self.alpha)
            finally:
                teardown.MAX_PASSES = original

        self.assertTrue(School.all_objects.filter(pk=self.alpha.pk).exists())
        self.assertEqual(Payment.all_objects.count(), before)
        self.assertTrue(
            Student.all_objects.filter(pk=self.alpha_bits["student"].pk).exists()
        )


class SummariseTests(SchoolTeardownTestCase):
    def test_it_counts_the_rows_that_would_go(self):
        labels = dict(teardown.summarise(self.alpha))
        self.assertEqual(labels.get("Payment"), 1)
        self.assertEqual(labels.get("Student"), 1)
        self.assertEqual(labels.get("Class"), 1)

    def test_it_counts_nothing_from_another_school(self):
        Student.all_objects.create(
            school_class=self.beta_bits["class"], admission_number="Y/002",
            first_name="Second", last_name="School", sex="male",
            parent_name="A Parent", parent_phone="08034129877",
        )
        self.assertEqual(dict(teardown.summarise(self.alpha)).get("Student"), 1)

    def test_an_empty_school_summarises_to_only_the_record_of_its_creation(self):
        """Closing a school takes its audit log too, so the page has to say so."""
        bare = School.all_objects.create(name="Bare", slug="bare")
        self.assertEqual(teardown.summarise(bare), [("Audit event", 1)])

    def test_the_biggest_count_comes_first(self):
        for index in range(4):
            Student.all_objects.create(
                school_class=self.alpha_bits["class"],
                admission_number=f"X/10{index}",
                first_name="Extra", last_name=str(index), sex="male",
                parent_name="A Parent", parent_phone="08034129878",
            )
        rows = teardown.summarise(self.alpha)
        self.assertEqual(rows[0][0], "Students")
        self.assertEqual(rows[0][1], 5)


class AdminDeleteScreenTests(SchoolTeardownTestCase):
    """The screen, which is what the complaint was actually about."""

    def url(self, school):
        return reverse("admin:schools_school_delete", args=[school.pk])

    def test_the_confirmation_page_offers_a_button_instead_of_a_refusal(self):
        self.signed_in_admin()
        response = self.client.get(self.url(self.alpha))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "protected related objects")
        self.assertContains(response, "Yes, delete the school and all of it")

    def test_it_says_what_will_go_and_counts_it(self):
        self.signed_in_admin()
        response = self.client.get(self.url(self.alpha))
        self.assertContains(response, "everything belonging to it")
        self.assertContains(response, "payment records")
        self.assertContains(response, "Payment: 1")

    def test_it_offers_the_reversible_alternative(self):
        """Closing a tenant for good and suspending one are different decisions,
        and the page people reach by accident is this one."""
        self.signed_in_admin()
        response = self.client.get(self.url(self.alpha))
        self.assertContains(response, "Suspended")

    def test_confirming_removes_the_school_and_everything_with_it(self):
        self.signed_in_admin()
        response = self.client.post(self.url(self.alpha), {"post": "yes"})
        self.assertEqual(response.status_code, 302)

        self.assertFalse(School.all_objects.filter(pk=self.alpha.pk).exists())
        self.assertFalse(
            Payment.all_objects.filter(pk=self.alpha_bits["payment"].pk).exists()
        )
        self.assertFalse(
            Student.all_objects.filter(pk=self.alpha_bits["student"].pk).exists()
        )
        self.assertTrue(School.all_objects.filter(pk=self.beta.pk).exists())

    def test_the_bulk_action_takes_several_at_once(self):
        self.signed_in_admin()
        response = self.client.post(
            reverse("admin:schools_school_changelist"),
            {
                "action": "delete_selected",
                "post": "yes",
                "_selected_action": [str(self.alpha.pk), str(self.beta.pk)],
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(School.all_objects.count(), 0)
        self.assertEqual(Payment.all_objects.count(), 0)

    def test_the_bulk_confirmation_page_names_the_schools(self):
        self.signed_in_admin()
        response = self.client.post(
            reverse("admin:schools_school_changelist"),
            {
                "action": "delete_selected",
                "_selected_action": [str(self.alpha.pk), str(self.beta.pk)],
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Alpha Schools")
        self.assertContains(response, "Beta College")
        self.assertNotContains(response, "protected related objects")

    def test_a_staff_account_without_permission_on_payments_is_refused(self):
        """Going through a school must not be a way to acquire a delete right
        the account does not have."""
        staff = User.objects.create_user(
            "limited", password="pw", is_staff=True, role=Role.PLATFORM_OWNER
        )
        for codename in ("view_school", "delete_school", "change_school"):
            staff.user_permissions.add(Permission.objects.get(codename=codename))
        self.client.force_login(staff)

        response = self.client.get(self.url(self.alpha))
        self.assertEqual(response.status_code, 200)
        # Django does not escape the apostrophe in its own literal template
        # text, so the phrase is matched around it.
        self.assertContains(response, "have permission to delete")
        self.assertContains(response, "<li>payment</li>", html=False)
        self.assertNotContains(response, "Yes, delete the school and all of it")

        # And the POST is refused, not merely hidden.
        self.assertEqual(
            self.client.post(self.url(self.alpha), {"post": "yes"}).status_code, 403
        )
        self.assertTrue(School.all_objects.filter(pk=self.alpha.pk).exists())

    def test_a_school_owner_cannot_reach_the_delete_view_at_all(self):
        """The teardown is the platform's. `bootstrap_tenant` grants a school
        owner view and change on their own school, never delete, so this is a
        403 before any of the above is reached."""
        owner = self.alpha_bits["owner"]
        owner.is_staff = True
        owner.save(update_fields=["is_staff"])
        for codename in ("view_school", "change_school"):
            owner.user_permissions.add(Permission.objects.get(codename=codename))
        self.client.force_login(owner)

        self.assertEqual(self.client.get(self.url(self.alpha)).status_code, 403)
        self.assertEqual(
            self.client.post(self.url(self.alpha), {"post": "yes"}).status_code, 403
        )
        self.assertTrue(School.all_objects.filter(pk=self.alpha.pk).exists())
