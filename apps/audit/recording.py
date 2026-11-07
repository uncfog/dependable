"""Writing the audit log.

On model signals rather than in the views, so a correction through the Django admin,
a management command or a service written next year is recorded without anyone
remembering to. A new model joins by adding one entry to :data:`TRACKED`.

``QuerySet.update()`` and ``bulk_create()`` skip signals, so they skip this log too.
Tracked models must be changed with ``save()``; nothing in the application updates
them any other way today.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Callable

from django.apps import apps as django_apps
from django.db.models.signals import post_delete, post_save, pre_save

from .context import current_actor
from .models import AuditEvent, Category

HIDDEN = "(hidden)"


@dataclass(frozen=True)
class Tracked:
    prefix: str
    category: str
    #: Column names, so ``student_id`` rather than ``student``.
    fields: tuple[str, ...]
    label: Callable = str
    school: Callable = lambda row: row.school_id
    branch: Callable = lambda row: row.branch_id
    #: Whether it changed is recorded; the values never are.
    hidden: tuple[str, ...] = ()
    files: tuple[str, ...] = ()
    #: Names a change of status more usefully than "changed".
    actions: Callable | None = None


def _payment_action(before: dict, after: dict) -> str | None:
    old, new = before["status"], after["status"]
    if old == new:
        return None
    return {
        "confirmed": "payment.confirmed",
        "void": "payment.voided",
        "pending": "payment.unconfirmed",
    }.get(new)


def _transaction_action(before: dict, after: dict) -> str | None:
    if before["status"] == after["status"]:
        return None
    return {
        "confirmed": "paymenttransaction.confirmed",
        "void": "paymenttransaction.voided",
    }.get(after["status"])


TRACKED = {
    "payments.Payment": Tracked(
        "payment", Category.MONEY,
        ("transaction_id", "student_id", "term_id", "branch_id", "amount", "date_paid",
         "label", "method", "reference", "receipt", "note", "status", "confirmed_by_id",
         "voided_by_id", "void_reason"),
        label=lambda row: row.receipt_number,
        files=("receipt",),
        actions=_payment_action,
    ),
    "payments.PaymentTransaction": Tracked(
        "paymenttransaction", Category.MONEY,
        ("kind", "family_id", "amount", "date_paid", "method", "reference", "payer_name",
         "receipt", "status", "confirmed_by_id", "voided_by_id", "void_reason"),
        label=lambda row: row.number,
        files=("receipt",),
        actions=_transaction_action,
    ),
    "families.Family": Tracked(
        "family", Category.ACCESS,
        ("name", "family_code", "contact_name", "contact_phone", "contact_email"),
        label=lambda row: row.name,
        branch=lambda row: None,
    ),
    "families.FamilyMembership": Tracked(
        "familymembership", Category.ACCESS,
        ("family_id", "student_id", "linked_by_id", "ended_by_id"),
    ),
    "admissions.AdmissionPayment": Tracked(
        "admissionpayment", Category.MONEY,
        ("applicant_id", "amount", "method", "status", "received_on", "reference",
         "note", "term_payment_id"),
    ),
    "fees.FeeComponent": Tracked(
        "feecomponent", Category.MONEY, ("fee_structure_id", "name", "amount"),
        label=lambda row: row.name,
    ),
    "fees.StudentFeeAdjustment": Tracked(
        "studentfeeadjustment", Category.MONEY,
        ("student_id", "term_id", "kind", "amount", "description", "voided_by_id",
         "void_reason"),
        label=lambda row: row.get_kind_display(),
        actions=lambda before, after: (
            "studentfeeadjustment.voided"
            if after["voided_by_id"] and not before["voided_by_id"]
            else None
        ),
    ),
    "accounts.User": Tracked(
        "user", Category.ACCESS,
        ("role", "school_id", "branch_id", "is_active", "is_staff", "is_superuser",
         "email", "password"),
        label=lambda row: row.get_username(),
        hidden=("password",),
    ),
    "schools.School": Tracked(
        "school", Category.ACCESS, ("status", "plan"),
        label=lambda row: row.name,
        school=lambda row: row.pk,
        branch=lambda row: None,
    ),
    "schools.SchoolModule": Tracked(
        "schoolmodule", Category.ACCESS, ("key", "enabled"),
        label=lambda row: row.key,
        branch=lambda row: None,
    ),
}


def record(*, action, category, target_type, target_id, target_label="", school_id=None,
           branch_id=None, changes=None) -> AuditEvent:
    """Write one event. Also the way in for anything that is not a model change."""
    actor, source, address = current_actor()
    return AuditEvent.all_objects.create(
        school_id=school_id,
        branch_id=branch_id,
        actor_id=actor.pk if actor is not None else None,
        actor_name=actor.get_username() if actor is not None else "",
        source=source,
        ip_address=address,
        category=category,
        action=action,
        target_type=target_type,
        target_id=str(target_id),
        target_label=str(target_label)[:160],
        changes=changes or {},
    )


def _plain(value):
    """A value as JSON can hold it, and as it compares equal to the database's."""
    if isinstance(value, Decimal):
        return f"{value:.2f}"
    if value is None or isinstance(value, (bool, int)):
        return value
    return str(value.isoformat() if hasattr(value, "isoformat") else value)


