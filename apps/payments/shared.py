"""Reading family payments: which are waiting, and how much is not yet shared."""

from __future__ import annotations

from decimal import Decimal

from django.db.models import DecimalField, F, Q, Sum, Value
from django.db.models.functions import Coalesce

from .models import PaymentStatus, PaymentTransaction, TransactionKind

ZERO = Decimal("0")
_MONEY = DecimalField(max_digits=12, decimal_places=2)


def transactions():
    """Family payments the caller can see, each with what is left of it as ``left``.

    The join is on the table rather than through a scoped manager, so ``left`` counts
    shares at every campus, as the ceiling does.
    """
    used = Sum(
        "allocations__amount",
        filter=Q(allocations__status__in=[PaymentStatus.PENDING, PaymentStatus.CONFIRMED]),
    )
    return (
        PaymentTransaction.objects.filter(kind=TransactionKind.FAMILY)
        .select_related("family", "branch")
        .annotate(used=Coalesce(used, Value(ZERO), output_field=_MONEY))
        .annotate(left=F("amount") - F("used"))
    )


def waiting(queryset=None):
    """What somebody still has to act on: money left to share, or money to check."""
    queryset = transactions() if queryset is None else queryset
    return queryset.exclude(status=PaymentStatus.VOID).filter(
        Q(left__gt=0) | Q(status=PaymentStatus.PENDING)
    )


def unallocated_total(queryset=None) -> Decimal:
    """Confirmed money not yet shared. Pending money has not been accepted yet."""
    queryset = transactions() if queryset is None else queryset
    lefts = queryset.filter(status=PaymentStatus.CONFIRMED).values_list("left", flat=True)
    return sum(lefts, ZERO)
