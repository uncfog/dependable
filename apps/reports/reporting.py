"""Building the collections report: what it covers, and what it says.

Two jobs live here.

**Scope.** :func:`resolve_scope` answers "which campuses is this report about?".
The tenant-scoped managers have already decided which campuses the caller is
*allowed* to see; scope narrows that further to the one school or campus they
asked for. A principal has one campus and no choice to make; a proprietor picks
between theirs; the platform owner picks a school first. Nothing here widens
anything -- every queryset still goes through ``objects``, so a filter can only
ever produce a subset of what the caller could already read.

**The figures.** :func:`build` produces the :class:`~apps.reports.structure.Report`
that the page and the PDF both render. It defines nothing of its own:

* what a student is expected to pay, and which of the four states that puts them
  in, comes from ``apps.students.fees``;
* what counts as money received comes from ``apps.payments``, through
  ``apps.core.finance``, which the four dashboards read as well;
* who still owes comes from ``apps.payments.balances.outstanding`` -- the same
  call the bursar's chase list and messaging's "parents who owe" resolve to.

All this module does is *group* those answers -- by school, by campus, by class,
by method, by month -- and format them.

Two figures on this report are deliberately not the same thing, and it says so
in its own notes rather than quietly picking one:

* **Collected**, everywhere money is totalled, is every confirmed payment
  credited to the term. That is the till question, it is what the bursar's
  dashboard shows, and it adds up: the campus rows sum to the headline.
* **Still owed**, on the outstanding list, is what the *current roster* has left
  to pay. That is the chase question. It parts company with
  expected-less-collected whenever a student who has paid has since left, or a
  parent has overpaid.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from django.apps import apps as django_apps
from django.db.models import Count, Sum
from django.db.models.functions import TruncMonth
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from apps.core.finance import (
    PAYMENT_STATES,
    collection_summary,
    tally_states,
    with_outstanding,
)
from apps.fees.models import Term
from apps.schools.models import Branch, School
from apps.students import fees as student_fees
from apps.students.models import Student, StudentStatus

from .structure import (
    RIGHT,
    Cell,
    Chart,
    Column,
    Report,
    Segment,
    Stat,
    Table,
    bar_label,
    bar_share,
    prose,
)

ZERO = Decimal("0")

#: How many students to name on the report's outstanding table. The full list
#: lives at ``/payments/outstanding/``, paginated; a report is a summary, and
#: forty pages of names printed behind it would be a roster instead.
OUTSTANDING_LIMIT = 20


# ===========================================================================
# Scope
# ===========================================================================


@dataclass(frozen=True)
class ReportScope:
    """Which campuses a report covers, and what to call them.

    ``branches`` is already both tenant-scoped and narrowed to the caller's
    choice, so everything downstream filters on ``branch_ids`` and stops
    thinking about isolation.
    """

    branches: tuple[Branch, ...]
    chosen_school: School | None = None
    chosen_branch: Branch | None = None
    #: Every school the caller may choose between, and every campus. One entry
    #: (or none) means there is no choice worth offering.
    schools: tuple[School, ...] = ()
    choosable_branches: tuple[Branch, ...] = ()

    @property
    def branch_ids(self) -> list[int]:
        return [branch.pk for branch in self.branches]

    @property
    def is_empty(self) -> bool:
        return not self.branches

    @property
    def covers_many_branches(self) -> bool:
        return len(self.branches) > 1

    @property
    def covers_many_schools(self) -> bool:
        return len({branch.school_id for branch in self.branches}) > 1

    @property
    def label(self) -> str:
        """What this report is of, in the words on its cover.

        Built from the rows rather than from the filter: a proprietor who chose
        nothing is looking at every campus they run, and the report should say so
        rather than leaving the reader to infer it from a blank.
        """
        if self.chosen_branch is not None:
            return f"{self.chosen_branch.school.name} · {self.chosen_branch.name}"
        if not self.branches:
            return "No campuses"
        if len(self.branches) == 1:
            only = self.branches[0]
            return f"{only.school.name} · {only.name}"
        if not self.covers_many_schools:
            return f"{self.branches[0].school.name} · every campus"
        if self.chosen_school is not None:
            return f"{self.chosen_school.name} · every campus"
        return "Every school on the platform"


def resolve_scope(*, school_id=None, branch_id=None) -> ReportScope:
    """The campuses a report should cover, given what the caller asked for.

    Both arguments are whatever arrived in the query string -- possibly rubbish,
    possibly another tenant's id. Neither is trusted: they are matched against
    the tenant-scoped querysets, and an id that does not appear there counts as
    not chosen at all. So a caller cannot widen their scope by editing a URL, and
    does not get a 404 for trying either -- they get their own report, which is
    the screen they were entitled to.
    """
    visible = list(
        Branch.objects.select_related("school").order_by("school__name", "name")
    )
    schools = list(School.objects.order_by("name"))

    chosen_school = _pick(schools, school_id)
    branches = visible
    if chosen_school is not None:
        branches = [b for b in branches if b.school_id == chosen_school.pk]

    chosen_branch = _pick(branches, branch_id)
    if chosen_branch is not None:
        branches = [chosen_branch]
        # Choosing a campus settles the school as well, whether or not one was
        # asked for: otherwise the cover would name a campus and no school.
        chosen_school = chosen_school or chosen_branch.school

    return ReportScope(
        branches=tuple(branches),
        chosen_school=chosen_school,
        chosen_branch=chosen_branch,
        schools=tuple(schools),
        choosable_branches=tuple(visible),
    )


def _pick(rows, wanted):
    """The row with that pk, or ``None``. Never raises on a bad value."""
    if wanted in (None, ""):
        return None
    try:
        wanted = int(wanted)
    except (TypeError, ValueError):
        return None
    return next((row for row in rows if row.pk == wanted), None)


# ===========================================================================
# Grouping
# ===========================================================================


@dataclass
class Group:
    """One row of a breakdown: a campus, a class, a school.

    ``expected`` and the four ``counts`` come from each student's own fee
    position. ``collected`` is filled in afterwards from the payment ledger, for
    the reason in the module docstring: it is the till figure, so the rows add up
    to the headline.
    """

    label: str
    sublabel: str = ""
    students: int = 0
    priced: int = 0
    expected: Decimal = ZERO
    collected: Decimal = ZERO
    counts: dict[str, int] = field(
        default_factory=lambda: {key: 0 for key, _ in PAYMENT_STATES}
    )

    def add(self, position) -> None:
        self.students += 1
        if not position.is_priced:
            return
        self.priced += 1
        self.expected += position.expected
        self.counts[position.state] = self.counts.get(position.state, 0) + 1

    @property
    def outstanding(self) -> Decimal:
        """Never negative: a campus that has over-collected is holding a credit
        for somebody, not a negative debt."""
        return max(self.expected - self.collected, ZERO)


def _grouped(students, positions, key_of, label_of, sublabel_of=None) -> dict:
    """Group ``students`` by whatever ``key_of`` returns, first seen first."""
    groups: dict = {}
    for student in students:
        key = key_of(student)
        if key is None:
            continue
        group = groups.get(key)
        if group is None:
            group = groups[key] = Group(
                label=label_of(student),
                sublabel=sublabel_of(student) if sublabel_of else "",
            )
        group.add(positions[student.pk])
    return groups


# ===========================================================================
# The report
# ===========================================================================


def build(scope: ReportScope, *, prepared_for: str = "") -> Report:
    """The collections report for ``scope``.

    Every campus is measured against **its own** current term. That is not a
    convenience: a proprietor whose campuses are mid-transition has two terms in
    flight, and pricing one against the other's calendar would overstate one and
    understate the other. It is also why this screen has no term picker --
    ``fees.load`` resolves each student's own campus term itself, and a report
    that let you point four campuses at one term would be offering a figure
    nobody should act on.
    """
    branch_ids = scope.branch_ids
    terms = {
        term.branch_id: term
        for term in Term.objects.filter(is_current=True, branch_id__in=branch_ids)
    }

    students = list(
        Student.objects.filter(
            status=StudentStatus.ACTIVE, branch_id__in=branch_ids
        ).select_related("school_class", "branch", "branch__school")
    )
    schedule = student_fees.load(students)
    positions = {student.pk: schedule.position_for(student) for student in students}

    expected_total = sum(
        (p.expected for p in positions.values() if p.is_priced), ZERO
    )
    summary = with_outstanding(collection_summary(terms.values()), expected_total)
    ledger = _ledger(branch_ids, terms.values())

    by_branch = _group_branches(scope, students, positions, ledger)
    by_school = _group_schools(scope, by_branch)
    by_class = _grouped(
        students, positions,
        key_of=lambda s: s.school_class_id,
        label_of=lambda s: s.school_class.display_name,
        sublabel_of=(lambda s: s.branch.name) if scope.covers_many_branches else None,
    )
    for class_id, group in by_class.items():
        group.collected = ledger["by_class"].get(class_id, ZERO)

    states = tally_states(students, schedule)

    return Report(
        key="collections",
        title="Collections report",
        scope_label=scope.label,
        period_label=_period_label(scope, terms),
        generated_at=timezone.localtime(),
        prepared_for=prepared_for,
        stats=_stats(summary, expected_total, students, states)
        + _arrears_stat(positions),
        charts=(_collection_chart(summary, expected_total), _states_chart(states)),
        tables=_tables(scope, by_school, by_branch, by_class, terms, ledger, students),
        notes=_notes(scope, terms, students, positions, summary, expected_total),
    )


def _group_branches(scope, students, positions, ledger) -> dict:
    """One group per campus in scope -- including a campus with nobody enrolled.

    A campus missing from a report reads as a campus nobody checked, which is the
    opposite of what an empty one should tell a proprietor.
    """
    groups = {
        branch.pk: Group(label=branch.name, sublabel=branch.school.name)
        for branch in scope.branches
    }
    for student in students:
        groups[student.branch_id].add(positions[student.pk])
    for branch_id, group in groups.items():
        group.collected = ledger["by_branch"].get(branch_id, ZERO)
    return groups


def _group_schools(scope, by_branch) -> dict:
    """The campus groups rolled up per school. Platform scope's own question."""
    groups: dict = {}
    for branch in scope.branches:
        group = groups.get(branch.school_id)
        if group is None:
            group = groups[branch.school_id] = Group(label=branch.school.name)
        campus = by_branch[branch.pk]
        group.students += campus.students
        group.priced += campus.priced
        group.expected += campus.expected
        group.collected += campus.collected
        for key, count in campus.counts.items():
            group.counts[key] = group.counts.get(key, 0) + count
    for school_id, group in groups.items():
        campuses = sum(1 for b in scope.branches if b.school_id == school_id)
        group.sublabel = f"{campuses} campus{'' if campuses == 1 else 'es'}"
    return groups


