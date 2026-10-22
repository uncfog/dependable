"""Fulfilled Academy's admission requirement fees -- what it costs to come in.

The single source for seeding intake fees, exactly as
``apps/fees/pricing.py`` is for termly fees and ``academics/curriculum.py`` is
for classes: edit the tuples below and re-run
``manage.py seed_admission_fees --replace``.

**This is not the termly fee schedule and must never be merged into it.** A
``FeeStructure`` is what a class owes *per term* and is repriced every term;
this is charged once, at the door, and carries items -- uniform, tracksuit, the
book list -- that a returning child does not pay again. Two files, two models,
two seeders, and neither command touches the other's rows.

Three things to know about how the data is expressed:

* A spec listing several class names produces *one schedule per class*, not one
  shared row -- the same rule the termly pricing file follows, so any one class
  can be repriced without dragging the others with it.
* ``client_total`` is a cross-check, never stored. Where the school's written
  total disagrees with the line items, the seeder charges the line items and
  prints a warning. Inventing a balancing line to make the arithmetic work
  would bury a real question about the source data. That is how Primary 1's
  2,000 gap is handled in the termly file, and it is how the Pre-KG-KG3 gap
  below is handled here.
* **Only one-off items live here.** The admission fee, uniform, sportswear (or
  practical, at senior level), track suit and exam/dossier. Tuition, the book
  list and development recur every term, so they are on the term fee
  structure in ``apps/fees/pricing.py`` and nowhere else. They used to be on
  both, which billed a new child twice for the same term: once at the door and
  again on the roster. A new child's bill is now this sheet plus the current
  term's structure, and no line appears on both.

--------------------------------------------------------------------------
THE BANDS
--------------------------------------------------------------------------
The school prices intake by band -- nursery, primary, junior, senior -- and
each band is one sheet. Senior charges "Practical" where the younger bands
charge "Sportswear", and the junior and senior sheets quote "Uniform (2)" --
two sets. The names here are the school's own, not tidied.

``client_total`` is the school's written total *less the recurring lines that
moved to the term structure* (tuition and development; books were never in
it). That keeps the cross-check meaningful, and it is why the nursery band
still warns: its written 81,000 less 26,000 is 55,000, against one-off lines
that sum to 56,000. The same 1,000 gap the client flagged, still unconfirmed,
still not balanced by an invented line.

Any line still marked PENDING is refused by the seeder rather than seeded as a
zero -- a real-looking 0 in front of a parent is worse than a command that
names the figure it is waiting for. Nothing is PENDING today.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

#: The intake year these sheets price. Matches the termly file's academic year,
#: but is stored on its own field -- an intake fee and a term fee for the same
#: year are still different things.
ACADEMIC_YEAR = "2026/2027"

#: Kind values, mirrored from ``models.AdmissionFeeKind``. Repeated as plain
#: strings so this file stays importable without Django configured, which is
#: what lets a test read the sheet without a database.
ADMISSION = "admission"
INTAKE = "intake"
BOOKS = "books"

#: An amount the school has not supplied yet. See the module docstring: the
#: seeder names these rather than guessing at them.
PENDING = None


@dataclass(frozen=True)
class AdmissionLine:
    name: str
    amount: Decimal | None
    kind: str = INTAKE

    @property
    def is_priced(self) -> bool:
        return self.amount is not None


def naira(amount: int | str) -> Decimal:
    """Decimal from a string or int, never a float -- money must not carry
    binary rounding error."""
    return Decimal(str(amount))


def line(name: str, amount: int | str | None, kind: str = INTAKE) -> AdmissionLine:
    return AdmissionLine(
        name=name, amount=None if amount is None else naira(amount), kind=kind
    )


@dataclass(frozen=True)
class AdmissionSpec:
    """One intake fee sheet, applied to each of ``class_names`` separately."""

    class_names: tuple[str, ...]
    lines: tuple[AdmissionLine, ...]
    #: Empty means every arm of the named class; otherwise only these arms.
    streams: tuple[str, ...] = ()
    #: The school's own written total for the compulsory items, for
    #: cross-checking only. Books are quoted separately and excluded.
    client_total: Decimal | None = None
    #: Why the two disagree, printed by the seeder when they do.
    total_note: str = ""

    @property
    def pending_lines(self) -> tuple[AdmissionLine, ...]:
        return tuple(item for item in self.lines if not item.is_priced)

    @property
    def is_complete(self) -> bool:
        return not self.pending_lines

    @property
    def computed_total(self) -> Decimal:
        """Admission plus intake -- what the sheet's own total line covers."""
        return sum(
            (
                item.amount
                for item in self.lines
                if item.is_priced and item.kind in (ADMISSION, INTAKE)
            ),
            Decimal("0"),
        )

    def matches(self, klass) -> bool:
        if klass.name not in self.class_names:
            return False
        return not self.streams or klass.stream in self.streams


