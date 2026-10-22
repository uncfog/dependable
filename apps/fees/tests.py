"""Tests for the fee structure layer.

The money rules that must never regress: a total is always the sum of its live
line items, a class has one structure per term, a bursar cannot edit, and no
branch can see another's pricing.
"""

from __future__ import annotations

from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db.utils import IntegrityError
from django.test import TestCase
from django.urls import reverse

from apps.academics.models import Class, Level
from apps.core.permissions import Capability, has_capability
from apps.core.roles import Role
from apps.core.tenancy import scope_to
from apps.schools.models import Branch, School

from .forms import FeeStructureForm
from .models import FeeComponent, FeeStructure, Term, TermSequence
from .pricing import TERM

User = get_user_model()


def component_post(rows, initial=0, prefix="components"):
    """Build formset POST data for ``rows`` of (name, amount[, pk, delete])."""
    data = {
        f"{prefix}-TOTAL_FORMS": str(len(rows)),
        f"{prefix}-INITIAL_FORMS": str(initial),
        f"{prefix}-MIN_NUM_FORMS": "0",
        f"{prefix}-MAX_NUM_FORMS": "1000",
    }
    for index, row in enumerate(rows):
        name, amount = row[0], row[1]
        data[f"{prefix}-{index}-name"] = name
        data[f"{prefix}-{index}-amount"] = str(amount)
        if len(row) > 2 and row[2] is not None:
            data[f"{prefix}-{index}-id"] = str(row[2])
        if len(row) > 3 and row[3]:
            data[f"{prefix}-{index}-DELETE"] = "on"
    return data


class FeesTestCase(TestCase):
    """One school with two branches, plus a second school."""

    @classmethod
    def setUpTestData(cls):
        cls.alpha = School.all_objects.create(name="Alpha Schools")
        cls.north = Branch.all_objects.create(school=cls.alpha, name="North")
        cls.south = Branch.all_objects.create(school=cls.alpha, name="South")
        cls.beta = School.all_objects.create(name="Beta College")
        cls.beta_main = Branch.all_objects.create(school=cls.beta, name="Beta Main")

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
        cls.north_p2 = Class.all_objects.create(
            branch=cls.north, name="Primary 2", level=Level.PRIMARY, year_in_level=2
        )
        cls.north_kg1 = Class.all_objects.create(
            branch=cls.north, name="KG 1", level=Level.NURSERY, year_in_level=1
        )
        cls.south_p1 = Class.all_objects.create(
            branch=cls.south, name="Primary 1", level=Level.PRIMARY, year_in_level=1
        )

        cls.north_term = Term.all_objects.create(
            branch=cls.north, name="First Term 2025/2026",
            academic_year="2025/2026", sequence=TermSequence.FIRST, is_current=True,
        )
        cls.south_term = Term.all_objects.create(
            branch=cls.south, name="First Term 2025/2026",
            academic_year="2025/2026", sequence=TermSequence.FIRST, is_current=True,
        )

        cls.north_p1_fees = FeeStructure.all_objects.create(
            school_class=cls.north_p1, term=cls.north_term
        )
        for position, (name, amount) in enumerate(
            [("School Fees", 26000), ("Textbooks", 50000), ("Development", 2000)]
        ):
            FeeComponent.all_objects.create(
                fee_structure=cls.north_p1_fees, name=name,
                amount=Decimal(amount), position=position,
            )

        cls.south_p1_fees = FeeStructure.all_objects.create(
            school_class=cls.south_p1, term=cls.south_term
        )
        FeeComponent.all_objects.create(
            fee_structure=cls.south_p1_fees, name="School Fees", amount=Decimal(99000)
        )

    def as_user(self, user):
        return scope_to(
            school_id=user.school_id, branch_id=user.branch_id, role=user.role
        )


class TotalTests(FeesTestCase):
    def test_total_is_the_sum_of_the_line_items(self):
        self.assertEqual(self.north_p1_fees.total, Decimal("78000"))

    def test_total_follows_an_edited_line_item(self):
        """The whole reason the total is not stored."""
        component = self.north_p1_fees.components.get(name="Textbooks")
        component.amount = Decimal("55000")
        component.save()
        self.north_p1_fees.refresh_from_db()
        self.assertEqual(self.north_p1_fees.total, Decimal("83000"))

    def test_total_follows_a_removed_line_item(self):
        self.north_p1_fees.components.get(name="Development").delete()
        self.assertEqual(self.north_p1_fees.total, Decimal("76000"))

    def test_structure_with_no_components_totals_zero(self):
        empty = FeeStructure.all_objects.create(
            school_class=self.north_kg1, term=self.north_term
        )
        self.assertEqual(empty.total, Decimal("0"))

    def test_total_is_not_a_database_column(self):
        """A stored total would be a second source of truth."""
        columns = {f.name for f in FeeStructure._meta.get_fields()}
        self.assertNotIn("total", columns)


