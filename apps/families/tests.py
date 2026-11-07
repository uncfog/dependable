"""Grouping children into a family, without touching any child's own record."""

from __future__ import annotations

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.core.permissions import Capability, capabilities_for
from apps.core.roles import Role
from apps.core.tenancy import scope_to
from apps.payments import allocation
from apps.payments.models import Payment
from apps.payments.tests_allocation import build_other_school, build_school
from apps.schools.models import School, SchoolModule
from apps.students import fees
from apps.students.models import Student

from .balances import summary_for
from .models import Family, FamilyMembership
from .services import FamilyLinkError, link, unlink

User = get_user_model()
D = Decimal


class FamilyTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        for key, value in build_school().items():
            setattr(cls, key, value)
        SchoolModule.all_objects.create(school=cls.school, key="families", enabled=True)


class ModelTests(FamilyTestCase):
    def test_one_active_family_per_child(self):
        other = Family.all_objects.create(school=self.school, name="Another")

        with self.assertRaises(FamilyLinkError):
            link(other, self.ada, by=self.owner)

    def test_the_database_holds_that_too(self):
        other = Family.all_objects.create(school=self.school, name="Another")

        with self.assertRaises(IntegrityError), transaction.atomic():
            FamilyMembership.all_objects.create(family=other, student=self.ada)

    def test_a_family_code_is_unique_per_school_whatever_its_case(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Family.all_objects.create(school=self.school, name="Copy", family_code="f-1")

    def test_two_schools_can_use_the_same_code(self):
        other = School.all_objects.create(name="Bravo")

        Family.all_objects.create(school=other, name="Theirs", family_code="F-1")

    def test_many_families_can_have_no_code(self):
        Family.all_objects.create(school=self.school, name="One")
        Family.all_objects.create(school=self.school, name="Two")

    def test_a_child_from_another_school_cannot_join(self):
        with self.assertRaises(FamilyLinkError):
            link(self.family, build_other_school(), by=self.owner)

    def test_leaving_keeps_the_history_and_allows_a_new_family(self):
        membership = FamilyMembership.all_objects.get(student=self.ada, ended_at__isnull=True)
        unlink(membership, by=self.owner)

        membership.refresh_from_db()
        self.assertIsNotNone(membership.ended_at)
        self.assertEqual(membership.ended_by, self.owner)
        other = Family.all_objects.create(school=self.school, name="Another")
        link(other, self.ada, by=self.owner)
        self.assertEqual(FamilyMembership.all_objects.filter(student=self.ada).count(), 2)

    def test_leaving_does_not_change_any_money(self):
        txn = allocation.receive(
            amount=D("50000"), date_paid=timezone.localdate(), method="cash",
            actor=self.bursar, branch=self.north, family=self.family,
        )
        allocation.allocate(txn, self.ada, D("50000"), actor=self.bursar)

        unlink(
            FamilyMembership.all_objects.get(student=self.ada, ended_at__isnull=True),
            by=self.owner,
        )

        self.assertEqual(Payment.all_objects.get().student, self.ada)
        self.assertEqual(
            fees.position_for(Student.all_objects.get(pk=self.ada.pk)).total_owed, D("0")
        )

    def test_linking_is_recorded(self):
        self.assertTrue(
            AuditEvent.all_objects.filter(action="familymembership.created").exists()
        )

    def test_nothing_is_merged_or_changed_on_the_child(self):
        before = Student.all_objects.filter(pk=self.ada.pk).values().get()

        other = Family.all_objects.create(school=self.school, name="Another")
        unlink(
            FamilyMembership.all_objects.get(student=self.ada, ended_at__isnull=True),
            by=self.owner,
        )
        link(other, self.ada, by=self.owner)

        self.assertEqual(Student.all_objects.filter(pk=self.ada.pk).values().get(), before)

    def test_no_family_is_made_for_children_who_just_share_a_surname(self):
        """Every child here is an Obi, and the stranger stays out."""
        self.assertFalse(
            FamilyMembership.all_objects.filter(student=self.stranger).exists()
        )


class SummaryTests(FamilyTestCase):
    def summary(self, user):
        with scope_to(school_id=user.school_id, branch_id=user.branch_id, role=user.role):
            return summary_for(self.family)

    def test_the_family_total_adds_up_each_childs_balance(self):
        Payment.all_objects.create(
            branch=self.north, student=self.ada, term=self.n_term, amount=D("20000")
        )

        summary = self.summary(self.owner)

        self.assertEqual(len(summary.children), 4)
        self.assertEqual(summary.total_owed, D("30000") + D("50000") * 3)
        self.assertFalse(summary.has_children_elsewhere)

    def test_one_childs_credit_does_not_reduce_a_siblings_debt(self):
        Payment.all_objects.create(
            branch=self.north, student=self.ada, term=self.n_term, amount=D("70000")
        )

        summary = self.summary(self.owner)

        self.assertEqual(summary.total_owed, D("150000"))
        self.assertEqual(summary.total_credit, D("20000"))

    def test_money_not_yet_shared_out_is_beside_the_total_not_inside_it(self):
        allocation.receive(
            amount=D("10000"), date_paid=timezone.localdate(), method="cash",
            actor=self.bursar, branch=self.north, family=self.family,
        )

        summary = self.summary(self.owner)

        self.assertEqual(summary.unallocated, D("10000"))
        self.assertEqual(summary.total_owed, D("200000"))

    def test_campus_staff_see_their_own_children_and_a_note_about_the_rest(self):
        summary = self.summary(self.bursar)

        self.assertEqual(
            {row.student.first_name for row in summary.children}, {"Ada", "Bola", "Chidi"}
        )
        self.assertTrue(summary.has_children_elsewhere)
        self.assertEqual(summary.total_owed, D("150000"))

    def test_a_child_who_left_is_not_counted(self):
        unlink(
            FamilyMembership.all_objects.get(student=self.ada, ended_at__isnull=True),
            by=self.owner,
        )

        self.assertEqual(self.summary(self.owner).total_owed, D("150000"))


class ScreenTests(FamilyTestCase):
    def test_campus_staff_are_told_about_children_elsewhere_without_details(self):
        self.client.force_login(self.bursar)

        response = self.client.get(reverse("families:detail", args=[self.family.pk]))

        self.assertContains(response, "children at another campus")
        self.assertNotContains(response, "Sade")

    def test_the_owner_sees_everyone(self):
        self.client.force_login(self.owner)

        self.assertContains(
            self.client.get(reverse("families:detail", args=[self.family.pk])), "Sade"
        )

    def test_the_list_finds_by_name_or_code(self):
        self.client.force_login(self.bursar)
        url = reverse("families:list")

        self.assertContains(self.client.get(url, {"q": "obis"}), "The Obis")
        self.assertContains(self.client.get(url, {"q": "F-1"}), "The Obis")
        self.assertNotContains(self.client.get(url, {"q": "nobody"}), "The Obis")

    def test_the_owner_and_principal_can_create_one(self):
        for user, name in ((self.owner, "Family A"), (self.principal, "Family B")):
            self.client.force_login(user)
            response = self.client.post(
                reverse("families:create"), {"name": name, "family_code": ""}
            )
            self.assertEqual(response.status_code, 302)
            self.assertEqual(Family.all_objects.get(name=name).school, self.school)

    def test_a_repeated_code_is_an_error_not_a_crash(self):
        self.client.force_login(self.owner)

        response = self.client.post(
            reverse("families:create"), {"name": "Copy", "family_code": "f-1"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Family.all_objects.filter(name="Copy").exists())

    def test_a_bursar_can_read_but_not_change(self):
        self.client.force_login(self.bursar)

        self.assertEqual(self.client.get(reverse("families:list")).status_code, 200)
        self.assertEqual(self.client.get(reverse("families:create")).status_code, 403)
        self.assertEqual(
            self.client.post(
                reverse("families:add_child", args=[self.family.pk]), {"student": "A9"}
            ).status_code,
            403,
        )

    def test_adding_a_child_by_their_number(self):
        self.client.force_login(self.principal)

        response = self.client.post(
            reverse("families:add_child", args=[self.family.pk]), {"student": "A9"}
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            FamilyMembership.all_objects.filter(
                family=self.family, student=self.stranger, ended_at__isnull=True
            ).exists()
        )

    def test_a_child_already_in_a_family_is_refused_with_a_reason(self):
        other = Family.all_objects.create(school=self.school, name="Another")
        self.client.force_login(self.owner)

        response = self.client.post(
            reverse("families:add_child", args=[other.pk]), {"student": "A1"}, follow=True
        )

        self.assertContains(response, "already")
        self.assertFalse(FamilyMembership.all_objects.filter(family=other).exists())

    def test_a_principal_cannot_add_a_child_from_another_campus(self):
        self.client.force_login(self.principal)
        membership = FamilyMembership.all_objects.get(student=self.ada, ended_at__isnull=True)
        unlink(membership, by=self.owner)
        FamilyMembership.all_objects.filter(student=self.sade).update(ended_at=timezone.now())

        self.client.post(
            reverse("families:add_child", args=[self.family.pk]), {"student": "S1"}
        )

        self.assertFalse(
            FamilyMembership.all_objects.filter(student=self.sade, ended_at__isnull=True).exists()
        )

    def test_removing_a_child_is_post_only_and_keeps_the_record(self):
        membership = FamilyMembership.all_objects.get(student=self.ada, ended_at__isnull=True)
        url = reverse("families:remove_child", args=[membership.pk])
        self.client.force_login(self.owner)

        self.assertEqual(self.client.get(url).status_code, 405)
        self.assertEqual(self.client.post(url).status_code, 302)
        membership.refresh_from_db()
        self.assertIsNotNone(membership.ended_at)

    def test_another_schools_family_is_a_404(self):
        other = School.all_objects.create(name="Bravo")
        SchoolModule.all_objects.create(school=other, key="families", enabled=True)
        stranger = User.objects.create_user(
            "stranger", password="pw", role=Role.SCHOOL_OWNER, school=other
        )
        self.client.force_login(stranger)

        self.assertEqual(
            self.client.get(reverse("families:detail", args=[self.family.pk])).status_code, 404
        )

    def test_the_childs_page_links_to_their_family(self):
        self.client.force_login(self.bursar)

        response = self.client.get(reverse("students:student_detail", args=[self.ada.pk]))

        self.assertContains(response, reverse("families:detail", args=[self.family.pk]))


class ModuleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        for key, value in build_school().items():
            setattr(cls, key, value)

    def test_it_is_off_until_a_school_is_given_it(self):
        self.client.force_login(self.owner)

        self.assertEqual(self.client.get(reverse("families:list")).status_code, 403)

    def test_the_sidebar_and_childs_page_do_not_offer_it_while_off(self):
        self.client.force_login(self.owner)

        response = self.client.get(reverse("students:student_detail", args=[self.ada.pk]))

        self.assertNotContains(response, reverse("families:list"))

    def test_switching_on_opens_it(self):
        SchoolModule.all_objects.create(school=self.school, key="families", enabled=True)
        self.client.force_login(self.owner)

        self.assertEqual(self.client.get(reverse("families:list")).status_code, 200)


class CapabilityTests(TestCase):
    def test_who_holds_what(self):
        for role, view, manage in (
            (Role.PLATFORM_OWNER, True, True),
            (Role.SCHOOL_OWNER, True, True),
            (Role.PRINCIPAL, True, True),
            (Role.BURSAR, True, False),
        ):
            held = capabilities_for(role)
            self.assertEqual(Capability.VIEW_FAMILIES in held, view, role)
            self.assertEqual(Capability.MANAGE_FAMILIES in held, manage, role)