def _ledger(branch_ids, terms) -> dict:
    """Confirmed money for those terms, grouped every way the report needs it.

    Five aggregates rather than a loop over payments: a term at a large school is
    thousands of rows, and none of them has to be in memory to be totalled.

    Resolved through the app registry, the same guard ``apps.core.finance`` uses.
    The report has to render whether or not the payments app is installed, and
    without it every figure here is honestly zero.
    """
    empty = {
        "by_branch": {}, "by_class": {},
        "by_method": [], "by_label": [], "by_month": [],
    }
    terms = list(terms)
    if not terms or not branch_ids or not django_apps.is_installed("apps.payments"):
        return empty

    from apps.payments.models import Payment, PaymentLabel, PaymentMethod, PaymentStatus

    confirmed = Payment.objects.filter(
        term__in=terms, branch_id__in=branch_ids, status=PaymentStatus.CONFIRMED
    )

    def totals(column, choices):
        labels = dict(choices)
        return [
            {
                "label": str(labels.get(row[column], row[column] or "Unknown")),
                "amount": row["amount"] or ZERO,
                "payments": row["payments"],
            }
            for row in confirmed.values(column)
            .annotate(amount=Sum("amount"), payments=Count("id"))
            .order_by("-amount")
        ]

    return {
        "by_branch": {
            row["branch_id"]: row["amount"] or ZERO
            for row in confirmed.values("branch_id").annotate(amount=Sum("amount"))
        },
        "by_class": {
            row["student__school_class_id"]: row["amount"] or ZERO
            for row in confirmed.values("student__school_class_id").annotate(
                amount=Sum("amount")
            )
        },
        "by_method": totals("method", PaymentMethod.choices),
        "by_label": totals("label", PaymentLabel.choices),
        "by_month": [
            {
                "label": row["month"].strftime("%B %Y") if row["month"] else "Undated",
                "amount": row["amount"] or ZERO,
                "payments": row["payments"],
            }
            # Chronological, not biggest first: this table is the shape of a
            # term's cash flow, and sorting it by size would destroy that.
            for row in confirmed.annotate(month=TruncMonth("date_paid"))
            .values("month")
            .annotate(amount=Sum("amount"), payments=Count("id"))
            .order_by("month")
        ],
    }