class ModelConstraintTests(FeesTestCase):
    def test_a_class_gets_one_structure_per_term(self):
        with self.assertRaises(IntegrityError):
            FeeStructure.all_objects.create(
                school_class=self.north_p1, term=self.north_term
            )

    def test_the_same_class_may_be_priced_in_another_term(self):
        second = Term.all_objects.create(
            branch=self.north, name="Second Term 2025/2026",
            academic_year="2025/2026", sequence=TermSequence.SECOND,
        )
        structure = FeeStructure.all_objects.create(
            school_class=self.north_p1, term=second
        )
        self.assertIsNotNone(structure.pk)

    def test_setting_a_term_current_stands_the_previous_one_down(self):
        second = Term.all_objects.create(
            branch=self.north, name="Second Term 2025/2026",
            academic_year="2025/2026", sequence=TermSequence.SECOND, is_current=True,
        )
        self.north_term.refresh_from_db()
        self.assertFalse(self.north_term.is_current)
        self.assertTrue(second.is_current)

    def test_each_branch_keeps_its_own_current_term(self):
        self.assertTrue(Term.all_objects.get(pk=self.north_term.pk).is_current)
        self.assertTrue(Term.all_objects.get(pk=self.south_term.pk).is_current)

    def test_line_item_names_are_unique_within_a_structure(self):
        with self.assertRaises(IntegrityError):
            FeeComponent.all_objects.create(
                fee_structure=self.north_p1_fees, name="Textbooks", amount=Decimal(1)
            )

    def test_structure_derives_branch_and_school_from_the_class(self):
        structure = FeeStructure.all_objects.create(
            school_class=self.north_kg1, term=self.north_term
        )
        self.assertEqual(structure.branch_id, self.north.pk)
        self.assertEqual(structure.school_id, self.alpha.pk)

    def test_component_derives_branch_and_school_from_the_structure(self):
        component = FeeComponent.all_objects.create(
            fee_structure=self.north_p1_fees, name="Uniform", amount=Decimal(24000)
        )
        self.assertEqual(component.branch_id, self.north.pk)
        self.assertEqual(component.school_id, self.alpha.pk)

    def test_a_term_from_another_branch_is_rejected(self):
        structure = FeeStructure(school_class=self.north_kg1, term=self.south_term)
        with self.assertRaises(ValidationError):
            structure.full_clean()

    def test_structures_order_by_admission_ladder(self):
        FeeStructure.all_objects.create(
            school_class=self.north_p2, term=self.north_term
        )
        FeeStructure.all_objects.create(
            school_class=self.north_kg1, term=self.north_term
        )
        with self.as_user(self.north_principal):
            names = [s.school_class.name for s in FeeStructure.objects.all()]
        self.assertEqual(names, ["KG 1", "Primary 1", "Primary 2"])


class ScopingTests(FeesTestCase):
    def test_principal_sees_only_their_branchs_structures(self):
        with self.as_user(self.north_principal):
            self.assertEqual(list(FeeStructure.objects.all()), [self.north_p1_fees])
            self.assertEqual(list(Term.objects.all()), [self.north_term])

    def test_school_owner_sees_both_branches(self):
        with self.as_user(self.alpha_owner):
            self.assertCountEqual(
                FeeStructure.objects.all(), [self.north_p1_fees, self.south_p1_fees]
            )

    def test_components_are_scoped_too(self):
        with self.as_user(self.north_principal):
            names = set(FeeComponent.objects.values_list("name", flat=True))
        self.assertEqual(names, {"School Fees", "Textbooks", "Development"})
        with self.as_user(self.north_principal):
            self.assertNotIn(
                self.south_p1_fees.pk,
                FeeComponent.objects.values_list("fee_structure_id", flat=True),
            )

    def test_a_principal_cannot_edit_any_structure(self):
        """Refused on the capability before the row is looked up, so their own
        campus's structure and another's get the same 403."""
        self.client.force_login(self.north_principal)
        for structure in (self.north_p1_fees, self.south_p1_fees):
            response = self.client.get(
                reverse("fees:structure_update", args=[structure.pk])
            )
            self.assertEqual(response.status_code, 403)


