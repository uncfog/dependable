"""An append-only record of changes to money and to who may do what.

The screens already store who recorded and voided a payment, but not what a payment
said before it was corrected. A wrong figure that has been overwritten leaves no
trace, and that is the question a school asks when money does not add up. So every
change is written here, with both values, and is never edited.

Append-only is enforced in the application. There is deliberately no database
trigger: closing a school takes its events with it (see ``apps.schools.teardown``),
and someone with direct database access is outside what this can defend against.

Targets are a type and an id rather than foreign keys, so the record of a deleted
row is not deleted with it.
"""

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import models

from apps.core.tenancy import TenantManager, TenantQuerySet, UnscopedManager


class Category(models.TextChoices):
    MONEY = "money", "Money"
    ACCESS = "access", "Access"


#: What the database itself does when a person or a campus is removed. The event
#: stays and the link goes. Nothing else may be updated.
_SET_NULL_ON_DELETE = frozenset({"actor", "branch"})

_NOT_REWRITABLE = "The audit log is append-only."

#: How an action reads on screen.
ACTION_LABELS = {
    "payment.created": "Recorded",
    "payment.changed": "Corrected",
    "payment.confirmed": "Confirmed",
    "payment.unconfirmed": "Taken back to pending",
    "payment.voided": "Voided",
    "payment.deleted": "Deleted",
    "paymenttransaction.created": "Recorded",
    "paymenttransaction.changed": "Corrected",
    "paymenttransaction.confirmed": "Confirmed",
    "paymenttransaction.voided": "Voided",
    "paymenttransaction.deleted": "Deleted",
    "ownership.transfer_started": "Ownership transfer started",
    "ownership.transfer_cancelled": "Ownership transfer cancelled",
    "ownership.transfer_declined": "Ownership transfer declined",
    "ownership.transferred": "Ownership transferred",
}


class AuditQuerySet(TenantQuerySet):
    def delete(self):
        raise PermissionDenied(_NOT_REWRITABLE)

    def update(self, **kwargs):
        # Django's own SET_NULL cascade comes through here, and has to keep working.
        if all(k in _SET_NULL_ON_DELETE and v is None for k, v in kwargs.items()):
            return super().update(**kwargs)
        raise PermissionDenied(_NOT_REWRITABLE)


class AuditManager(TenantManager.from_queryset(AuditQuerySet)):
    """Scoped like every tenant table: another school's events are invisible."""


class AuditUnscopedManager(UnscopedManager.from_queryset(AuditQuerySet)):
    """Every event, for the platform and for jobs."""


class AuditEvent(models.Model):
    #: Null for platform accounts, which belong to no school.
    school = models.ForeignKey(
        "schools.School", on_delete=models.CASCADE, null=True, blank=True,
        related_name="audit_events",
    )
    branch = models.ForeignKey(
        "schools.Branch", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="audit_events",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="audit_events",
    )
    #: Kept as text too, so the record still names them once the account is gone.
    actor_name = models.CharField(max_length=150, blank=True)
    #: ``web``, ``admin``, or ``system`` for anything outside a request.
    source = models.CharField(max_length=16)
    ip_address = models.CharField(max_length=45, blank=True)

    category = models.CharField(max_length=16, choices=Category.choices)
    action = models.CharField(max_length=64)
    target_type = models.CharField(max_length=64)
    target_id = models.CharField(max_length=64)
    #: What a person would call it: a receipt number, a username.
    target_label = models.CharField(max_length=160, blank=True)
    #: ``{field: [old, new]}``.
    changes = models.JSONField(default=dict, blank=True)

    objects = AuditManager()
    all_objects = AuditUnscopedManager()

    class Meta:
        ordering = ["-created_at", "-id"]
        base_manager_name = "all_objects"
        default_manager_name = "objects"
        indexes = [
            models.Index(fields=["target_type", "target_id"]),
            models.Index(fields=["school", "-created_at"]),
            models.Index(fields=["actor", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.action} {self.target_label or self.target_id}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise PermissionDenied(_NOT_REWRITABLE)
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionDenied(_NOT_REWRITABLE)

    @property
    def action_label(self) -> str:
        return ACTION_LABELS.get(self.action, self.action.replace(".", " ").capitalize())

    @property
    def change_lines(self) -> list[str]:
        """One readable line per changed field."""
        lines = []
        for field, (old, new) in self.changes.items():
            name = field.removesuffix("_id").replace("_", " ").capitalize()
            if field.endswith("_id"):
                old, new = (f"#{v}" if v is not None else None for v in (old, new))
            old, new = ("—" if v in (None, "") else v for v in (old, new))
            lines.append(f"{name}: {old} → {new}")
        return lines
