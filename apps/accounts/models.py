"""The custom user model.

Swapped in before the first migration, because ``AUTH_USER_MODEL`` cannot be
changed afterwards without a painful data migration.
"""

from __future__ import annotations

from django.conf import settings
from django.contrib.auth.models import AbstractUser, UserManager
from django.db import models
from django.utils import timezone

from apps.core.roles import Role, Scope, scope_for
from apps.core.tenancy import (
    TenantManager,
    TenantQuerySet,
    UnscopedManager,
    get_current_context,
)


class UserQuerySet(TenantQuerySet):
    tenant_school_field = "school_id"
    tenant_branch_field = "branch_id"
    # A user with no branch belongs to the school as a whole (e.g. the owner),
    # and branch staff should be able to see them in directories.
    tenant_include_school_wide = True


class DependableUserManager(UserManager.from_queryset(UserQuerySet)):
    """Unscoped user manager -- named, because migrations must serialise it.

    Keeps ``create_user``/``create_superuser`` and adds the tenancy queryset
    helpers without applying any filtering of its own.
    """

    use_in_migrations = True


class ScopedUserManager(DependableUserManager):
    """Tenant-filtered view of the user table, exposed as ``User.scoped``."""

    # Migrations must never run through tenant filtering.
    use_in_migrations = False

    def get_queryset(self) -> UserQuerySet:
        return super().get_queryset().for_context(get_current_context())


class User(AbstractUser):
    """A person with an account.

    ``role`` is the permission role -- what they may see.  ``job_title`` is what
    they do at the school.  The two are intentionally separate fields: promoting
    a bursar to "Head of Finance" is an HR change, not an access change.
    """

    school = models.ForeignKey(
        "schools.School",
        on_delete=models.CASCADE,
        related_name="users",
        null=True,
        blank=True,
        help_text="The tenant this account belongs to. Blank for platform staff.",
    )
    branch = models.ForeignKey(
        "schools.Branch",
        on_delete=models.SET_NULL,
        related_name="users",
        null=True,
        blank=True,
        help_text="Optional. Leave blank for school-wide accounts.",
    )
    role = models.CharField(
        "permission role",
        max_length=32,
        choices=Role.choices,
        default=Role.BURSAR,
        help_text="Access level. Determines which rows and screens are visible.",
    )
    job_title = models.CharField(
        max_length=120,
        blank=True,
        help_text="Their job at the school, e.g. 'Head of Mathematics'. "
        "Has no effect on permissions.",
    )
    phone = models.CharField(max_length=32, blank=True)
    must_change_password = models.BooleanField(
        default=False,
        # A database default as well as a Python one, so a deployment still on
        # the code from before this column can keep inserting users once the
        # migration has run.
        db_default=False,
        help_text="Set when the account was given a temporary password. Until "
        "they choose their own, every screen sends them to Account settings.",
    )

    # ``objects`` must stay unscoped: authentication backends and
    # ``createsuperuser`` resolve users through the default manager, before any
    # tenant context exists.  Use ``User.scoped`` for tenant-facing listings.
    objects = DependableUserManager()
    scoped = ScopedUserManager()

    class Meta:
        ordering = ["last_name", "first_name", "username"]
        indexes = [models.Index(fields=["school", "branch"])]

    def __str__(self) -> str:
        return self.get_full_name() or self.username

    @property
    def scope(self) -> Scope:
        return scope_for(self.role)

    @property
    def is_platform_staff(self) -> bool:
        return self.scope is Scope.PLATFORM or self.is_superuser

    def set_password(self, raw_password):
        """Choosing a password, by any route, ends a temporary one.

        Account settings, the reset email and the admin all come through here,
        so none of them has to remember to clear the flag. Whatever gives out a
        temporary password sets the flag *after* calling this.
        """
        super().set_password(raw_password)
        self.must_change_password = False

    def clean(self):
        super().clean()
        from django.core.exceptions import ValidationError

        if self.branch_id and self.school_id:
            if self.branch.school_id != self.school_id:
                raise ValidationError(
                    {"branch": "Branch must belong to the selected school."}
                )
        if self.role != Role.PLATFORM_OWNER and not self.school_id:
            raise ValidationError(
                {"school": "Non-platform accounts must belong to a school."}
            )


class OwnershipTransferQuerySet(TenantQuerySet):
    # A transfer belongs to one school and to no campus.
    tenant_school_field = "school_id"
    tenant_branch_field = None


class OwnershipTransferManager(TenantManager.from_queryset(OwnershipTransferQuerySet)):
    """Auto-scoped: a school sees its own transfers and nobody else's."""


