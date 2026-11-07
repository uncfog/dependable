"""Judging a workbook's rows, then writing the ones that passed, then taking them back.

The rows are judged again on every write, because the report a person read was only
true when it was drawn. Nothing already on the system is ever overwritten: a row either
adds something or points at what is there. No messages are sent, because nothing here
goes near the messaging code.

Writing runs against a time budget and picks up where it stopped, so a large school
can't run into the server's 60-second limit, and each row saves or fails on its own.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections import defaultdict
from datetime import datetime, time as clock
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import ProtectedError, Sum
from django.db.models.deletion import RestrictedError
from django.utils import timezone

from apps.academics.lookup import ClassIndex
from apps.audit.recording import record
from apps.audit.models import Category
from apps.core.imports import SourceRow, parse_amount, parse_date
from apps.families.models import Family, FamilyMembership
from apps.families.services import FamilyLinkError, link, unlink
from apps.fees.models import (
    AdjustmentKind,
    StudentFeeAdjustment,
    StudentTermEnrolment,
    Term,
    TermSequence,
)
from apps.payments import allocation
from apps.payments.models import (
    Payment,
    PaymentLabel,
    PaymentMethod,
    PaymentSource,
    PaymentStatus,
    PaymentTransaction,
    TransactionKind,
)
from apps.schools.models import Branch
from apps.students.importer import _validate_row, parse_import_phone
from apps.students.models import Student, StudentStatus
from apps.students.validators import normalise_admission_number

from .models import BatchStatus, ImportBatch, ImportRecord, ImportRow, MatchDecision, RowStatus
from .sheets import KEYS

ZERO = Decimal("0")
BUDGET_SECONDS = 30
BUSY_FOR_SECONDS = 90

_WRITTEN = (RowStatus.DONE, RowStatus.FAILED)
_SEQUENCES = {"first": TermSequence.FIRST, "second": TermSequence.SECOND, "third": TermSequence.THIRD}
_TERM_TEXT = re.compile(r"^\s*(first|second|third)\b.*?(\d{4}\s*/\s*\d{4})\s*$", re.I)
_KINDS = {label.lower(): value for value, label in AdjustmentKind.choices} | {
    value.lower(): value for value, _ in AdjustmentKind.choices
} | {"opening balance": AdjustmentKind.OPENING_BALANCE, "charge": AdjustmentKind.CHARGE}
_METHODS = {label.lower(): value for value, label in PaymentMethod.choices} | {
    value.lower(): value for value, _ in PaymentMethod.choices
} | {"transfer": PaymentMethod.TRANSFER}
_LABELS = {label.lower(): value for value, label in PaymentLabel.choices} | {
    value.lower(): value for value, _ in PaymentLabel.choices
}


def _folded(value: str) -> str:
    return (value or "").strip().upper()


# ---------------------------------------------------------------------------
# Judging
# ---------------------------------------------------------------------------


class Judge:
    """Everything known about the school and the file, then a verdict on each row."""

    def __init__(self, batch: ImportBatch):
        self.batch = batch
        self.school_id = batch.school_id
        self.branches = list(Branch.all_objects.filter(school_id=self.school_id))
        self.by_campus = {_folded(b.name): b for b in self.branches}
        self.classes = {}
        self.terms = defaultdict(list)
        self.current = {}
        for term in Term.all_objects.filter(branch__school_id=self.school_id):
            self.terms[term.branch_id].append(term)
            if term.is_current:
                self.current[term.branch_id] = term
        #: (campus, NUMBER) -> the child, or None for one this file will create.
        self.children = {}
        self.names = defaultdict(list)
        self.existing = {}
        for child in Student.all_objects.filter(school_id=self.school_id):
            key = (child.branch_id, _folded(child.admission_number))
            self.children[key] = child
            self.existing[key] = child
            if child.date_of_birth:
                self.names[self._name_key(child.first_name, child.last_name, child.date_of_birth)].append(
                    child.admission_number
                )
        self.families = {
            _folded(f.family_code): f
            for f in Family.all_objects.filter(school_id=self.school_id).exclude(family_code="")
        }
        self.planned_families = set()
        self.member_of = {}
        for membership in FamilyMembership.all_objects.filter(
            school_id=self.school_id, ended_at__isnull=True
        ).select_related("family", "student"):
            key = (membership.student.branch_id, _folded(membership.student.admission_number))
            self.member_of[key] = membership.family
        self.legacy = set(
            _folded(value)
            for value in PaymentTransaction.all_objects.filter(school_id=self.school_id)
            .exclude(legacy_reference="")
            .exclude(status=PaymentStatus.VOID)
            .values_list("legacy_reference", flat=True)
        )
        self.money = set(
            (str(date_paid), amount, _folded(reference))
            for date_paid, amount, reference in PaymentTransaction.all_objects.filter(
                school_id=self.school_id
            )
            .exclude(reference="")
            .exclude(status=PaymentStatus.VOID)
            .values_list("date_paid", "amount", "reference")
        )
        self.remembered = dict(
            MatchDecision.all_objects.filter(school_id=self.school_id).values_list(
                "signature", "decision"
            )
        )

    @staticmethod
    def _name_key(first, last, born) -> tuple:
        return (_folded(first), _folded(last), str(born))

    # -- shared questions ----------------------------------------------------

    def campus(self, text: str):
        """(branch, None) or (None, why)."""
        text = (text or "").strip()
        if text:
            branch = self.by_campus.get(_folded(text))
            if branch is None:
                names = ", ".join(b.name for b in self.branches)
                return None, f"There is no campus called '{text}'. This school has: {names}."
            return branch, None
        if len(self.branches) == 1:
            return self.branches[0], None
        return None, "Say which campus: this school has more than one."

    def child(self, row_values: dict):
        """(key, None) or (None, why). Campus can be left out when the number is unique."""
        number = _folded(normalise_admission_number(row_values.get("admission_number", "")))
        campus_text = row_values.get("campus", "")
        if campus_text or len(self.branches) == 1:
            branch, why = self.campus(campus_text)
            if why:
                return None, why
            keys = [(branch.pk, number)]
        else:
            keys = [(b.pk, number) for b in self.branches if (b.pk, number) in self.children]
            if len(keys) > 1:
                return None, f"{number} is used at more than one campus. Say which."
        if not keys or keys[0] not in self.children:
            return None, f"No child with admission number {number} is on the system or in this file."
        return keys[0], None

    def term(self, branch_id: int, text: str):
        """((year, sequence, term or None), None), or (None, why)."""
        text = (text or "").strip()
        terms = self.terms[branch_id]
        found = next((t for t in terms if _folded(t.name) == _folded(text)), None)
        if found is None:
            match = _TERM_TEXT.match(text)
            if match is None:
                return None, f"'{text}' could not be read as a term. Write it like First Term 2025/2026."
            year = re.sub(r"\s", "", match.group(2))
            sequence = _SEQUENCES[match.group(1).lower()]
            found = next((t for t in terms if t.academic_year == year and t.sequence == sequence), None)
            if found is None:
                key = (year, sequence)
                current = self.current.get(branch_id)
                if current is not None and key > current.sort_key:
                    return None, f"'{text}' is after this campus's current term."
                return (year, sequence, None), None
        current = self.current.get(branch_id)
        if current is not None and found.sort_key > current.sort_key:
            return None, f"'{text}' is after this campus's current term."
        return (found.academic_year, found.sequence, found), None

    def amount(self, text: str):
        value = parse_amount(text or "")
        if value is None or value <= ZERO:
            return None, f"'{text}' is not an amount above zero."
        return value, None

    def ask(self, row, signature: str, message: str, options: list[str]) -> str:
        """Put a question on the row. Returns the answer, or "" while it is open."""
        # Hashed: it outlives the file, and shouldn't keep a child's name with it.
        row.signature = hashlib.sha256(signature.encode()).hexdigest()
        row.options = options
        row.messages = [message]
        decision = row.decision or self.remembered.get(row.signature, "")
        if decision not in options:
            row.status = RowStatus.REVIEW
            return ""
        row.decision = decision
        if decision == "skip":
            row.status = RowStatus.SKIP
        return decision

    # -- running it ----------------------------------------------------------

    def run(self) -> None:
        rows = list(self.batch.rows.all())
        by_sheet = defaultdict(list)
        for row in rows:
            by_sheet[row.sheet].append(row)
        self.allocations = defaultdict(list)
        for row in by_sheet["allocations"]:
            self.allocations[_folded(row.raw.get("legacy_id", ""))].append(row)

        for sheet in KEYS:
            if sheet == "allocations":
                continue
            judge = getattr(self, f"_judge_{sheet}")
            for row in by_sheet[sheet]:
                if row.status in _WRITTEN:
                    continue
                row.status, row.messages, row.options = RowStatus.READY, [], []
                judge(row)
        self._judge_allocations(by_sheet["allocations"], by_sheet["payments"])
        ImportRow.objects.bulk_update(
            rows, ["status", "messages", "options", "signature", "decision"], batch_size=500
        )

    def _problem(self, row, *messages) -> None:
        row.status = RowStatus.PROBLEM
        row.messages = [m for m in messages if m]

    # -- families ------------------------------------------------------------

    def _judge_families(self, row) -> None:
        raw = row.raw
        code = _folded(raw.get("family_id"))
        if not code or not (raw.get("name") or "").strip():
            return self._problem(row, "A family needs a Family ID and a name.")
        if code in self.planned_families:
            return self._problem(row, f"Family ID {code} appears twice on this sheet.")
        phone = parse_import_phone(raw.get("phone", ""))
        existing = self.families.get(code)
        self.planned_families.add(code)
        if existing is None:
            return None
        differs = (
            phone and existing.contact_phone and phone != existing.contact_phone
        ) or (
            raw.get("email") and existing.contact_email
            and _folded(raw["email"]) != _folded(existing.contact_email)
        )
        if not differs:
            row.status = RowStatus.MATCHED
            row.messages = [f"{existing.name} is already on the system."]
            return None
        decision = self.ask(
            row, f"family:{code}:{phone}",
            f"Family ID {code} is already {existing.name}, with different contact details. "
            f"Is it the same family? Their details won't be changed either way.",
            ["use_existing", "skip"],
        )
        if decision == "use_existing":
            row.status = RowStatus.MATCHED
        elif decision == "skip":
            self.planned_families.discard(code)

    # -- students ------------------------------------------------------------

    def _judge_students(self, row) -> None:
        raw = row.raw
        branch, why = self.campus(raw.get("campus", ""))
        if why:
            return self._problem(row, why)
        number = normalise_admission_number(raw.get("admission_number", ""))
        key = (branch.pk, _folded(number))
        seen = getattr(self, "_seen_students", None)
        if seen is None:
            seen = self._seen_students = {}
        if number and key in seen:
            return self._problem(row, f"'{number}' is already used on row {seen[key]} of this sheet.")
        if number:
            seen[key] = row.number

        result = _validate_row(
            SourceRow(number=row.number, values=raw), branch, self._classes(branch), {}, {}
        )
        if result.errors:
            return self._problem(row, *[error.message for error in result.errors])
        new = result.record

        existing = self.existing.get(key)
        if existing is not None:
            same = (
                _folded(existing.first_name) == _folded(new.first_name)
                and _folded(existing.last_name) == _folded(new.last_name)
                and (not new.date_of_birth or new.date_of_birth == existing.date_of_birth)
            )
            if same:
                row.status = RowStatus.MATCHED
                row.messages = [f"{existing.full_name} is already on the system."]
            else:
                decision = self.ask(
                    row, f"student:{branch.pk}:{key[1]}:{_folded(new.first_name)}:{_folded(new.last_name)}",
                    f"{number} already belongs to {existing.full_name}, but this row says "
                    f"{new.first_name} {new.last_name}. Their record won't be changed.",
                    ["use_existing", "skip"],
                )
                if decision == "use_existing":
                    row.status = RowStatus.MATCHED
            if row.status in (RowStatus.MATCHED, RowStatus.REVIEW):
                self._plan_family(key, raw.get("family_id"), row)
            return None

        if new.date_of_birth:
            name_key = self._name_key(new.first_name, new.last_name, new.date_of_birth)
            others = [n for n in self.names[name_key] if _folded(n) != key[1]]
            if others:
                decision = self.ask(
                    row, f"twin:{name_key}:{key[1]}",
                    f"{others[0]} has the same name and date of birth. Are they different "
                    f"children? They will never be merged.",
                    ["create", "skip"],
                )
                if decision != "create":
                    if not decision:
                        self._plan_new(key, new, raw.get("family_id"), row)
                    return None
                row.status = RowStatus.READY
            self.names[name_key].append(number)
        self._plan_new(key, new, raw.get("family_id"), row)

    def _plan_new(self, key, new, family_code, row) -> None:
        self.children[key] = new
        self._plan_family(key, family_code, row)

    def _plan_family(self, key, family_code, row) -> None:
        code = _folded(family_code)
        if not code:
            return
        current = self.member_of.get(key)
        if current is not None:
            if _folded(current.family_code) != code:
                row.messages = row.messages + [
                    f"Already in {current.name}, so Family ID {code} is ignored for this child."
                ]
            return
        self.planned_families.add(code)
        self.member_of[key] = code

    def _classes(self, branch):
        if branch.pk not in self.classes:
            self.classes[branch.pk] = ClassIndex(branch)
        return self.classes[branch.pk]

    # -- links ---------------------------------------------------------------

    def _family_known(self, code: str) -> bool:
        return code in self.families or code in self.planned_families

    def _judge_links(self, row) -> None:
        code = _folded(row.raw.get("family_id"))
        if not self._family_known(code):
            return self._problem(row, f"No family with ID {code} is on the system or in this file.")
        key, why = self.child(row.raw)
        if why:
            return self._problem(row, why)
        current = self.member_of.get(key)
        if current is None:
            self.member_of[key] = code
            return None
        current_code = current if isinstance(current, str) else _folded(current.family_code)
        if current_code == code:
            row.status = RowStatus.MATCHED
            row.messages = ["This child is already in that family."]
            return None
        name = current if isinstance(current, str) else current.name
        return self._problem(row, f"This child is already in {name}. Remove them from it first.")

    # -- fee records ---------------------------------------------------------

    def _judge_fees(self, row) -> None:
        raw = row.raw
        key, why = self.child(raw)
        if why:
            return self._problem(row, why)
        problems = []
        _, why = self.term(key[0], raw.get("term"))
        problems.append(why)
        if _KINDS.get((raw.get("kind") or "").strip().lower()) is None:
            problems.append(
                f"'{raw.get('kind')}' is not a type. Use Opening balance, Extra charge, "
                f"Discount, Scholarship or Waiver."
            )
        _, why = self.amount(raw.get("amount"))
        problems.append(why)
        if not (raw.get("description") or "").strip():
            problems.append("Say what this fee record is for.")
        if raw.get("school_class"):
            branch = next(b for b in self.branches if b.pk == key[0])
            _, why = self._classes(branch).resolve(raw["school_class"])
            problems.append(why)
        if any(problems):
            self._problem(row, *problems)

    # -- payments ------------------------------------------------------------

    def _judge_payments(self, row) -> None:
        raw = row.raw
        legacy = _folded(raw.get("legacy_id"))
        seen = getattr(self, "_seen_legacy", None)
        if seen is None:
            seen = self._seen_legacy = set()
        shares = self.allocations.get(legacy, [])

        if not legacy:
            return self._problem(row, "A payment needs a Transaction ID.")
        if legacy in seen:
            return self._problem(row, f"Transaction ID {legacy} appears twice on this sheet.")
        seen.add(legacy)
        if legacy in self.legacy:
            row.status = RowStatus.SKIP
            row.messages = ["Already imported, so it isn't added again."]
            return None
        status_text = (raw.get("status") or "confirmed").strip().lower()
        if status_text == "void" or status_text == "voided":
            row.status = RowStatus.SKIP
            row.messages = ["Voided payments aren't imported."]
            return None
        problems = []
        if status_text not in ("confirmed", "pending"):
            problems.append(f"'{raw.get('status')}' is not a status. Use Confirmed or Pending.")
        amount, why = self.amount(raw.get("amount"))
        problems.append(why)
        paid = parse_date(raw.get("date_paid") or "")
        if paid is None:
            problems.append(f"'{raw.get('date_paid')}' could not be read as a date.")
        elif paid > timezone.localdate():
            problems.append("That date is in the future.")
        if _METHODS.get((raw.get("method") or "").strip().lower()) is None:
            problems.append(f"'{raw.get('method')}' is not a method. Use Cash, Bank transfer, POS or Other.")

        code = _folded(raw.get("family_id"))
        number = (raw.get("admission_number") or "").strip()
        if code and not self._family_known(code):
            problems.append(f"No family with ID {code} is on the system or in this file.")
        if number and shares:
            problems.append("Name one child here or use the allocations sheet, not both.")
        if not number and not code and len(shares) != 1:
            problems.append(
                "Say who this is for: an admission number for one child, or a Family ID "
                "for a payment shared between children."
            )
        if number:
            key, why = self.child(raw)
            problems.append(why)
            if key:
                problems.append(self._term_problem(key[0], raw.get("term")))

        branch = None
        if raw.get("campus") or len(self.branches) == 1:
            branch, why = self.campus(raw.get("campus", ""))
            problems.append(why)
        elif not number and not shares:
            problems.append("Say which campus took this payment.")

        total = ZERO
        for share in shares:
            why = self._share_problem(share, code)
            if why:
                problems.append(f"Allocation on row {share.number}: {why}")
            else:
                total += parse_amount(share.raw["amount"])
        if amount is not None and total > amount:
            problems.append(
                f"Its allocations add up to {total:,.2f}, more than the {amount:,.2f} received."
            )
        if not code and len(shares) == 1 and amount is not None and total != amount:
            problems.append(
                "One child's allocation has to be the whole payment. Give it a Family ID "
                "if part of it is still to be shared."
            )
        if any(problems):
            return self._problem(row, *problems)

        reference = _folded(raw.get("reference"))
        if reference:
            money = (paid.isoformat(), amount, reference)
            if money in self.money:
                decision = self.ask(
                    row, f"money:{legacy}",
                    f"A payment of {amount:,.2f} on {paid} with reference {reference} is already "
                    f"recorded. Is this a different payment?",
                    ["create", "skip"],
                )
                return None
            self.money.add(money)
        return None

    def _term_problem(self, branch_id, text):
        if not (text or "").strip():
            return "A payment for one child needs a term."
        _, why = self.term(branch_id, text)
        return why

    def _share_problem(self, share, code):
        key, why = self.child(share.raw)
        if why:
            return why
        why = self._term_problem(key[0], share.raw.get("term"))
        if why:
            return why
        _, why = self.amount(share.raw.get("amount"))
        if why:
            return why
        if code:
            current = self.member_of.get(key)
            current_code = current if isinstance(current, (str, type(None))) else _folded(current.family_code)
            if current_code != code:
                return f"that child is not in family {code}."
        return None

    def _judge_allocations(self, rows, payments) -> None:
        verdicts = {_folded(p.raw.get("legacy_id")): p for p in payments}
        for row in rows:
            if row.status in _WRITTEN:
                continue
            row.options = []
            parent = verdicts.get(_folded(row.raw.get("legacy_id")))
            if parent is None:
                self._problem(row, f"No payment with Transaction ID {row.raw.get('legacy_id')} is on the sheet.")
            elif parent.status == RowStatus.DONE:
                row.status = RowStatus.DONE
            elif parent.status == RowStatus.READY:
                row.status, row.messages = RowStatus.READY, []
            elif parent.status == RowStatus.REVIEW:
                row.status, row.messages = RowStatus.MATCHED, ["Waiting on its payment's review."]
            elif parent.status == RowStatus.SKIP:
                row.status, row.messages = RowStatus.SKIP, ["Its payment is left out."]
            else:
                self._problem(row, "Its payment can't be imported.")


def judge(batch: ImportBatch) -> None:
    Judge(batch).run()


# ---------------------------------------------------------------------------
# What a person sees before deciding
# ---------------------------------------------------------------------------


def summary(batch: ImportBatch) -> list[dict]:
    counts = defaultdict(lambda: defaultdict(int))
    for sheet, status in batch.rows.values_list("sheet", "status"):
        counts[sheet][status] += 1
    lines = []
    for key, sheet in KEYS.items():
        if key in counts:
            line = {"sheet": sheet.name, "key": key}
            for status in RowStatus.values:
                line[status] = counts[key][status]
            lines.append(line)
    return lines


def money_preview(batch: ImportBatch) -> dict:
    received = allocated = ZERO
    shared = defaultdict(lambda: ZERO)
    for row in batch.rows.filter(sheet="allocations"):
        shared[_folded(row.raw.get("legacy_id"))] += parse_amount(row.raw.get("amount", "")) or ZERO
    rows = batch.rows.filter(sheet="payments", status__in=[RowStatus.READY, RowStatus.DONE])
    for row in rows:
        amount = parse_amount(row.raw.get("amount", ""))
        if amount and (row.raw.get("status") or "confirmed").strip().lower() == "confirmed":
            received += amount
            legacy = _folded(row.raw.get("legacy_id"))
            if legacy in shared:
                allocated += shared[legacy]
            elif (row.raw.get("admission_number") or "").strip():
                allocated += amount
    before = (
        Payment.all_objects.filter(school_id=batch.school_id, status=PaymentStatus.CONFIRMED)
        .aggregate(total=Sum("amount"))["total"] or ZERO
    )
    return {
        "received": received,
        "allocated": allocated,
        "unallocated": received - allocated,
        "before": before,
        "after": before + allocated,
    }


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


class Writer:
    def __init__(self, batch: ImportBatch, actor):
        self.batch = batch
        self.actor = actor
        self.judge = Judge(batch)
        self.school_id = batch.school_id

    def remember(self, instance) -> None:
        ImportRecord.objects.create(
            batch=self.batch, model=instance._meta.label, object_id=instance.pk
        )

    def branch(self, values: dict):
        branch, why = self.judge.campus(values.get("campus", ""))
        if why:
            raise ValueError(why)
        return branch

    def student(self, values: dict) -> Student:
        number = normalise_admission_number(values.get("admission_number", ""))
        rows = Student.all_objects.filter(school_id=self.school_id, admission_number__iexact=number)
        if values.get("campus") or len(self.judge.branches) == 1:
            rows = rows.filter(branch=self.branch(values))
        return rows.get()

    def family(self, code: str, details: dict | None = None) -> Family:
        found = Family.all_objects.filter(school_id=self.school_id, family_code__iexact=code).first()
        if found is not None:
            return found
        details = details or {}
        created = Family.all_objects.create(
            school_id=self.school_id,
            family_code=details.get("family_id", code).strip() or code,
            name=(details.get("name") or f"Family {code}").strip()[:160],
            contact_name=(details.get("contact_name") or "").strip()[:160],
            contact_phone=parse_import_phone(details.get("phone", "")),
            contact_email=(details.get("email") or "").strip(),
        )
        self.remember(created)
        return created

    def term(self, branch_id: int, text: str) -> Term:
        verdict, why = self.judge.term(branch_id, text)
        if why:
            raise ValueError(why)
        year, sequence, found = verdict
        if found is not None:
            return found
        created = Term.all_objects.create(
            school_id=self.school_id, branch_id=branch_id, academic_year=year, sequence=sequence,
            name=f"{TermSequence(sequence).label} Term {year}".replace("Term Term", "Term"),
        )
        self.judge.terms[branch_id].append(created)
        self.remember(created)
        return created


def _write_family(writer: Writer, row: ImportRow) -> None:
    writer.family(row.raw["family_id"].strip(), row.raw)


def _write_student(writer: Writer, row: ImportRow) -> None:
    branch = writer.branch(row.raw)
    result = _validate_row(
        SourceRow(number=row.number, values=row.raw), branch, ClassIndex(branch), {}, {}
    )
    if result.errors:
        raise ValueError(result.errors[0].message)
    child = result.record
    child.save()
    writer.remember(child)
    _link(writer, child, row)


def _write_matched_student(writer: Writer, row: ImportRow) -> None:
    """A child already here can still be given a family. Nothing else on them changes."""
    _link(writer, writer.student(row.raw), row)


def _link(writer: Writer, child: Student, row: ImportRow) -> None:
    code = (row.raw.get("family_id") or "").strip()
    if not code:
        return
    if FamilyMembership.all_objects.filter(student=child, ended_at__isnull=True).exists():
        return
    writer.remember(link(writer.family(code), child, by=writer.actor))


def _write_link(writer: Writer, row: ImportRow) -> None:
    child = writer.student(row.raw)
    writer.remember(link(writer.family(row.raw["family_id"].strip()), child, by=writer.actor))


def _write_fee(writer: Writer, row: ImportRow) -> None:
    raw = row.raw
    child = writer.student(raw)
    term = writer.term(child.branch_id, raw["term"])
    if raw.get("school_class"):
        klass, why = ClassIndex(child.branch).resolve(raw["school_class"])
        if why:
            raise ValueError(why)
        enrolment, created = StudentTermEnrolment.all_objects.get_or_create(
            student=child, term=term,
            defaults={"school_id": child.school_id, "branch_id": child.branch_id, "school_class": klass},
        )
        if created:
            writer.remember(enrolment)
    adjustment = StudentFeeAdjustment.all_objects.create(
        school_id=child.school_id, branch_id=child.branch_id, student=child, term=term,
        kind=_KINDS[raw["kind"].strip().lower()], amount=parse_amount(raw["amount"]),
        description=raw["description"].strip()[:250], created_by=writer.actor,
    )
    writer.remember(adjustment)


def _confirmed_at(day):
    return timezone.make_aware(datetime.combine(day, clock(12)))


def _write_payment(writer: Writer, row: ImportRow, shares: list[ImportRow]) -> None:
    raw = row.raw
    legacy = raw["legacy_id"].strip()
    amount = parse_amount(raw["amount"])
    day = parse_date(raw["date_paid"])
    method = _METHODS[raw["method"].strip().lower()]
    status = (
        PaymentStatus.PENDING
        if (raw.get("status") or "").strip().lower() == "pending"
        else PaymentStatus.CONFIRMED
    )
    common = {
        "amount": amount, "date_paid": day, "method": method,
        "reference": (raw.get("reference") or "").strip(),
        "payer_name": (raw.get("payer_name") or "").strip()[:160],
        "status": status, "source": PaymentSource.IMPORT, "actor": writer.actor,
        "legacy_reference": legacy,
        "legacy_receipt_number": (raw.get("receipt_number") or "").strip()[:60],
        "confirmed_at": _confirmed_at(day) if status == PaymentStatus.CONFIRMED else None,
    }
    code = (raw.get("family_id") or "").strip()
    if code:
        family = writer.family(code)
        branch = writer.branch(raw) if (raw.get("campus") or len(writer.judge.branches) == 1) else (
            writer.student(shares[0].raw).branch
        )
        txn = allocation.receive(family=family, branch=branch, **common)
        for share in shares:
            child = writer.student(share.raw)
            label = _LABELS.get((share.raw.get("label") or "").strip().lower(), PaymentLabel.SCHOOL_FEES)
            allocation.allocate(
                txn, child, parse_amount(share.raw["amount"]), actor=writer.actor, label=label,
                allow_credit=True, term=writer.term(child.branch_id, share.raw["term"]),
            )
    else:
        values = raw if not shares else {**shares[0].raw}
        child = writer.student(values)
        term = writer.term(child.branch_id, values["term"])
        txn = allocation.receive_for_one_child(child, term, **common)
    writer.remember(txn)


def commit_batch(batch: ImportBatch, *, actor, budget: float = BUDGET_SECONDS) -> bool:
    """Write what is ready. True once nothing is left; call again if it returns False.

    With a negative budget, one row is written per call.
    """
    if not _claim(batch, (BatchStatus.DRAFT, BatchStatus.COMMITTING)):
        batch.refresh_from_db()
        return batch.status == BatchStatus.COMMITTED
    try:
        started = time.monotonic()
        judge(batch)
        if batch.rows.filter(status=RowStatus.REVIEW).exists():
            ImportBatch.all_objects.filter(pk=batch.pk, status=BatchStatus.COMMITTING).update(
                status=BatchStatus.DRAFT
            )
            return False
        ImportBatch.all_objects.filter(pk=batch.pk).update(status=BatchStatus.COMMITTING)
        writer = Writer(batch, actor)
        for sheet in KEYS:
            if sheet == "allocations":
                continue
            for row in _to_write(batch, sheet):
                _write_row(writer, batch, sheet, row)
                if time.monotonic() - started > budget and _anything_left(batch):
                    return False
        ImportBatch.all_objects.filter(pk=batch.pk).update(
            status=BatchStatus.COMMITTED, committed_by=actor, committed_at=timezone.now()
        )
        batch.refresh_from_db()
        record(
            action="importbatch.committed", category=Category.ACCESS,
            target_type="onboarding.ImportBatch", target_id=batch.pk, target_label=batch.filename,
            school_id=batch.school_id,
            changes={"rows": [None, batch.rows.filter(status=RowStatus.DONE).count()]},
        )
        return True
    finally:
        _release(batch)


def _to_write(batch, sheet) -> list[ImportRow]:
    rows = list(batch.rows.filter(sheet=sheet, status__in=[RowStatus.READY, RowStatus.MATCHED]))
    # A child already here only has something to write if the row gives them a family.
    return [
        row for row in rows
        if row.status == RowStatus.READY
        or (sheet == "students" and (row.raw.get("family_id") or "").strip())
    ]


def _anything_left(batch) -> bool:
    return any(_to_write(batch, sheet) for sheet in KEYS if sheet != "allocations")


def _write_row(writer, batch, sheet, row) -> None:
    shares = []
    if sheet == "payments":
        legacy = _folded(row.raw.get("legacy_id"))
        shares = [
            s for s in batch.rows.filter(sheet="allocations", status=RowStatus.READY)
            if _folded(s.raw.get("legacy_id")) == legacy
        ]
    try:
        with transaction.atomic():
            if sheet == "payments":
                _write_payment(writer, row, shares)
            elif sheet == "families":
                _write_family(writer, row)
            elif sheet == "links":
                _write_link(writer, row)
            elif sheet == "fees":
                _write_fee(writer, row)
            elif row.status == RowStatus.MATCHED:
                _write_matched_student(writer, row)
            else:
                _write_student(writer, row)
    except (ValueError, KeyError, IntegrityError, FamilyLinkError, allocation.AllocationError,
            Student.DoesNotExist, Student.MultipleObjectsReturned) as failure:
        row.status, row.messages = RowStatus.FAILED, [str(failure)[:300]]
        for share in shares:
            share.status, share.messages = RowStatus.FAILED, ["Its payment failed."]
    else:
        row.status, row.messages = RowStatus.DONE, []
        for share in shares:
            share.status, share.messages = RowStatus.DONE, []
    ImportRow.objects.bulk_update([row, *shares], ["status", "messages"])


def _claim(batch, statuses) -> bool:
    now = timezone.now()
    claimed = (
        ImportBatch.all_objects.filter(pk=batch.pk, status__in=statuses)
        .exclude(busy_until__gt=now)
        .update(busy_until=now + timezone.timedelta(seconds=BUSY_FOR_SECONDS))
    )
    return bool(claimed)


def _release(batch) -> None:
    ImportBatch.all_objects.filter(pk=batch.pk).update(busy_until=None)


# ---------------------------------------------------------------------------
# Taking it back
# ---------------------------------------------------------------------------


def undo_batch(batch: ImportBatch, *, actor, reason: str, budget: float = BUDGET_SECONDS) -> bool:
    """Void or remove what this batch made, newest first. True once nothing is left."""
    if not _claim(batch, (BatchStatus.COMMITTED, BatchStatus.UNDOING)):
        batch.refresh_from_db()
        return batch.status == BatchStatus.UNDONE
    try:
        started = time.monotonic()
        ImportBatch.all_objects.filter(pk=batch.pk).update(
            status=BatchStatus.UNDOING, undo_reason=reason[:250]
        )
        notes = list(batch.undo_notes)
        void_reason = f"Import undone: {reason}"[:250]
        for item in batch.records.filter(undone=False).order_by("-id"):
            with transaction.atomic():
                note = _undo_record(item, actor, void_reason)
                if note:
                    notes.append(note)
                item.undone = True
                item.save(update_fields=["undone"])
            if time.monotonic() - started > budget:
                ImportBatch.all_objects.filter(pk=batch.pk).update(undo_notes=notes)
                if batch.records.filter(undone=False).exists():
                    return False
        ImportBatch.all_objects.filter(pk=batch.pk).update(
            status=BatchStatus.UNDONE, undone_by=actor, undone_at=timezone.now(), undo_notes=notes
        )
        batch.refresh_from_db()
        record(
            action="importbatch.undone", category=Category.ACCESS,
            target_type="onboarding.ImportBatch", target_id=batch.pk, target_label=batch.filename,
            school_id=batch.school_id, changes={"reason": [None, reason[:250]]},
        )
        return True
    finally:
        _release(batch)


def _undo_record(item: ImportRecord, actor, reason: str) -> str:
    if item.model == "payments.PaymentTransaction":
        txn = PaymentTransaction.all_objects.filter(pk=item.object_id).first()
        if txn is None or txn.status == PaymentStatus.VOID:
            return ""
        if txn.is_shared:
            allocation.void_transaction(txn, actor=actor, reason=reason)
        else:
            for payment in txn.shares().exclude(status=PaymentStatus.VOID):
                payment.void(by=actor, reason=reason)
        return ""
    if item.model == "fees.StudentFeeAdjustment":
        adjustment = StudentFeeAdjustment.all_objects.filter(pk=item.object_id).first()
        if adjustment is not None and not adjustment.is_void:
            adjustment.void(by=actor, reason=reason)
        return ""
    if item.model == "families.FamilyMembership":
        membership = FamilyMembership.all_objects.filter(pk=item.object_id).first()
        if membership is not None:
            unlink(membership, by=actor)
        return ""
    if item.model == "fees.StudentTermEnrolment":
        StudentTermEnrolment.all_objects.filter(pk=item.object_id).delete()
        return ""
    if item.model == "students.Student":
        return _undo_student(item, actor)
    if item.model == "families.Family":
        family = Family.all_objects.filter(pk=item.object_id).first()
        if family is not None and not family.memberships.filter(ended_at__isnull=True).exists():
            _try_delete(family)
        return ""
    if item.model == "fees.Term":
        term = Term.all_objects.filter(pk=item.object_id, is_current=False).first()
        if term is not None:
            _try_delete(term)
        return ""
    return ""


def _try_delete(instance) -> bool:
    try:
        with transaction.atomic():
            instance.delete()
        return True
    except (ProtectedError, RestrictedError):
        return False


def _undo_student(item: ImportRecord, actor) -> str:
    """Remove a child nothing else points at. Keep any with money or history of their own."""
    child = Student.all_objects.filter(pk=item.object_id).first()
    if child is None:
        return ""
    imported = set(
        ImportRecord.objects.filter(batch=item.batch, model="payments.PaymentTransaction")
        .values_list("object_id", flat=True)
    )
    others = Payment.all_objects.filter(student=child).exclude(transaction_id__in=imported)
    if others.exists():
        return f"{child.full_name} ({child.admission_number}) has other payments, so stayed on the roll."
    child_has_rows = (
        Payment.all_objects.filter(student=child).exists()
        or StudentFeeAdjustment.all_objects.filter(student=child).exists()
    )
    if not child_has_rows and _try_delete(child):
        return ""
    child.status = StudentStatus.WITHDRAWN
    child.save(update_fields=["status", "updated_at"])
    return f"{child.full_name} ({child.admission_number}) was marked withdrawn, to keep their voided records."