# ---------------------------------------------------------------------------
# Headline figures and charts
# ---------------------------------------------------------------------------


def _plural(count: int, word: str, suffix: str = "s") -> str:
    return f"{count:,} {word}{'' if count == 1 else suffix}"


def _arrears_stat(positions) -> tuple[Stat, ...]:
    """What is owed from earlier terms, beside this term's figures and not inside them.

    Expected, collected and outstanding answer for the current term. Mixing older debt
    into them would change what the collection rate means from one term to the next.
    Absent until a school has any, so nothing appears for a school with none.
    """
    total = sum((p.arrears_outstanding for p in positions.values()), ZERO)
    if not total:
        return ()
    owing = sum(1 for p in positions.values() if p.arrears_outstanding)
    return (
        Stat(
            label="From earlier terms",
            value=Cell.money(total),
            note=f"Still owed by {_plural(owing, 'student')}",
            tone="overdue",
        ),
    )


def _stats(summary, expected_total, students, states) -> tuple[Stat, ...]:
    collected = summary["collected_total"]
    pending = summary["pending_total"]
    return (
        Stat(
            label="Expected this term",
            value=Cell.money(expected_total),
            note=f"Across {_plural(len(students), 'enrolled student')}",
        ),
        Stat(
            label="Collected",
            value=Cell.money(collected),
            note="Confirmed payments only",
            tone="paid" if collected else "muted",
        ),
        Stat(
            label="Outstanding",
            value=Cell.money(summary["outstanding_total"]),
            note="Expected less confirmed",
            tone="overdue",
        ),
        Stat(
            label="Collection rate",
            value=Cell.share_of(collected, expected_total),
            note="Confirmed as a share of expected",
        ),
        Stat(
            label="Pending confirmation",
            value=Cell.money(pending),
            note=f"{_plural(summary['pending_count'], 'receipt')} not yet counted",
            tone="partial" if pending else "muted",
        ),
        Stat(
            label="Priced students",
            value=Cell.count(states["total"]),
            note="In a class with fees set this term",
        ),
    )


