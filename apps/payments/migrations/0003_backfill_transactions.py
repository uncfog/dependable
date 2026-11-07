"""Give every existing payment its own one-child transaction.

Only adds rows and sets the new link. No amount, status, date or receipt number on any
payment changes, so no balance can move. It checks its own work, and if the totals do
not match it raises and the whole migration rolls back. Reversing clears the link and
removes the rows it could have made, which leaves payments exactly as they were.
"""

from django.db import migrations
from django.db.models import Sum

BATCH = 500
FIELDS = (
    "school_id", "branch_id", "amount", "date_paid", "method", "reference", "source",
    "status", "note", "recorded_by_id", "confirmed_by_id", "confirmed_at", "voided_by_id",
    "voided_at", "void_reason",
)


def backfill(apps, schema_editor):
    Payment = apps.get_model("payments", "Payment")
    Transaction = apps.get_model("payments", "PaymentTransaction")
    manager = Payment._base_manager

    while True:
        batch = list(manager.filter(transaction__isnull=True).order_by("pk")[:BATCH])
        if not batch:
            break
        created = Transaction._base_manager.bulk_create(
            [
                Transaction(kind="individual", **{f: getattr(p, f) for f in FIELDS})
                for p in batch
            ]
        )
        for payment, txn in zip(batch, created):
            payment.transaction_id = txn.pk
            # bulk_create stamps "now". The money came in when the payment was made.
            txn.created_at = payment.created_at
            txn.updated_at = payment.updated_at
        manager.bulk_update(batch, ["transaction"])
        Transaction._base_manager.bulk_update(created, ["created_at", "updated_at"])

    _check(manager, Transaction)


def _check(payments, Transaction):
    missing = payments.filter(transaction__isnull=True).count()
    paid = payments.aggregate(total=Sum("amount"))["total"] or 0
    received = Transaction._base_manager.aggregate(total=Sum("amount"))["total"] or 0
    if missing or paid != received:
        raise RuntimeError(
            f"Backfill does not reconcile: {missing} payments without a transaction, "
            f"payments {paid}, transactions {received}. Nothing has been changed."
        )


def undo(apps, schema_editor):
    Payment = apps.get_model("payments", "Payment")
    Transaction = apps.get_model("payments", "PaymentTransaction")
    Payment._base_manager.update(transaction=None)
    Transaction._base_manager.filter(kind="individual").delete()


class Migration(migrations.Migration):
    dependencies = [("payments", "0002_transactions")]

    operations = [migrations.RunPython(backfill, undo)]