class CapabilityTests(TestCase):
    def test_only_owners_manage_fees(self):
        for role in (Role.PLATFORM_OWNER, Role.SCHOOL_OWNER):
            self.assertTrue(
                has_capability(User(role=role), Capability.MANAGE_FEES), role
            )

    def test_principal_views_but_does_not_manage(self):
        principal = User(role=Role.PRINCIPAL)
        self.assertTrue(has_capability(principal, Capability.VIEW_FEES))
        self.assertFalse(has_capability(principal, Capability.MANAGE_FEES))

    def test_bursar_views_but_does_not_manage(self):
        bursar = User(role=Role.BURSAR)
        self.assertTrue(has_capability(bursar, Capability.VIEW_FEES))
        self.assertFalse(has_capability(bursar, Capability.MANAGE_FEES))


class ViewPermissionTests(FeesTestCase):
    def test_bursar_can_read_the_list(self):
        self.client.force_login(self.north_bursar)
        response = self.client.get(reverse("fees:structure_list"))
        self.assertEqual(response.status_code, 200)

    def test_bursar_cannot_reach_create_edit_or_delete(self):
        self.client.force_login(self.north_bursar)
        cases = [
            reverse("fees:structure_create"),
            reverse("fees:structure_update", args=[self.north_p1_fees.pk]),
            reverse("fees:structure_delete", args=[self.north_p1_fees.pk]),
        ]
        for url in cases:
            self.assertEqual(self.client.get(url).status_code, 403, url)

    def test_bursar_cannot_post_a_structure(self):
        self.client.force_login(self.north_bursar)
        before = FeeStructure.all_objects.count()
        data = {"school_class": self.north_kg1.pk, "term": self.north_term.pk}
        data.update(component_post([("School Fees", 1000)]))
        response = self.client.post(reverse("fees:structure_create"), data)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(FeeStructure.all_objects.count(), before)

    def test_bursar_list_hides_edit_controls(self):
        self.client.force_login(self.north_bursar)
        body = self.client.get(reverse("fees:structure_list")).content.decode()
        self.assertNotIn("New structure", body)

    def test_owner_sees_edit_controls(self):
        self.client.force_login(self.alpha_owner)
        body = self.client.get(reverse("fees:structure_list")).content.decode()
        self.assertIn("New structure", body)

    def test_principal_reads_the_list_without_edit_controls(self):
        self.client.force_login(self.north_principal)
        response = self.client.get(reverse("fees:structure_list"))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("New structure", response.content.decode())

    def test_anonymous_is_redirected_to_login(self):
        response = self.client.get(reverse("fees:structure_list"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.headers["Location"])


class ListViewTests(FeesTestCase):
    def test_defaults_to_the_current_term(self):
        self.client.force_login(self.north_principal)
        response = self.client.get(reverse("fees:structure_list"))
        self.assertEqual(response.context["selected_term"], self.north_term)

    def test_groups_by_level_with_subtotals(self):
        FeeStructure.all_objects.create(
            school_class=self.north_kg1, term=self.north_term
        )
        self.client.force_login(self.north_principal)
        response = self.client.get(reverse("fees:structure_list"))
        groups = {g["label"]: g for g in response.context["level_groups"]}
        self.assertEqual(set(groups), {"Nursery", "Primary"})
        self.assertEqual(groups["Primary"]["subtotal"], Decimal("78000"))
        self.assertEqual(response.context["grand_total"], Decimal("78000"))

    def test_lists_classes_that_still_need_pricing(self):
        self.client.force_login(self.north_principal)
        response = self.client.get(reverse("fees:structure_list"))
        pending = {c.name for c in response.context["classes_without_fees"]}
        self.assertEqual(pending, {"Primary 2", "KG 1"})

    def test_another_branchs_term_shows_nothing_to_a_principal(self):
        """The term is out of scope, so it cannot be selected at all."""
        self.client.force_login(self.north_principal)
        response = self.client.get(
            reverse("fees:structure_list"), {"term": self.south_term.pk}
        )
        # Falls back to the caller's own current term rather than leaking.
        self.assertEqual(response.context["selected_term"], self.north_term)


class EditingTests(FeesTestCase):
    def test_owner_creates_a_structure_with_line_items(self):
        self.client.force_login(self.alpha_owner)
        data = {"school_class": self.north_kg1.pk, "term": self.north_term.pk}
        data.update(
            component_post([("School Fees", 24000), ("Textbooks", 32000),
                            ("Development", 2000)])
        )
        response = self.client.post(reverse("fees:structure_create"), data)
        self.assertEqual(response.status_code, 302)

        structure = FeeStructure.all_objects.get(school_class=self.north_kg1)
        self.assertEqual(structure.total, Decimal("58000"))
        self.assertEqual(
            [c.name for c in structure.components.all()],
            ["School Fees", "Textbooks", "Development"],
        )
        self.assertEqual([c.position for c in structure.components.all()], [0, 1, 2])

    def test_a_structure_needs_at_least_one_line_item(self):
        self.client.force_login(self.alpha_owner)
        data = {"school_class": self.north_kg1.pk, "term": self.north_term.pk}
        data.update(component_post([]))
        response = self.client.post(reverse("fees:structure_create"), data)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(FeeStructure.all_objects.filter(school_class=self.north_kg1))

    def test_duplicate_line_item_names_are_rejected(self):
        self.client.force_login(self.alpha_owner)
        data = {"school_class": self.north_kg1.pk, "term": self.north_term.pk}
        data.update(component_post([("Textbooks", 1000), ("textbooks", 2000)]))
        response = self.client.post(reverse("fees:structure_create"), data)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(FeeStructure.all_objects.filter(school_class=self.north_kg1))

    def test_nothing_is_written_when_the_line_items_fail(self):
        """Form and formset are validated together, inside one transaction."""
        self.client.force_login(self.alpha_owner)
        before = FeeStructure.all_objects.count()
        data = {"school_class": self.north_kg1.pk, "term": self.north_term.pk}
        data.update(component_post([("Textbooks", "not-a-number")]))
        self.client.post(reverse("fees:structure_create"), data)
        self.assertEqual(FeeStructure.all_objects.count(), before)

    def test_editing_removes_and_adds_line_items(self):
        self.client.force_login(self.alpha_owner)
        existing = list(self.north_p1_fees.components.all())
        rows = [
            (existing[0].name, 30000, existing[0].pk, False),   # repriced
            (existing[1].name, existing[1].amount, existing[1].pk, False),
            (existing[2].name, existing[2].amount, existing[2].pk, True),  # removed
            ("Uniform", 24000, None, False),                    # added
        ]
        data = {"school_class": self.north_p1.pk, "term": self.north_term.pk}
        data.update(component_post(rows, initial=3))
        response = self.client.post(
            reverse("fees:structure_update", args=[self.north_p1_fees.pk]), data
        )
        self.assertEqual(response.status_code, 302)

        self.north_p1_fees.refresh_from_db()
        names = [c.name for c in self.north_p1_fees.components.all()]
        self.assertEqual(names, ["School Fees", "Textbooks", "Uniform"])
        self.assertEqual(self.north_p1_fees.total, Decimal("104000"))

    def test_a_class_cannot_be_priced_twice_in_one_term(self):
        self.client.force_login(self.alpha_owner)
        data = {"school_class": self.north_p1.pk, "term": self.north_term.pk}
        data.update(component_post([("School Fees", 1000)]))
        response = self.client.post(reverse("fees:structure_create"), data)
        self.assertEqual(response.status_code, 200)
        self.assertIn("school_class", response.context["form"].errors)

    def test_deleting_a_structure_takes_its_components(self):
        self.client.force_login(self.alpha_owner)
        response = self.client.post(
            reverse("fees:structure_delete", args=[self.north_p1_fees.pk])
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(FeeComponent.all_objects.filter(
            fee_structure_id=self.north_p1_fees.pk).count(), 0)


class FormScopingTests(FeesTestCase):
    def test_class_and_term_choices_are_limited_to_the_branch(self):
        with self.as_user(self.north_principal):
            form = FeeStructureForm()
            self.assertNotIn(self.south_p1, form.fields["school_class"].queryset)
            self.assertNotIn(self.south_term, form.fields["term"].queryset)

    def test_mismatched_class_and_term_are_rejected(self):
        """A school owner sees both branches and must not cross them."""
        with self.as_user(self.alpha_owner):
            form = FeeStructureForm(
                data={"school_class": self.north_kg1.pk, "term": self.south_term.pk}
            )
            self.assertFalse(form.is_valid())
            self.assertIn("term", form.errors)


class SeedCommandTests(TestCase):
    def _seed(self):
        out = StringIO()
        call_command("seed_academics", "--create-school", stdout=StringIO())
        call_command("seed_fees", stdout=out)
        return out.getvalue()

    def test_every_class_is_priced_and_totals_match_the_schedule(self):
        self._seed()
        self.assertEqual(FeeStructure.all_objects.count(), 19)

        expected = {
            "Pre-KG": 56000, "KG 1": 58000, "KG 2": 61000, "KG 3": 71000,
            # Recurring items only: the entry years no longer carry uniform,
            # and senior development no longer carries the practical charge.
            "Primary 1": 78000, "Primary 2": 78000, "Primary 3": 78000,
            "Primary 4": 78000, "Primary 5": 78000, "Primary 6": 78000,
            "JSS 1": 88000, "JSS 2": 88000, "JSS 3": 88000,
            "SSS 1 Arts": 117000, "SSS 1 Science": 117000,
            "SSS 2 Arts": 112000, "SSS 2 Science": 117000,
            "SSS 3 Arts": 112000, "SSS 3 Science": 117000,
        }
        actual = {
            s.school_class.display_name: int(s.total)
            for s in FeeStructure.all_objects.select_related(
                "school_class"
            ).prefetch_related("components")
        }
        self.assertEqual(actual, expected)

    def test_shared_pricing_still_gives_each_class_its_own_row(self):
        """Primary 2-6 charge the same but must be independently editable."""
        self._seed()
        structures = FeeStructure.all_objects.filter(
            school_class__name__startswith="Primary"
        ).exclude(school_class__name="Primary 1")
        self.assertEqual(structures.count(), 5)

        component_ids = set()
        for structure in structures:
            component_ids |= {c.pk for c in structure.components.all()}
        # 5 classes x 3 components, none shared between them.
        self.assertEqual(len(component_ids), 15)

        # Repricing one leaves the others alone.
        target = structures.get(school_class__name="Primary 3")
        target.components.filter(name="Textbooks").update(amount=Decimal("60000"))
        others = structures.exclude(pk=target.pk)
        self.assertTrue(all(s.total == Decimal("78000") for s in others))
        self.assertEqual(
            FeeStructure.all_objects.get(pk=target.pk).total, Decimal("88000")
        )

    def test_primary_one_discrepancy_is_reported_not_papered_over(self):
        output = self._seed()
        self.assertIn("Primary 1", output)
        self.assertIn("78,000", output)
        self.assertIn("80,000", output)

        structure = FeeStructure.all_objects.get(school_class__name="Primary 1")
        self.assertEqual(structure.total, Decimal("78000"))
        # No invented balancing line.
        self.assertEqual(structure.components.count(), 3)
        self.assertEqual(
            sorted(c.name for c in structure.components.all()),
            ["Development", "School Fees", "Textbooks"],
        )

    def test_line_items_keep_the_order_they_were_specified_in(self):
        self._seed()
        structure = FeeStructure.all_objects.get(school_class__name="Primary 1")
        self.assertEqual(
            [c.name for c in structure.components.all()],
            ["School Fees", "Development", "Textbooks"],
        )

    def test_seeding_twice_does_not_duplicate_anything(self):
        self._seed()
        call_command("seed_fees", stdout=StringIO())
        self.assertEqual(FeeStructure.all_objects.count(), 19)
        self.assertEqual(FeeComponent.all_objects.count(), 57)
        self.assertEqual(Term.all_objects.count(), 1)

    def test_the_seeded_term_is_current(self):
        self._seed()
        term = Term.all_objects.get()
        self.assertTrue(term.is_current)
        # Read from the pricing file rather than restated here: the year moves
        # every session, and a copy in the test would just go stale.
        self.assertEqual(term.academic_year, TERM["academic_year"])
        self.assertEqual(term.name, TERM["name"])
        self.assertEqual(term.sequence, TermSequence.FIRST)

    def test_refuses_to_run_before_classes_exist(self):
        School.all_objects.create(name="Fulfilled Academy")
        with self.assertRaises(CommandError):
            call_command("seed_fees", stdout=StringIO())


class NavigationTests(TestCase):
    def test_every_role_gets_the_fee_structures_link(self):
        from apps.core.navigation import nav_for

        for role in Role.values:
            finance = [
                item
                for section in nav_for(role, "/")
                if section.label == "Finance"
                for item in section.items
            ]
            labels = {item.label for item in finance}
            self.assertIn("Fee Structures", labels, role)

    def test_the_fee_structures_link_resolves(self):
        from apps.core.navigation import nav_for

        sections = {s.label: s.items for s in nav_for(Role.PRINCIPAL, "/")}
        item = next(i for i in sections["Finance"] if i.label == "Fee Structures")
        self.assertTrue(item.available)
