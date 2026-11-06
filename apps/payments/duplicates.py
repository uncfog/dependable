"""Telling one press of Record, sent twice, from two payments.

On a slow connection a person presses again, or the browser resends. The test is
the shape of the payment: the same person, the same child and the same details
within two minutes, which is far longer than a resend takes and rarer than a real
second payment. Voided rows do not count, because re-entering a voided payment is
how a mistake gets corrected.

The caller holds a row lock across the check and the write. Without it, two copies
arriving together on two workers would both find nothing and both write. The lock
is on the child or applicant the money is for, so nobody else's payment waits.

A fingerprint because there is no column to hold a submission key yet. The planned
``PaymentTransaction`` carries a real one and this can then go.
"""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

WINDOW = timedelta(minutes=2)


def lock_row(model, pk) -> None:
    """Hold ``model``'s row ``pk`` until the surrounding transaction ends."""
    # order_by() clears the default ordering: Student's joins other tables, and
    # the lock should not reach them.
    list(
        model.all_objects.select_for_update().filter(pk=pk).order_by().values_list("pk")
    )


def recent_twin(queryset, *, recorded_by):
    """The newest non-void row in ``queryset`` this person just entered, or None.

    ``queryset`` is already narrowed to the same money; this adds who and when.
    """
    return (
        queryset.filter(
            recorded_by=recorded_by, created_at__gte=timezone.now() - WINDOW
        )
        .exclude(status="void")
        .order_by("-pk")
        .first()
    )