def _collection_chart(summary, expected_total) -> Chart:
    """Collection against the term: one measure against its own whole.

    Pending is its own segment because money handed in but not yet confirmed is
    genuinely a third state, and nobody reading this should have to guess which
    of the other two it is sitting in.
    """
    collected = summary["collected_total"]
    pending = summary["pending_total"]
    outstanding = summary["outstanding_total"]
    return Chart(
        key="collection",
        title="Collected against the term",
        headline=bar_label(collected, expected_total),
        caption="of the fees expected this term",
        legend_columns=3,
        segments=(
            Segment(
                key="paid",
                label="Confirmed",
                value=Cell.money(collected),
                share=bar_share(collected, expected_total),
                share_label=bar_label(collected, expected_total),
            ),
            Segment(
                key="partial",
                label="Pending confirmation",
                value=Cell.money(pending),
                share=bar_share(pending, expected_total),
                share_label=bar_label(pending, expected_total),
            ),
            Segment(
                key="still",
                label="Still to come",
                value=Cell.money(outstanding),
                share=bar_share(outstanding, expected_total),
                share_label=bar_label(outstanding, expected_total),
                # The empty part of the track: named in the legend, not drawn.
                in_bar=False,
            ),
        ),
    )


def _states_chart(states) -> Chart:
    total = states["total"]
    return Chart(
        key="states",
        title="Where the roster stands",
        headline=f"{total:,}",
        caption=f"priced student{'' if total == 1 else 's'}",
        legend_columns=4,
        segments=tuple(
            Segment(
                key=bucket["key"],
                label=bucket["label"],
                value=Cell.count(bucket["count"]),
                share=bar_share(bucket["count"], total),
                share_label=bar_label(bucket["count"], total),
            )
            for bucket in states["buckets"]
        ),
    )


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