class OwnershipTransferUnscopedManager(
    UnscopedManager.from_queryset(OwnershipTransferQuerySet)
):
    """Every transfer. The acceptance link is opened by someone with no school yet."""


class OwnershipTransfer(models.Model):
    """A school's owner handing the school to someone else, by email.

    The owner starts it; the person it is for accepts from a single-use link; the
    two roles swap in one transaction (see ``apps.accounts.ownership``). Nothing
    the school owns moves: students, payments, the plan and the audit history are
    the school's, and only the two accounts change.

    Only a SHA-256 of the link's token is stored, so a copy of this table cannot
    be used to accept anything.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Waiting to be accepted"
        ACCEPTED = "accepted", "Accepted"
        DECLINED = "declined", "Declined"
        CANCELLED = "cancelled", "Cancelled"
        EXPIRED = "expired", "Expired"

    class Afterwards(models.TextChoices):
        """What happens to the outgoing owner's account."""

        STAY_PRINCIPAL = "stay_principal", "Stay on as principal of a campus"
        LEAVE = "leave", "Stop having access"

    school = models.ForeignKey(
        "schools.School", on_delete=models.CASCADE, related_name="ownership_transfers"
    )
    #: Null when the platform started it (the owner had lost access).
    started_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ownership_transfers_started",
    )
    to_email = models.EmailField()
    to_name = models.CharField(max_length=150)
    #: Set when accepted: the account that became the owner.
    to_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ownership_transfers_received",
    )
    afterwards = models.CharField(max_length=20, choices=Afterwards.choices)
    #: The campus the outgoing owner runs afterwards, for STAY_PRINCIPAL.
    stay_branch = models.ForeignKey(
        "schools.Branch",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    token_hash = models.CharField(max_length=64, unique=True)
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.PENDING
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    decided_at = models.DateTimeField(null=True, blank=True)

    objects = OwnershipTransferManager()
    all_objects = OwnershipTransferUnscopedManager()

    class Meta:
        ordering = ["-created_at"]
        base_manager_name = "all_objects"
        default_manager_name = "objects"
        constraints = [
            # One open handover at a time, enforced where two tabs cannot race it.
            models.UniqueConstraint(
                fields=["school"],
                condition=models.Q(status="pending"),
                name="one_pending_ownership_transfer_per_school",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.school} to {self.to_email} ({self.get_status_display()})"

    @property
    def is_open(self) -> bool:
        """Pending and not yet past its expiry."""
        return self.status == self.Status.PENDING and self.expires_at > timezone.now()


class RoleHandover(models.Model):
    """A principal or bursar handing their own position to someone new, by email.

    The person asks; the school's owner approves; the newcomer accepts from a
    single-use link; then the newcomer has the same role at the same campus and
    the old account is switched off. Nobody gains anything above the role being
    handed over. The rules are in :mod:`apps.accounts.handover`.

    ``school`` only, never branch-scoped: an owner must see every campus's
    requests, and the scoped manager would stamp the asker's branch.
    """

    class Status(models.TextChoices):
        AWAITING_APPROVAL = "awaiting_approval", "Waiting for the owner"
        PENDING = "pending", "Waiting to be accepted"
        ACCEPTED = "accepted", "Accepted"
        DECLINED = "declined", "Declined"
        REJECTED = "rejected", "Not approved"
        CANCELLED = "cancelled", "Cancelled"
        EXPIRED = "expired", "Expired"

    OPEN = (Status.AWAITING_APPROVAL, Status.PENDING)

    school = models.ForeignKey(
        "schools.School", on_delete=models.CASCADE, related_name="role_handovers"
    )
    branch = models.ForeignKey(
        "schools.Branch", on_delete=models.CASCADE, related_name="role_handovers"
    )
    from_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="role_handovers"
    )
    #: The role being handed over, kept so a later role change cannot widen it.
    role = models.CharField(max_length=32, choices=Role.choices)
    to_email = models.EmailField()
    to_name = models.CharField(max_length=150)
    to_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    #: SHA-256 of the emailed token. Empty until the owner approves.
    token_hash = models.CharField(max_length=64, blank=True)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.AWAITING_APPROVAL
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["from_user"],
                condition=models.Q(status__in=["awaiting_approval", "pending"]),
                name="one_open_role_handover_per_person",
            ),
            models.UniqueConstraint(
                fields=["token_hash"],
                condition=~models.Q(token_hash=""),
                name="role_handover_token_unique",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_role_display()} at {self.branch} to {self.to_email}"

    @property
    def is_acceptable(self) -> bool:
        return (
            self.status == self.Status.PENDING
            and self.expires_at is not None
            and self.expires_at > timezone.now()
        )