#: The one-off items each band charges at intake, in the order the school's own
#: sheet lists them.
NURSERY_INTAKE: tuple[AdmissionLine, ...] = (
    line("Admission Fee", 3_000, ADMISSION),
    line("Uniform", 9_000),
    line("Sportswear", 16_000),
    line("Exam / Dossier", 3_000),
    line("Track suit", 25_000),
)

PRIMARY_INTAKE: tuple[AdmissionLine, ...] = (
    line("Admission Fee", 3_000, ADMISSION),
    line("Uniform", 10_000),
    line("Sportswear", 16_000),
    line("Exam / Dossier", 3_000),
    line("Track suit", 25_000),
)

JUNIOR_INTAKE: tuple[AdmissionLine, ...] = (
    line("Admission Fee", 3_000, ADMISSION),
    # "(2)" is the school's own shorthand for two sets, and is kept.
    line("Uniform (2)", 24_000),
    line("Sportswear", 10_000),
    line("Exam / Dossier", 3_000),
    line("Track suit", 25_000),
)

#: Senior charges Practical where the younger bands charge Sportswear. Not a
#: rename of the same line -- a different thing, at a different price.
SENIOR_INTAKE: tuple[AdmissionLine, ...] = (
    line("Admission Fee", 3_000, ADMISSION),
    line("Uniform (2)", 30_000),
    line("Practical", 3_000),
    line("Exam / Dossier", 3_000),
    line("Track suit", 25_000),
)


def written_less_recurring(written: int, tuition: int, development: int = 2_000):
    """The school's written intake total, less the lines now billed per term."""
    return naira(written - tuition - development)


NURSERY_NOTE = (
    "School's written total was 81,000 including 24,000 tuition and 2,000 "
    "development, so 55,000 for the one-off items -- but its own one-off lines "
    "sum to 56,000. A 1,000 gap being confirmed with the client. Line items "
    "are seeded as supplied; no balancing line has been invented. Same "
    "handling as the Primary 1 gap in apps/fees/pricing.py."
)


ADMISSION_FEES: tuple[AdmissionSpec, ...] = (
    AdmissionSpec(
        ("Pre-KG", "KG 1", "KG 2", "KG 3"),
        NURSERY_INTAKE,
        client_total=written_less_recurring(81_000, 24_000),
        total_note=NURSERY_NOTE,
    ),
    AdmissionSpec(
        ("Primary 1", "Primary 2", "Primary 3", "Primary 4", "Primary 5",
         "Primary 6"),
        PRIMARY_INTAKE,
        client_total=written_less_recurring(85_000, 26_000),
    ),
    AdmissionSpec(
        ("JSS 1", "JSS 2", "JSS 3"),
        JUNIOR_INTAKE,
        client_total=written_less_recurring(98_000, 31_000),
    ),
    # One sheet for both arms now: the book list was the only thing that
    # differed between them, and books are billed per term.
    AdmissionSpec(
        ("SSS 1", "SSS 2", "SSS 3"),
        SENIOR_INTAKE,
        client_total=written_less_recurring(106_000, 40_000),
    ),
)


def specs_for(klass) -> list[AdmissionSpec]:
    """Every spec that should produce a schedule for ``klass``."""
    return [spec for spec in ADMISSION_FEES if spec.matches(klass)]


def pending_report() -> list[tuple[str, tuple[str, ...]]]:
    """Which sheets are still waiting on figures, and which lines.

    Used by the seeder to print a precise list rather than a vague failure, and
    by the test suite to assert that an incomplete sheet is refused rather than
    seeded as zeros.
    """
    report: list[tuple[str, tuple[str, ...]]] = []
    for spec in ADMISSION_FEES:
        if spec.is_complete:
            continue
        label = ", ".join(spec.class_names)
        report.append((label, tuple(item.name for item in spec.pending_lines)))
    return report
