"""Fulfilled Academy's First Term 2026/2027 fee schedule.

The single source for seeding, the same way ``academics/curriculum.py`` is for
classes and subjects: edit the tuples below and re-run
``manage.py seed_fees --replace``.

Two things to know about how this data is expressed:

* A spec listing several class names creates *one structure per class*, not one
  shared row. "Primary 2-6" charge the same today, but each class owns its own
  components so any one of them can be repriced without touching the others.
* ``client_total`` is a cross-check, never stored. Where the school's written
  total disagrees with the line items, the seeder charges the line items and
  prints a warning. Inventing a balancing component to make the arithmetic work
  would bury a real question about the source data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(frozen=True)
class FeeLine:
    name: str
    amount: Decimal


def line(name: str, amount: int | str) -> FeeLine:
    # Decimal from a string/int, never a float -- money must not carry binary
    # rounding error.
    return FeeLine(name=name, amount=Decimal(str(amount)))


@dataclass(frozen=True)
class StructureSpec:
    """One fee set, applied to each of ``class_names`` separately."""

    class_names: tuple[str, ...]
    lines: tuple[FeeLine, ...]
    #: Empty means every arm of the named class; otherwise only these arms.
    streams: tuple[str, ...] = ()
    #: The school's own written total, for cross-checking only.
    client_total: Decimal | None = None
    #: Why the two disagree, shown by the seeder when they do.
    total_note: str = ""

    @property
    def computed_total(self) -> Decimal:
        return sum((line.amount for line in self.lines), Decimal("0"))

    def matches(self, klass) -> bool:
        if klass.name not in self.class_names:
            return False
        return not self.streams or klass.stream in self.streams


TERM = {
    "name": "First Term 2026/2027",
    "academic_year": "2026/2027",
    "sequence": 1,
    "is_current": True,
    # Left unset on purpose. "Overdue" is a property of the term, not of a
    # student, so the moment a due date is in the past *every* unpaid and part
    # paid balance at the branch turns overdue at once -- which is correct, and
    # is also why seeding one would hide the unpaid and part-paid states behind
    # a wall of red. Set it on the term (admin, or Fee structures > Terms) when
    # the school has agreed a deadline, and the overdue pill appears.
    "due_date": None,
}


FEE_STRUCTURES: tuple[StructureSpec, ...] = (
    # Recurring items only: school fees, textbooks and development. Everything
    # a child pays once -- the admission fee, uniform, sportswear or practical,
    # track suit, exam/dossier -- is on the intake sheet in
    # apps/admissions/admission_pricing.py and nowhere else, so a new child's
    # bill is intake one-offs plus this term's structure with no line counted
    # twice. The entry years used to carry a Uniform line here as well, at a
    # different price from the intake sheet's; uniform now lives there only.

    # --- Nursery ----------------------------------------------------------
    StructureSpec(
        ("Pre-KG",),
        (line("School Fees", 24_000), line("Textbooks", 30_000),
         line("Development", 2_000)),
    ),
    StructureSpec(
        ("KG 1",),
        (line("School Fees", 24_000), line("Textbooks", 32_000),
         line("Development", 2_000)),
    ),
    StructureSpec(
        ("KG 2",),
        (line("School Fees", 24_000), line("Textbooks", 35_000),
         line("Development", 2_000)),
    ),
    StructureSpec(
        ("KG 3",),
        (line("School Fees", 24_000), line("Textbooks", 45_000),
         line("Development", 2_000)),
    ),

    # --- Primary ----------------------------------------------------------
    # Primary 1 is its own spec only to keep the cross-check: the school's
    # written total was 104,000 including a 24,000 uniform, so 80,000 without
    # it, against line items that sum to 78,000.
    StructureSpec(
        ("Primary 1",),
        (line("School Fees", 26_000), line("Development", 2_000),
         line("Textbooks", 50_000)),
        client_total=Decimal("80000"),
        total_note=(
            "School's written total was 104,000 including a 24,000 uniform "
            "(80,000 without it, now that uniform is an intake item), but these "
            "line items sum to 78,000 -- a 2,000 gap being confirmed with the "
            "client. Components are seeded as supplied; no balancing line has "
            "been invented."
        ),
    ),
    StructureSpec(
        ("Primary 2", "Primary 3", "Primary 4", "Primary 5", "Primary 6"),
        (line("School Fees", 26_000), line("Textbooks", 50_000),
         line("Development", 2_000)),
    ),

    # --- Junior secondary --------------------------------------------------
    StructureSpec(
        ("JSS 1", "JSS 2", "JSS 3"),
        (line("School Fees", 31_000), line("Textbooks", 55_000),
         line("Development", 2_000)),
    ),

    # --- Senior secondary ---------------------------------------------------
    # The school quoted "Practical/Devy" at 5,000: development (2,000) and the
    # practical charge (3,000) together. Practical is an intake one-off on the
    # admission sheet, so only the development part recurs here. SSS 1 is
    # priced the same for both arms; the book list splits at SSS 2.
    StructureSpec(
        ("SSS 1",),
        (line("School Fees", 40_000), line("Development", 2_000),
         line("Textbooks", 75_000)),
    ),
    StructureSpec(
        ("SSS 2", "SSS 3"),
        (line("School Fees", 40_000), line("Textbooks", 75_000),
         line("Development", 2_000)),
        streams=("Science",),
    ),
    StructureSpec(
        ("SSS 2", "SSS 3"),
        (line("School Fees", 40_000), line("Textbooks", 70_000),
         line("Development", 2_000)),
        streams=("Arts",),
    ),
)


def structures_for(klass) -> list[StructureSpec]:
    """Every spec that should produce a structure for ``klass``."""
    return [spec for spec in FEE_STRUCTURES if spec.matches(klass)]