def _href(url_name: str) -> str:
    """A nav link for a table heading, or nothing at all.

    Reversed here rather than in the template so the structure holds real URLs
    and the PDF renderer never has to know what a URL name is. A destination
    that does not exist yet simply drops the link, the same way the sidebar
    greys an unbuilt entry rather than pointing at ``#``.
    """
    try:
        return reverse(url_name)
    except NoReverseMatch:
        return ""


def _link(label: str, url_name: str) -> tuple[str, str] | None:
    href = _href(url_name)
    return (label, href) if href else None


def _money_columns(first: str, *, weight: float = 2.2) -> list[Column]:
    return [
        Column(first, weight=weight),
        Column("Students", align=RIGHT, numeric=True, weight=0.9),
        Column("Expected", align=RIGHT, numeric=True, weight=1.4),
        Column("Collected", align=RIGHT, numeric=True, weight=1.4),
        Column("Outstanding", align=RIGHT, numeric=True, weight=1.4),
        Column("Rate", align=RIGHT, numeric=True, weight=0.8),
        Column("Share of expected", bar=True, weight=1.2),
    ]


def _money_row(group: Group, biggest: Decimal) -> list[Cell]:
    return [
        Cell.of(group.label, strong=True),
        Cell.count(group.students),
        Cell.money(group.expected),
        Cell.money(group.collected),
        Cell.money(group.outstanding),
        Cell.share_of(group.collected, group.expected),
        Cell(text="", share=bar_share(group.expected, biggest)),
    ]


def _money_total(groups) -> list[Cell]:
    expected = sum((g.expected for g in groups), ZERO)
    collected = sum((g.collected for g in groups), ZERO)
    return [
        Cell.of("Total", strong=True),
        Cell.count(sum(g.students for g in groups)),
        Cell.money(expected, strong=True),
        Cell.money(collected, strong=True),
        Cell.money(max(expected - collected, ZERO), strong=True),
        Cell.share_of(collected, expected, strong=True),
        Cell(text=""),
    ]


