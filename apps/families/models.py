"""Children grouped into a household, so one payment can cover several of them.

A family groups records and owns nothing. Every child keeps their own student record,
fee ledger and payment history, and nothing here changes those. So a family can be
corrected at any time without touching a kobo: leaving one ends a membership, and the
child's money stays exactly where it is.

A person always makes the link. Nothing here matches children up by surname or
guardian name, because two unrelated families can share both, and a wrong link shows
one family's children and balances beside another's.
"""

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.db.models.functions import Upper
from django.urls import reverse

from apps.core.models import BranchScopedModel, TimeStampedModel
from apps.core.tenancy import TenantManager, TenantQuerySet, UnscopedManager


class FamilyQuerySet(TenantQuerySet):
    # Belongs to a school and to no campus, because siblings can be at different
    # campuses. Which children a member of staff sees is decided by the membership.
    tenant_school_field = "school_id"
    tenant_branch_field = None


class FamilyManager(TenantManager.from_queryset(FamilyQuerySet)):
    """Auto-scoped to the caller's school."""


class FamilyUnscopedManager(UnscopedManager.from_queryset(FamilyQuerySet)):
    """Every family, regardless of caller."""


class Family(TimeStampedModel):
    # Not TenantScopedModel: its save() stamps a blank branch with the creator's,
    # which would hide a school-wide family from every other campus.
    school = models.ForeignKey(
        "schools.School", on_delete=models.CASCADE, related_name="families"
    )
    name = models.CharField(max_length=160, help_text='e.g. "The Okonkwo family".')
    family_code = models.CharField(
        "family ID",
        max_length=40,
        blank=True,
        help_text="Optional. The school's own reference, if it has one.",
    )
    contact_name = models.CharField(max_length=160, blank=True)
    contact_phone = models.CharField(max_length=20, blank=True)
    contact_email = models.EmailField(blank=True)
    note = models.CharField(max_length=250, blank=True)

    objects = FamilyManager()
    all_objects = FamilyUnscopedManager()

    class Meta:
        ordering = ["name", "id"]
        base_manager_name = "all_objects"
        default_manager_name = "objects"
        verbose_name_plural = "families"
        constraints = [
            models.UniqueConstraint(
                Upper("family_code"),
                "school",
                condition=~Q(family_code=""),
                name="unique_family_code_per_school",
                violation_error_message="Another family already has that family ID.",
            )
        ]
        indexes = [models.Index(fields=["school", "name"])]

    def __str__(self) -> str:
        return self.name

    def get_absolute_url(self) -> str:
        return reverse("families:detail", args=[self.pk])


class FamilyMembership(BranchScopedModel):
    """A child's place in a family, for as long as it lasts.

    A row rather than a column on the student, so the busiest table is left alone and
    a link that was wrong, or a family that changed, leaves its history behind. Carries
    the child's campus, so campus staff only see memberships of their own children.
    """

    branch = models.ForeignKey(
        "schools.Branch", on_delete=models.CASCADE, related_name="family_memberships"
    )
    family = models.ForeignKey(Family, on_delete=models.CASCADE, related_name="memberships")
    student = models.ForeignKey(
        "students.Student", on_delete=models.CASCADE, related_name="family_memberships"
    )
    linked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="family_links_made",
    )
    ended_at = models.DateTimeField(null=True, blank=True)
    ended_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="family_links_ended",
    )

    class Meta(BranchScopedModel.Meta):
        abstract = False
        ordering = ["student__last_name", "student__first_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["student"],
                condition=Q(ended_at__isnull=True),
                name="one_active_family_per_student",
            )
        ]
        indexes = [
            models.Index(fields=["school", "branch"]),
            models.Index(fields=["family", "ended_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.student_id} in {self.family_id}"

    @property
    def is_active(self) -> bool:
        return self.ended_at is None

    def save(self, *args, **kwargs):
        if self.student_id and self.branch_id is None:
            self.branch_id = self.student.branch_id
        super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        if self.family_id and self.student_id:
            if self.family.school_id != self.student.school_id:
                raise ValidationError("That child is at a different school.")
