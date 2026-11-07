"""A school's existing records on their way in, and a record of what they became.

Staged in tables rather than the session because review can take days and span six
sheets. Rows hold personal data, so ``purge_import_files`` empties them once an import
has been finished for a while. What was imported stays, with its link to the batch.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.urls import reverse

from apps.core.models import TimeStampedModel
from apps.core.tenancy import TenantManager, TenantQuerySet, UnscopedManager


class SchoolWideQuerySet(TenantQuerySet):
    tenant_school_field = "school_id"
    tenant_branch_field = None


class SchoolWideManager(TenantManager.from_queryset(SchoolWideQuerySet)):
    """Auto-scoped to the caller's school."""


class SchoolWideUnscopedManager(UnscopedManager.from_queryset(SchoolWideQuerySet)):
    """Every row, regardless of caller."""


class BatchStatus(models.TextChoices):
    DRAFT = "draft", "Waiting for review"
    COMMITTING = "committing", "Importing"
    COMMITTED = "committed", "Imported"
    UNDOING = "undoing", "Undoing"
    UNDONE = "undone", "Undone"


def batch_upload_to(instance: "ImportBatch", filename: str) -> str:
    return f"imports/{instance.school_id}/{instance.file_hash}.xlsx"


class ImportBatch(TimeStampedModel):
    school = models.ForeignKey(
        "schools.School", on_delete=models.CASCADE, related_name="import_batches"
    )
    status = models.CharField(
        max_length=20, choices=BatchStatus.choices, default=BatchStatus.DRAFT
    )
    filename = models.CharField(max_length=200)
    file = models.FileField(upload_to=batch_upload_to, blank=True)
    file_hash = models.CharField(max_length=64, db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="import_batches_created",
    )
    committed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="import_batches_committed",
    )
    committed_at = models.DateTimeField(null=True, blank=True)
    undone_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="import_batches_undone",
    )
    undone_at = models.DateTimeField(null=True, blank=True)
    undo_reason = models.CharField(max_length=250, blank=True)
    #: Held by whichever request is writing, so a second press can't write it twice.
    busy_until = models.DateTimeField(null=True, blank=True)
    #: Reasons a child stayed on the roll after an undo, for the person who pressed it.
    undo_notes = models.JSONField(default=list, blank=True)

    objects = SchoolWideManager()
    all_objects = SchoolWideUnscopedManager()

    class Meta:
        ordering = ["-created_at", "-id"]
        base_manager_name = "all_objects"
        default_manager_name = "objects"
        verbose_name_plural = "import batches"

    def __str__(self) -> str:
        return self.filename

    def get_absolute_url(self) -> str:
        return reverse("onboarding:detail", args=[self.pk])


class RowStatus(models.TextChoices):
    READY = "ready", "Will be imported"
    MATCHED = "matched", "Already here"
    REVIEW = "review", "Needs a decision"
    SKIP = "skip", "Left out"
    PROBLEM = "problem", "Can't be imported"
    DONE = "done", "Imported"
    FAILED = "failed", "Failed"


class ImportRow(models.Model):
    """One row of one sheet. Always reached through its batch, which carries the school."""

    batch = models.ForeignKey(ImportBatch, on_delete=models.CASCADE, related_name="rows")
    sheet = models.CharField(max_length=20)
    number = models.PositiveIntegerField()
    raw = models.JSONField(default=dict, blank=True)
    label = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=20, choices=RowStatus.choices, default=RowStatus.READY)
    messages = models.JSONField(default=list, blank=True)
    #: The answers a reviewer may give, and the question's identity across uploads.
    options = models.JSONField(default=list, blank=True)
    signature = models.CharField(max_length=200, blank=True)
    decision = models.CharField(max_length=20, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )

    class Meta:
        ordering = ["id"]
        indexes = [models.Index(fields=["batch", "sheet", "status"])]

    def __str__(self) -> str:
        return f"{self.sheet} row {self.number}"


class ImportRecord(models.Model):
    """Something an import created, so it can be taken back."""

    batch = models.ForeignKey(ImportBatch, on_delete=models.CASCADE, related_name="records")
    model = models.CharField(max_length=60)
    object_id = models.PositiveBigIntegerField()
    undone = models.BooleanField(default=False)

    class Meta:
        ordering = ["id"]
        indexes = [models.Index(fields=["batch", "undone"])]

    def __str__(self) -> str:
        return f"{self.model} {self.object_id}"


class MatchDecision(TimeStampedModel):
    """An answer to "are these the same?", so nobody is asked it twice."""

    school = models.ForeignKey(
        "schools.School", on_delete=models.CASCADE, related_name="match_decisions"
    )
    signature = models.CharField(max_length=200)
    decision = models.CharField(max_length=20)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )

    objects = SchoolWideManager()
    all_objects = SchoolWideUnscopedManager()

    class Meta:
        base_manager_name = "all_objects"
        default_manager_name = "objects"
        constraints = [
            models.UniqueConstraint(
                fields=["school", "signature"], name="one_decision_per_question"
            )
        ]

    def __str__(self) -> str:
        return f"{self.signature}: {self.decision}"
