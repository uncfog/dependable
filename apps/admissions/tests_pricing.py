"""The intake sheet and the term structure never price the same item.

A new child's bill is the intake one-offs plus the current term's structure.
That sum is only right if no item sits on both lists -- which is exactly what
went wrong before: tuition, books and development were on both, so a family
that paid everything at the door still showed the term as owed.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from django.test import SimpleTestCase

from apps.fees.pricing import FEE_STRUCTURES, structures_for

from .admission_pricing import ADMISSION_FEES, specs_for

#: The school's own spellings, reduced to the item they name. "Practical/Devy"
#: is two items in one line, which is why the answer is a set.
_SYNONYMS = {
    "tuition": {"tuition"},
    "school fees": {"tuition"},
    "textbooks": {"books"},
    "compulsory books": {"books"},
    "development": {"development"},
    "development levy": {"development"},
    "practical/devy": {"practical", "development"},
}


def items(name: str) -> set[str]:
    key = name.lower().replace("(2)", "").strip()
    return _SYNONYMS.get(key, {key})


def every_class():
    """Each class name on either sheet, in each arm a sheet might split on."""
    names = {n for spec in ADMISSION_FEES for n in spec.class_names}
    names |= {n for spec in FEE_STRUCTURES for n in spec.class_names}
    for name in sorted(names):
        for stream in ("", "Arts", "Science"):
            yield SimpleNamespace(name=name, stream=stream)


class NoDoubleChargeTests(SimpleTestCase):
    def test_no_item_is_on_both_the_intake_sheet_and_the_term_structure(self):
        for klass in every_class():
            intake = {i for s in specs_for(klass) for line in s.lines
                      for i in items(line.name)}
            term = {i for s in structures_for(klass) for line in s.lines
                    for i in items(line.name)}
            with self.subTest(klass=f"{klass.name} {klass.stream}".strip()):
                self.assertFalse(intake & term, intake & term)

    def test_every_class_has_exactly_one_of_each(self):
        for klass in every_class():
            if klass.name.startswith("SSS") and not klass.stream:
                continue  # SSS 2/3 term fees are per arm; a bare SSS 2 is not a class
            with self.subTest(klass=f"{klass.name} {klass.stream}".strip()):
                self.assertEqual(len(specs_for(klass)), 1)
                self.assertEqual(len(structures_for(klass)), 1)

    def test_uniform_lives_on_the_intake_sheet_only(self):
        for spec in FEE_STRUCTURES:
            with self.subTest(classes=spec.class_names):
                self.assertFalse(
                    any("uniform" in items(line.name) for line in spec.lines)
                )
        for spec in ADMISSION_FEES:
            with self.subTest(classes=spec.class_names):
                self.assertTrue(
                    any("uniform" in items(line.name) for line in spec.lines)
                )

    def test_the_term_structure_carries_the_recurring_items(self):
        for spec in FEE_STRUCTURES:
            names = {i for line in spec.lines for i in items(line.name)}
            with self.subTest(classes=spec.class_names, streams=spec.streams):
                self.assertEqual(names, {"tuition", "books", "development"})

    def test_a_new_kg2_childs_bill_is_one_off_plus_term(self):
        """The pilot's own case: 56,000 at the door plus 61,000 for the term."""
        kg2 = SimpleNamespace(name="KG 2", stream="")
        intake = specs_for(kg2)[0].computed_total
        term = structures_for(kg2)[0].computed_total
        self.assertEqual(intake, Decimal("56000"))
        self.assertEqual(term, Decimal("61000"))
        self.assertEqual(intake + term, Decimal("117000"))
