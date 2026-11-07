"""Prove the money still reconciles. Read-only, and safe to run at any time.

Checks that every payment has a transaction, that a one-child transaction says what
its payment says, and that no shared transaction has handed out more than it received.
Exits with 1 if anything is wrong, so it can gate a deployment.

    python manage.py check_payment_transactions
"""

from __future__ import annotations

import sys

from django.core.management.base import BaseCommand
from django.db.models import Count, Q, Sum

from apps.payments.models import Payment, PaymentStatus, PaymentTransaction, TransactionKind


class Command(BaseCommand):
    help = "Check that payments and their transactions agree. Read-only."

    def handle(self, *args, **options):
        problems = []

        for payment in Payment.all_objects.filter(transaction__isnull=True).order_by("pk"):
            problems.append(f"{payment.receipt_number} has no transaction.")

        not_void = ~Q(allocations__status=PaymentStatus.VOID)
        rows = PaymentTransaction.all_objects.annotate(
            used=Sum("allocations__amount", filter=not_void),
            shares=Count("allocations"),
        ).order_by("pk")
        for txn in rows.iterator():
            used = txn.used or 0
            if txn.kind == TransactionKind.INDIVIDUAL:
                if txn.shares != 1:
                    problems.append(f"{txn.number} is for one child but has {txn.shares} rows.")
                elif txn.status != PaymentStatus.VOID and used != txn.amount:
                    problems.append(
                        f"{txn.number} says {txn.amount} but its payment says {used}."
                    )
            elif txn.status != PaymentStatus.VOID and used > txn.amount:
                problems.append(f"{txn.number} received {txn.amount} but allocated {used}.")

        if problems:
            for problem in problems:
                self.stderr.write(problem)
            self.stderr.write(f"{len(problems)} problem(s) found.")
            sys.exit(1)
        self.stdout.write("Payments and transactions agree.")
