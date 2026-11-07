"""The six sheets of the onboarding workbook. Any of them can be left out."""

from __future__ import annotations

from apps.core.imports import Column, Sheet
from apps.students.importer import COLUMNS as STUDENT_COLUMNS

MAX_ROWS = 5000
CAMPUS = Column(
    "campus", "Campus", False, "Main Campus — can be left blank at a one-campus school",
    ("branch",), width=20,
)
FAMILY_ID = Column("family_id", "Family ID", False, "F-001", ("family", "family code"), width=14)
ADMISSION = Column(
    "admission_number", "Admission Number", True, "FA/2025/001",
    ("admission no", "reg number"), width=18,
)
TERM = Column(
    "term", "Term", True, "First Term 2025/2026", ("session term",), width=24,
)


def _sheet(name, noun, plural, columns, label_keys, guidance) -> Sheet:
    return Sheet(
        name=name, noun=noun, noun_plural=plural, filename="schoolcord-onboarding.xlsx",
        columns=columns, label_keys=label_keys, guidance_column=guidance, max_rows=MAX_ROWS,
        header_match_threshold=2,
    )


FAMILIES = _sheet(
    "Families", "family", "families",
    (
        Column("family_id", "Family ID", True, "F-001", ("family", "family code"), width=14),
        Column("name", "Family Name", True, "The Okonkwo family", ("name",), width=26),
        Column("contact_name", "Primary Contact", False, "Mrs. Ngozi Okonkwo", width=24),
        Column("phone", "Phone", False, "08034129876", ("contact phone",), width=16),
        Column("email", "Email", False, "ngozi@example.com", ("contact email",), width=24),
    ),
    ("family_id", "name"), "family_id",
)
STUDENTS = _sheet(
    "Students", "student", "students",
    (CAMPUS, *STUDENT_COLUMNS, FAMILY_ID),
    ("admission_number", "first_name", "last_name"), "admission_number",
)
LINKS = _sheet(
    "Family Links", "family link", "family links",
    (
        Column("family_id", "Family ID", True, "F-001", ("family", "family code"), width=14),
        CAMPUS, ADMISSION,
    ),
    ("family_id", "admission_number"), "family_id",
)
FEES = _sheet(
    "Fee Records", "fee record", "fee records",
    (
        ADMISSION, CAMPUS, TERM,
        Column("school_class", "Class That Term", False, "JSS 1A", ("class",), width=16),
        Column("kind", "Type", True, "Opening balance, Extra charge, Discount, Scholarship or Waiver",
               ("fee type",), width=24),
        Column("amount", "Amount", True, "15000", width=14),
        Column("description", "Description", True, "Owed from 2024/2025", ("reason",), width=30),
    ),
    ("admission_number", "term", "kind"), "admission_number",
)
PAYMENTS = _sheet(
    "Payment Records", "payment", "payments",
    (
        Column("legacy_id", "Transaction ID", True, "OLD-1001 — your own number, unique per payment",
               ("legacy transaction id", "legacy id"), width=20),
        Column("date_paid", "Date Paid", True, "2025-10-03 (YYYY-MM-DD)", ("date",), width=16),
        Column("amount", "Amount", True, "50000", width=14),
        Column("method", "Method", True, "Cash, Bank transfer, POS or Other", width=18),
        Column("reference", "Reference", False, "Teller or transfer reference", width=20),
        Column("payer_name", "Paid By", False, "Mrs. Ngozi Okonkwo", ("payer",), width=22),
        FAMILY_ID, CAMPUS,
        Column("admission_number", "Admission Number", False,
               "Only if the whole payment is for one child", ("admission no",), width=18),
        Column("term", "Term", False, "First Term 2025/2026 — needed with an admission number",
               width=24),
        Column("receipt_number", "Receipt Number", False, "Your old receipt number", width=18),
        Column("status", "Status", False, "Confirmed or Pending", width=12),
    ),
    ("legacy_id",), "legacy_id",
)
ALLOCATIONS = _sheet(
    "Payment Allocations", "allocation", "allocations",
    (
        Column("legacy_id", "Transaction ID", True, "OLD-1001 — from Payment Records",
               ("legacy transaction id", "legacy id"), width=20),
        ADMISSION, CAMPUS, TERM,
        Column("amount", "Amount", True, "25000", width=14),
        Column("label", "For", False, "School fees", width=16),
    ),
    ("legacy_id", "admission_number"), "legacy_id",
)

ALL_SHEETS = (FAMILIES, STUDENTS, LINKS, FEES, PAYMENTS, ALLOCATIONS)
BY_NAME = {sheet.name: sheet for sheet in ALL_SHEETS}
#: The order rows are judged and written in, because each can lean on those before it.
KEYS = {
    "families": FAMILIES, "students": STUDENTS, "links": LINKS,
    "fees": FEES, "payments": PAYMENTS, "allocations": ALLOCATIONS,
}