def _tables(scope, by_school, by_branch, by_class, terms, ledger, students) -> tuple:
    tables: list[Table] = []

    if len(by_school) > 1:
        groups = sorted(by_school.values(), key=lambda g: g.expected, reverse=True)
        biggest = max((g.expected for g in groups), default=ZERO)
        columns = _money_columns("School")
        columns.insert(1, Column("Campuses", weight=1.2))
        rows = []
        for group in groups:
            row = _money_row(group, biggest)
            row.insert(1, Cell.of(group.sublabel))
            rows.append(tuple(row))
        total = _money_total(groups)
        total.insert(1, Cell(text=""))
        tables.append(Table(
            key="schools",
            title="By school",
            columns=tuple(columns),
            rows=tuple(rows),
            total=tuple(total),
            note=prose("Each school measured against its own campuses' current "
                       "terms."),
        ))

    if scope.covers_many_branches:
        ordered = [by_branch[branch.pk] for branch in scope.branches]
        biggest = max((g.expected for g in ordered), default=ZERO)
        columns = _money_columns("Campus")
        columns.insert(1, Column("Current term", weight=1.6))
        rows = []
        for branch in scope.branches:
            row = _money_row(by_branch[branch.pk], biggest)
            term = terms.get(branch.pk)
            row.insert(1, Cell.of(term.name if term else "No current term",
                                  muted=term is None))
            rows.append(tuple(row))
        total = _money_total(ordered)
        total.insert(1, Cell(text=""))
        tables.append(Table(
            key="branches",
            title="By campus",
            columns=tuple(columns),
            rows=tuple(rows),
            total=tuple(total),
            note=prose("A campus with no current term has nothing to price, so "
                       "its expected total is unknown rather than zero."),
            link=_link("Manage branches", "schools:branch_list"),
        ))

    tables.append(_class_table(scope, by_class))
    tables.append(_states_table(scope, by_class))

    for key, first, title, rows, note in (
        ("methods", "Method", "How the money came in", ledger["by_method"],
         prose("Confirmed payments only, grouped by how each one was taken.")),
        ("labels", "Recorded as", "What it was recorded against", ledger["by_label"],
         prose("Labels are descriptive: a balance stays one combined figure, so "
               "these shares say what parents thought they were paying for, not "
               "what they owe per line.")),
        ("months", "Month", "When it arrived", ledger["by_month"],
         prose("By the date the money was received, not the date it was typed "
               "in.")),
    ):
        tables.append(_amount_table(key, first, title, rows, note))

    tables.append(_outstanding_table(students))
    return tuple(tables)


def _class_table(scope, by_class) -> Table:
    # Only classes that have fees set this term. A class whose children are all
    # unpriced has an *unknown* expected total, not a zero one, and a row of
    # zeroes here would read as a class that charges nothing -- which is the one
    # thing the note underneath promises this table does not say. The children
    # themselves are named in the report's notes instead.
    groups = [group for group in by_class.values() if group.priced]
    groups.sort(key=lambda g: g.expected, reverse=True)
    biggest = max((g.expected for g in groups), default=ZERO)
    columns = _money_columns("Class")
    rows = []
    for group in groups:
        row = _money_row(group, biggest)
        if scope.covers_many_branches:
            row.insert(1, Cell.of(group.sublabel))
        rows.append(tuple(row))
    total = _money_total(groups)
    if scope.covers_many_branches:
        columns.insert(1, Column("Campus", weight=1.4))
        total.insert(1, Cell(text=""))
    return Table(
        key="classes",
        title="By class",
        columns=tuple(columns),
        rows=tuple(rows),
        total=tuple(total) if rows else (),
        note=prose(
            "Expected is the class's fee structure for the term multiplied by the "
            "children enrolled in it. Two things are therefore missing here and "
            "present in the campus totals: a class with no fee structure, whose "
            "expected total is unknown rather than zero, and confirmed payments "
            "from children who have since left the roster."
        ),
        empty="No class has fees set for the current term yet.",
        link=_link("Fee structures", "fees:structure_list"),
    )


def _states_table(scope, by_class) -> Table:
    """The four payment states, per class: the chart above, read as numbers."""
    groups = sorted(by_class.values(), key=lambda g: g.priced, reverse=True)
    groups = [group for group in groups if group.priced]
    columns: list[Column] = [Column("Class", weight=2.2)]
    if scope.covers_many_branches:
        columns.append(Column("Campus", weight=1.4))
    columns += [
        Column(label, align=RIGHT, numeric=True, weight=1.0)
        for _, label in PAYMENT_STATES
    ]
    columns.append(Column("Priced", align=RIGHT, numeric=True, weight=1.0))

    rows = []
    for group in groups:
        row: list[Cell] = [Cell.of(group.label, strong=True)]
        if scope.covers_many_branches:
            row.append(Cell.of(group.sublabel))
        row += [Cell.count(group.counts.get(key, 0)) for key, _ in PAYMENT_STATES]
        row.append(Cell.count(group.priced))
        rows.append(tuple(row))

    total: list[Cell] = [Cell.of("Total", strong=True)]
    if scope.covers_many_branches:
        total.append(Cell(text=""))
    total += [
        Cell.count(sum(g.counts.get(key, 0) for g in groups), strong=True)
        for key, _ in PAYMENT_STATES
    ]
    total.append(Cell.count(sum(g.priced for g in groups), strong=True))

    return Table(
        key="class_states",
        title="Payment status by class",
        columns=tuple(columns),
        rows=tuple(rows),
        total=tuple(total) if rows else (),
        note=prose(
            "Counted from each child's own fee position. A child in a class with "
            "no fees set this term is in none of the four states -- that is a "
            "setup gap rather than a debt, and counting them as unpaid would "
            "invent one. Overdue is not a fifth state either: it is unpaid or "
            "part paid after the term's own due date."
        ),
        empty="No class has fees set for the current term yet.",
    )