def _read(spec: Tracked, source) -> dict:
    """``source`` is a dict from ``values()`` or a model instance."""
    read = source.get if isinstance(source, dict) else lambda f: getattr(source, f)
    values = {}
    for field in spec.fields:
        value = read(field)
        if field in spec.files:
            value = getattr(value, "name", value) or None
        values[field] = _plain(value)
    return values


def _difference(spec: Tracked, before: dict, after: dict, fields) -> dict:
    changed = {}
    for field in fields:
        if before.get(field) != after.get(field):
            changed[field] = (
                [HIDDEN, HIDDEN]
                if field in spec.hidden
                else [before.get(field), after.get(field)]
            )
    return changed


def _remember(sender, instance, raw=False, update_fields=None, **kwargs):
    instance._audit_before = None
    if raw or instance._state.adding or instance.pk is None:
        return
    spec = TRACKED[sender._meta.label]
    row = sender._base_manager.filter(pk=instance.pk).values(*spec.fields).first()
    if row is not None:
        instance._audit_before = _read(spec, row)


def _write(spec: Tracked, sender, instance, action: str, changes: dict, *, branch=True):
    record(
        action=action,
        category=spec.category,
        target_type=sender._meta.label,
        target_id=instance.pk,
        target_label=spec.label(instance),
        school_id=spec.school(instance),
        branch_id=spec.branch(instance) if branch else None,
        changes=changes,
    )


def _saved(sender, instance, created=False, raw=False, update_fields=None, **kwargs):
    if raw:
        return
    spec = TRACKED[sender._meta.label]
    after = _read(spec, instance)

    if created:
        changes = {
            field: [None, HIDDEN if field in spec.hidden else value]
            for field, value in after.items()
            if value not in (None, "")
        }
        _write(spec, sender, instance, f"{spec.prefix}.created", changes)
        return

    before = getattr(instance, "_audit_before", None)
    if before is None:
        return
    # A partial save wrote only these, whatever else differs on the instance.
    fields = spec.fields
    if update_fields is not None:
        written = {
            sender._meta.get_field(name).attname for name in update_fields
        }
        fields = [field for field in fields if field in written]
    changes = _difference(spec, before, after, fields)
    if not changes:
        return
    action = spec.actions(before, after) if spec.actions else None
    _write(spec, sender, instance, action or f"{spec.prefix}.changed", changes)


def _deleted(sender, instance, origin=None, **kwargs):
    # A closing school's rows are going together, and its events with them. An event
    # written here would point at a school about to vanish, and the delete would fail.
    school_model = django_apps.get_model("schools.School")
    origin_model = getattr(origin, "model", type(origin))
    if origin_model is school_model:
        return
    spec = TRACKED[sender._meta.label]
    # No campus: this may be the campus being deleted.
    _write(spec, sender, instance, f"{spec.prefix}.deleted", {}, branch=False)


def connect() -> None:
    for label in TRACKED:
        model = django_apps.get_model(label)
        uid = f"audit_{label}"
        pre_save.connect(_remember, sender=model, dispatch_uid=f"{uid}_pre")
        post_save.connect(_saved, sender=model, dispatch_uid=f"{uid}_post")
        post_delete.connect(_deleted, sender=model, dispatch_uid=f"{uid}_del")


def history_for(model_label: str, pk) -> list[AuditEvent]:
    """A record's own history, newest first, through the tenant-scoped manager."""
    return list(
        AuditEvent.objects.filter(target_type=model_label, target_id=str(pk)).order_by(
            "-created_at", "-id"
        )
    )
