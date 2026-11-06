"""List past payments that may have been credited to the wrong child.

Two bugs in the student picker could do that, both now fixed, and neither left a
mark on the row. This cannot say a payment *was* wrong. It says which ones to check
against the slip or the school's own records:

``shared_admission_number``
    The child's number is shared with another child, and someone who could see
    both recorded or confirmed the payment. The old picker took the first match.

``numeric_number_read_as_id``
    The child's database id equals another child's all-digit admission number. A
    refused form used to swap the typed child for this one, and pressing the button
    again would credit them. Most of these will be coincidence, so treat them as
    the lower priority.

Read-only: it runs no writes. Correct a wrong payment by voiding it and recording
it against the right child. Prints CSV with the identifiers needed to look a
payment up and no family contact details; treat the output as confidential anyway.

    python manage.py report_wrong_child_risk > at-risk.csv
    python manage.py report_wrong_child_risk --school fulfilled-academy
"""

from __future__ import annotations

import csv
from collections import Counter, defaultdict

from django.core.management.base import BaseCommand

from apps.core.roles import Scope, scope_for
from apps.payments.models import Payment, PaymentStatus
from apps.students.models import Student

HEADINGS = (
    "risk", "receipt", "payment_id", "school", "campus", "admission_number",
    "student_id", "term", "amount", "date_paid", "status", "recorded_by",
    "confirmed_by", "also_matches_student_ids",
)


class Command(BaseCommand):
    help = "List past payments that may have been credited to the wrong child. Read-only."

    def add_arguments(self, parser):
        parser.add_argument("--school", help="Only payments at the school with this slug.")

    def handle(self, *args, **options):
        everyone = list(
            Student.all_objects.values_list("pk", "school_id", "admission_number")
        )
        by_number_in_school = defaultdict(list)
        by_number_anywhere = defaultdict(list)
        for pk, school_id, number in everyone:
            by_number_in_school[school_id, number.upper()].append(pk)
            by_number_anywhere[number.upper()].append(pk)
        numeric_owner = defaultdict(list)
        for pk, school_id, number in everyone:
            if number.isdigit():
                numeric_owner[school_id, int(number)].append(pk)

        payments = (
            Payment.all_objects.exclude(status=PaymentStatus.VOID)
            .select_related("student", "school", "branch", "term", "recorded_by", "confirmed_by")
            .order_by("pk")
        )
        if options.get("school"):
            payments = payments.filter(school__slug=options["school"])

        writer = csv.writer(self.stdout)
        writer.writerow(HEADINGS)
        found = Counter()
        for payment in payments.iterator():
            student = payment.student
            number = student.admission_number.upper()
            people = (payment.recorded_by, payment.confirmed_by)

            others = set()
            if _any_scope(people, Scope.PLATFORM):
                others = set(by_number_anywhere[number])
            elif _any_scope(people, Scope.SCHOOL):
                others = set(by_number_in_school[payment.school_id, number])
            others.discard(student.pk)
            if others:
                found["shared_admission_number"] += 1
                writer.writerow(_row("shared_admission_number", payment, others))

            typed = set(numeric_owner[payment.school_id, student.pk]) - {student.pk}
            if typed:
                found["numeric_number_read_as_id"] += 1
                writer.writerow(_row("numeric_number_read_as_id", payment, typed))

        self.stderr.write(
            f"{found['shared_admission_number']} shared-number and "
            f"{found['numeric_number_read_as_id']} numeric-number rows to check."
        )


def _any_scope(people, scope: Scope) -> bool:
    """Whether anyone here could see at least that widely when they acted."""
    for person in people:
        if person is None:
            continue
        if scope is Scope.PLATFORM and person.is_superuser:
            return True
        if scope_for(person.role) is scope:
            return True
    return False


def _row(risk: str, payment: Payment, others) -> list:
    student = payment.student
    return [
        risk,
        payment.receipt_number,
        payment.pk,
        payment.school.name,
        payment.branch.name,
        student.admission_number,
        student.pk,
        payment.term.name,
        payment.amount,
        payment.date_paid.isoformat(),
        payment.status,
        payment.recorded_by.username if payment.recorded_by else "",
        payment.confirmed_by.username if payment.confirmed_by else "",
        " ".join(str(pk) for pk in sorted(others)),
    ]