def _amount_table(key: str, first: str, title: str, rows, note: Cell) -> Table:
    biggest = max((row["amount"] for row in rows), default=ZERO)
    total = sum((row["amount"] for row in rows), ZERO)
    return Table(
        key=key,
        title=title,
        columns=(
            Column(first, weight=2.0),
            Column("Payments", align=RIGHT, numeric=True, weight=1.0),
            Column("Amount", align=RIGHT, numeric=True, weight=1.5),
            Column("Share", align=RIGHT, numeric=True, weight=0.9),
            Column("Share of the total", bar=True, weight=1.4),
        ),
        rows=tuple(
            (
                Cell.of(row["label"], strong=True),
                Cell.count(row["payments"]),
                Cell.money(row["amount"]),
                Cell.share_of(row["amount"], total),
                Cell(text="", share=bar_share(row["amount"], biggest)),
            )
            for row in rows
        ),
        total=(
            (
                Cell.of("Total", strong=True),
                Cell.count(sum(row["payments"] for row in rows)),
                Cell.money(total, strong=True),
                Cell.of("100%", strong=True),
                Cell(text=""),
            )
            if rows
            else ()
        ),
        note=note,
        empty="No confirmed payments against the current term yet.",
    )


def _outstanding_table(students) -> Table:
    """Who still owes, most owed first.

    Goes through ``balances.outstanding`` rather than re-filtering the positions
    this module already has in hand. It costs one more load of the same schedule,
    and it buys the guarantee that the names on this report are exactly the names
    on the bursar's chase list and exactly the parents a fee reminder would
    reach. A second copy of the rule "priced, and owes more than nothing" is a
    second thing to keep in step.
    """
    rows: list[tuple[Cell, ...]] = []
    owed_total = ZERO
    found_count = 0

    if django_apps.is_installed("apps.payments"):
        from apps.payments import balances

        found = balances.outstanding(students)
        found_count = len(found)
        owed_total = sum((row.owed for row in found), ZERO)
        biggest = found[0].owed if found else ZERO
        for row in found[:OUTSTANDING_LIMIT]:
            student, position = row.student, row.position
            rows.append((
                Cell.of(student.formal_name, strong=True,
                        href=student.get_absolute_url()),
                Cell.of(student.admission_number),
                Cell.of(student.school_class.display_name),
                Cell.of(student.parent_name),
                Cell.money(position.expected),
                Cell.money(position.paid),
                Cell.money(row.owed, strong=True),
                Cell.of(position.pill_label, pill=position.pill_class),
                Cell(text="", share=bar_share(row.owed, biggest)),
            ))

    explanation = (
        "What the current roster still owes -- each child's expected fee less "
        "their own confirmed payments. A different question from Outstanding "
        "above, which is the term's expected total less every confirmed payment "
        "against it: the two part company when a child who has paid has since "
        "left, or a parent has overpaid."
    )
    if found_count > len(rows):
        note = prose(
            f"The {len(rows)} largest of {found_count:,} balances, owing {{owed}} "
            f"between them. " + explanation,
            owed=owed_total,
        )
    else:
        note = prose(f"{{owed}} owed in total. " + explanation, owed=owed_total)

    return Table(
        key="outstanding",
        title="Largest outstanding balances",
        columns=(
            Column("Student", weight=2.0),
            Column("Admission no.", weight=1.3),
            Column("Class", weight=1.3),
            Column("Parent / guardian", weight=1.8),
            Column("Expected", align=RIGHT, numeric=True, weight=1.3),
            Column("Paid", align=RIGHT, numeric=True, weight=1.3),
            Column("Owed", align=RIGHT, numeric=True, weight=1.3),
            Column("Status", weight=1.1),
            Column("Share of the largest", bar=True, weight=1.0),
        ),
        rows=tuple(rows),
        note=note,
        empty="Nobody in a priced class owes anything for the current term.",
        link=_link("Full list", "payments:outstanding"),
    )


