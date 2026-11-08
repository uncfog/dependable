"""Confirming a plan payment: the one place a school's plan changes.

Used by the platform owner's Plans & billing screen and by the Django admin action,
so both do exactly the same thing.
"""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from .models import PlanOrder, SchoolStatus

OPEN_STATUSES = (
    PlanOrder.Status.AWAITING_TRANSFER,
    PlanOrder.Status.TRANSFER_REPORTED,
)


def confirm_order(order: PlanOrder) -> bool:
    """Mark the transfer received and move the school onto the plan it paid for.

    Returns ``False``, and changes nothing, for an order already paid or cancelled.
    """
    with transaction.atomic():
        order = PlanOrder.all_objects.select_for_update().select_related("school").get(
            pk=order.pk
        )
        if order.status not in OPEN_STATUSES:
            return False
        school = order.school
        school.plan = order.plan
        school.status = SchoolStatus.ACTIVE
        school.save(update_fields=["plan", "status", "updated_at"])
        order.status = PlanOrder.Status.PAID
        order.paid_at = timezone.now()
        order.save(update_fields=["status", "paid_at", "updated_at"])
    return True
