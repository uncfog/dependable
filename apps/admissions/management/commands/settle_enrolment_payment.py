"""Split an enrolled student's admission money the way enrolment now does.

Students enrolled before :mod:`apps.admissions.split` existed had their whole
all-in payment recorded as admission money, so their term still shows as owed.
This applies :func:`~apps.admissions.split.settle_at_enrolment` to one of them
after the fact, and prints the figures before and after.

**Dry run by default.** The split is worked out inside a transaction, the
"after" figures are printed, and the transaction is rolled back. Nothing is
written until ``--commit`` is passed.

The price lists have to be the fixed ones first: an intake sheet that still
carries tuition, books or development would hand the term's money back to the
intake side. The command refuses in that case and names the seeder to run.

    manage.py settle_enrolment_payment FA/2026/003 --school "Fulfilled Academy"
    manage.py settle_enrolment_payment FA/2026/003 --school "Fulfilled Academy" --commit
"""

from __future__ import annotations

from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.admissions.models import AdmissionPayment, Applicant
from apps.admissions.split import bill_for, settle_at_enrolment
from apps.payments.models import Payment
from apps.students import fees
from apps.students.models import Student

#: Line names that recur every term. Any of them on an intake sheet means the
#: sheet predates the price-list cleanup.
RECURRING = {"tuition", "school fees", "textbooks", "compulsory books",
             "development", "development levy"}


class Command(BaseCommand):
    help = (
        "Move the term share of an enrolled student's admission payment onto "
        "their term balance. Dry run unless --commit."
    )

    def add_arguments(self, parser):
        parser.add_argument("admission_number")
        parser.add_argument("--school", required=True, help="The school's name.")
        parser.add_argument(
            "--commit", action="store_true", help="Write the split. Without "
            "this the command only reports what it would do."
        )

    def handle(self, *args, **options):
        student = self._student(options["admission_number"], options["school"])
        applicant = Applicant.all_objects.filter(student=student).first()
        if applicant is None:
            raise CommandError(
                f"{student} was not enrolled through admissions, so there is no "
                f"admission payment to split."
            )
        self._check_prices(student)

        self.stdout.write(f"{student.full_name} ({student.admission_number}), "
                          f"{student.school_class.display_name}, "
                          f"applicant {applicant.reference}\n")
        self.stdout.write("Before:")
        self._report(student, applicant)

        with transaction.atomic():
            result = settle_at_enrolment(applicant, student)
            self.stdout.write("\nAfter:")
            self._report(student, applicant)
            if result is None or result.term_payment is None:
                self.stdout.write(self.style.WARNING(
                    "\nNothing to move: no confirmed, unsplit admission money "
                    "with a term share."
                ))
            if not options["commit"]:
                transaction.set_rollback(True)
                self.stdout.write(self.style.WARNING(
                    "\nDry run -- nothing was written. Re-run with --commit."
                ))
                return

        self.stdout.write(self.style.SUCCESS(
            f"\nWritten. Term payment {result.receipt_number} on "
            f"{student.admission_number}."
            if result and result.term_payment else "\nNothing to write."
        ))

    # -- helpers -------------------------------------------------------------

    def _student(self, number: str, school: str) -> Student:
        matches = list(
            Student.all_objects.filter(
                admission_number__iexact=number.strip(), school__name=school
            ).select_related("school_class", "branch")
        )
        if not matches:
            raise CommandError(f"No student {number} at {school}.")
        if len(matches) > 1:
            raise CommandError(
                f"{number} is used on more than one campus of {school}: "
                + ", ".join(s.branch.name for s in matches)
            )
        return matches[0]

    def _check_prices(self, student) -> None:
        bill = bill_for(student.school_class)
        stale = sorted(
            line.name
            for line in bill.admission_lines + bill.one_off_lines
            if line.name.lower().strip() in RECURRING
        )
        if stale:
            raise CommandError(
                f"{student.school_class.display_name}'s intake sheet still "
                f"charges {', '.join(stale)}, which the term structure bills. "
                f"Run `manage.py seed_admission_fees --school "
                f"\"{student.school.name}\" --branch \"{student.branch.name}\"` "
                f"first so the split is worked out against the fixed prices."
            )

    def _report(self, student, applicant) -> None:
        position = fees.position_for(student)
        bill = bill_for(student.school_class)
        rows = AdmissionPayment.all_objects.filter(applicant=applicant)
        intake_paid = sum(
            (r.amount for r in rows if r.status == "confirmed"), Decimal("0")
        )
        term_payments = Payment.all_objects.filter(student=student)
        self.stdout.write(
            f"  term {position.term.name if position.term else '(none)'}: "
            f"expected {position.expected:,.0f}, paid {position.paid:,.0f}, "
            f"outstanding {position.outstanding:,.0f}"
        )
        self.stdout.write(
            f"  intake sheet {bill.intake_total:,.0f}: paid {intake_paid:,.0f}, "
            f"outstanding {max(bill.intake_total - intake_paid, Decimal('0')):,.0f}"
        )
        for row in rows:
            self.stdout.write(
                f"    admission payment {row.receipt_number}: {row.amount:,.0f} "
                f"{row.status} {row.received_on}"
            )
        for payment in term_payments:
            self.stdout.write(
                f"    term payment {payment.receipt_number}: {payment.amount:,.0f} "
                f"{payment.status} {payment.date_paid}"
            )