# ---------------------------------------------------------------------------
# The prose
# ---------------------------------------------------------------------------


def _period_label(scope, terms) -> str:
    if not scope.branches:
        return "No campuses in scope"
    if len(scope.branches) == 1:
        term = terms.get(scope.branches[0].pk)
        return term.name if term else "No current term set"
    missing = [b.name for b in scope.branches if b.pk not in terms]
    label = "Each campus against its own current term"
    if missing:
        label += f" · none set at {', '.join(missing)}"
    return label


def _notes(scope, terms, students, positions, summary, expected_total) -> tuple:
    if not scope.branches:
        return (
            prose(
                "This account has no campus assigned, so there is nothing to "
                "report on. A proprietor, or the platform, can assign one."
            ),
        )

    notes: list[Cell] = []

    missing = [b.name for b in scope.branches if b.pk not in terms]
    if missing:
        notes.append(prose(
            f"No current term is set at {', '.join(missing)}. Fees are always "
            f"priced against a term, so nothing at "
            f"{'those campuses' if len(missing) > 1 else 'that campus'} is "
            f"counted in any figure above -- their expected total is unknown, "
            f"not zero."
        ))

    unpriced = _unpriced_classes(students, positions, terms)
    if unpriced:
        listed = ", ".join(
            f"{name} ({_plural(count, 'student')})" for name, count in unpriced
        )
        notes.append(prose(
            f"Not counted above: {listed}. These classes have children enrolled "
            f"but no fee structure for the current term, so what they owe is "
            f"unknown rather than nothing."
        ))

    notes.append(prose(
        "Expected is each class's fee structure for the term multiplied by the "
        "children enrolled in it. It is derived when this report is generated "
        "and never stored, which is why re-pricing a class or voiding a payment "
        "moves every figure here at once. Collected counts confirmed payments "
        "only: a receipt still waiting to be checked is reported separately and "
        "moves no balance."
    ))

    if summary["pending_total"]:
        would_be = bar_label(
            summary["collected_total"] + summary["pending_total"], expected_total
        )
        notes.append(prose(
            f"{_plural(summary['pending_count'], 'receipt')} worth {{pending}} "
            f"{'is' if summary['pending_count'] == 1 else 'are'} pending. "
            f"Confirming {'it' if summary['pending_count'] == 1 else 'them'} "
            f"would take the collection rate to {would_be}.",
            pending=summary["pending_total"],
        ))

    return tuple(notes)


def _unpriced_classes(students, positions, terms) -> list[tuple[str, int]]:
    """Classes with children in them and no fees set for the current term.

    Only where the campus *has* a current term. A campus without one is a
    different gap, reported on its own above, and listing every one of its
    classes here would bury it.
    """
    counts: dict[int, int] = {}
    names: dict[int, str] = {}
    for student in students:
        if positions[student.pk].is_priced or student.branch_id not in terms:
            continue
        if student.school_class_id is None:
            continue
        counts[student.school_class_id] = counts.get(student.school_class_id, 0) + 1
        names[student.school_class_id] = student.school_class.display_name
    return [
        (names[class_id], count)
        for class_id, count in sorted(counts.items(), key=lambda kv: -kv[1])
    ]
