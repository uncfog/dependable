"""Making and ending the links between children and families."""

from __future__ import annotations

from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import FamilyMembership


class FamilyLinkError(Exception):
    """A link that cannot be made. The message is written for the person making it."""


def link(family, student, *, by=None) -> FamilyMembership:
    if family.school_id != student.school_id:
        raise FamilyLinkError("That child is at a different school.")
    current = (
        FamilyMembership.all_objects.filter(student=student, ended_at__isnull=True)
        .select_related("family")
        .first()
    )
    if current is not None:
        raise FamilyLinkError(
            f"{student.full_name} is already in {current.family.name}. Remove them "
            f"from that family first."
        )
    try:
        # A savepoint, so losing a race with another link is an answer and not a
        # broken transaction for whoever called this.
        with transaction.atomic():
            return FamilyMembership.all_objects.create(
                school_id=student.school_id,
                family=family,
                student=student,
                linked_by=by,
            )
    except IntegrityError:
        raise FamilyLinkError(f"{student.full_name} has just been added to a family.")


def unlink(membership: FamilyMembership, *, by=None) -> FamilyMembership:
    """End a membership. No money moves: allocations belong to the child."""
    if membership.is_active:
        membership.ended_at = timezone.now()
        membership.ended_by = by
        membership.save(update_fields=["ended_at", "ended_by", "updated_at"])
    return membership
