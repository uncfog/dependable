"""The tenant itself: a School, and the Branches (campuses) beneath it."""

from __future__ import annotations

import hashlib

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.urls import reverse
from django.utils.text import slugify

from apps.core.models import TimeStampedModel
from apps.core.tenancy import TenantManager, TenantQuerySet, UnscopedManager

#: What a school may upload as its logo, and how big. Deliberately the same
#: shape of rule as the payment-receipt upload: a school's logo arrives as a
#: PNG someone exported from a design, or a JPEG photographed off a signboard.
LOGO_TYPES = {".png", ".jpg", ".jpeg", ".webp", ".svg"}
LOGO_MAX_BYTES = 2 * 1024 * 1024


def validate_logo(upload) -> None:
    """Reject anything that is not a reasonably sized image.

    A ``FileField`` with this validator rather than an ``ImageField``, which
    would pull in Pillow for the sake of reading a header. The receipt upload
    on ``payments.Payment`` made the same trade, and a dependency the school's
    server then has to keep patched is not free.
    """
    name = (getattr(upload, "name", "") or "").lower()
    if name and not any(name.endswith(ext) for ext in sorted(LOGO_TYPES)):
        raise ValidationError(
            "Upload the logo as a PNG, JPEG, WebP or SVG image."
        )
    size = getattr(upload, "size", 0) or 0
    if size > LOGO_MAX_BYTES:
        raise ValidationError(
            f"That file is {size // 1024 // 1024} MB. Keep the logo under "
            f"{LOGO_MAX_BYTES // 1024 // 1024} MB so it loads on a slow "
            f"connection."
        )


def logo_upload_to(instance: "School", filename: str) -> str:
    """One folder per school, keyed by slug so the path reads in a file browser."""
    return f"logos/{instance.slug or instance.pk or 'school'}/{filename}"


class Plan(models.TextChoices):
    FREE = "free", "Free"
    TRIAL = "trial", "Trial"
    STARTER = "starter", "Starter"
    GROWTH = "growth", "Growth"
    PROFESSIONAL = "professional", "Professional"
    ENTERPRISE = "enterprise", "Enterprise"


class SchoolStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    TRIALING = "trialing", "Trialing"
    PAST_DUE = "past_due", "Past due"
    SUSPENDED = "suspended", "Suspended"
    CANCELLED = "cancelled", "Cancelled"


class SchoolQuerySet(TenantQuerySet):
    # A school *is* the tenant, so the school column on this table is its own pk.
    tenant_school_field = "id"
    tenant_branch_field = None


class SchoolManager(TenantManager.from_queryset(SchoolQuerySet)):
    """Auto-scoped: a school owner querying schools sees only their own."""


class SchoolUnscopedManager(UnscopedManager.from_queryset(SchoolQuerySet)):
    """Every school, regardless of caller. Used by admin FK dropdowns and jobs."""


class School(TimeStampedModel):
    """A tenant.  Every other row on the platform ultimately belongs to one."""

    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=220, unique=True, blank=True)
    plan = models.CharField(max_length=20, choices=Plan.choices, default=Plan.TRIAL)
    status = models.CharField(
        max_length=20, choices=SchoolStatus.choices, default=SchoolStatus.TRIALING
    )
    contact_email = models.EmailField(blank=True)
    contact_phone = models.CharField(max_length=32, blank=True)
    logo = models.FileField(
        upload_to=logo_upload_to,
        validators=[validate_logo],
        null=True,
        blank=True,
        help_text="Optional. Appears on your screens and on payment receipts. "
        "Without one, your school's initial is shown instead.",
    )

    objects = SchoolManager()
    all_objects = SchoolUnscopedManager()

    class Meta:
        ordering = ["name"]
        base_manager_name = "all_objects"
        default_manager_name = "objects"

    def __str__(self) -> str:
        return self.name

    @property
    def is_active(self) -> bool:
        return self.status in {SchoolStatus.ACTIVE, SchoolStatus.TRIALING}

    @property
    def logo_url(self) -> str:
        """Where a page points an ``<img>`` at this school's logo, or ``""``.

        The app's own view rather than the storage URL, because the bucket is
        private -- see :mod:`apps.core.files`. ``v`` changes whenever the stored
        file does (every upload gets a fresh name), so the view can tell the
        browser to cache it for a year without a new logo ever going unseen.
        """
        if not self.logo:
            return ""
        version = hashlib.sha256(self.logo.name.encode()).hexdigest()[:12]
        return f"{reverse('core:school_logo', args=[self.slug])}?v={version}"

    @property
    def initial(self) -> str:
        """The letter shown when there is no logo.

        Falls back to a question mark rather than an empty square, so a
        half-created school still renders as something on purpose.
        """
        return (self.name.strip()[:1] or "?").upper()

    @property
    def has_logo(self) -> bool:
        return bool(self.logo)

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = self._unique_slug()
        super().save(*args, **kwargs)

    def _unique_slug(self) -> str:
        base = slugify(self.name) or "school"
        slug, counter = base, 2
        while School.all_objects.filter(slug=slug).exclude(pk=self.pk).exists():
            slug = f"{base}-{counter}"
            counter += 1
        return slug


class BranchQuerySet(TenantQuerySet):
    tenant_school_field = "school_id"
    # For the branch table itself the "branch column" is the primary key, and a
    # branch is never school-wide, so shared rows do not apply.
    tenant_branch_field = "id"
    tenant_include_school_wide = False


class BranchManager(TenantManager.from_queryset(BranchQuerySet)):
    """Auto-scoped: branch staff see their own campus, school owners see all."""


class BranchUnscopedManager(UnscopedManager.from_queryset(BranchQuerySet)):
    """Every branch, regardless of caller."""


class Branch(TimeStampedModel):
    """A campus of a school.  The second, narrower axis of tenant isolation."""

    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="branches")
    name = models.CharField(max_length=200)
    city = models.CharField(max_length=120, blank=True)
    state = models.CharField(max_length=120, blank=True)
    address = models.TextField(blank=True)
    head = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="branches_headed",
        null=True,
        blank=True,
        help_text="Principal or branch administrator responsible for this campus.",
    )
    is_active = models.BooleanField(default=True)

    objects = BranchManager()
    all_objects = BranchUnscopedManager()

    class Meta:
        ordering = ["school__name", "name"]
        verbose_name_plural = "branches"
        base_manager_name = "all_objects"
        default_manager_name = "objects"
        constraints = [
            models.UniqueConstraint(
                fields=["school", "name"], name="unique_branch_name_per_school"
            )
        ]

    def __str__(self) -> str:
        return f"{self.school.name} - {self.name}"


class PlanOrderQuerySet(TenantQuerySet):
    # An order belongs to one school and to no campus: the school pays for its plan.
    tenant_school_field = "school_id"
    tenant_branch_field = None


class PlanOrderManager(TenantManager.from_queryset(PlanOrderQuerySet)):
    """Auto-scoped: a school sees its own orders and nobody else's."""


class PlanOrderUnscopedManager(UnscopedManager.from_queryset(PlanOrderQuerySet)):
    """Every order. The platform owner confirms payments across schools."""


class PlanOrder(TimeStampedModel):
    """A school's request to move onto a paid plan, paid by bank transfer.

    The order is the record of the money, not of the plan. The school's plan only
    changes when the platform owner marks the order paid in the admin, so an order
    that is waiting, reported or cancelled changes nothing anyone sees in the app.
    """

    class Status(models.TextChoices):
        AWAITING_TRANSFER = "awaiting_transfer", "Awaiting transfer"
        TRANSFER_REPORTED = "transfer_reported", "Transfer reported"
        PAID = "paid", "Paid"
        CANCELLED = "cancelled", "Cancelled"

    school = models.ForeignKey(
        School, on_delete=models.CASCADE, related_name="plan_orders"
    )
    plan = models.CharField(max_length=20, choices=Plan.choices)
    amount_naira = models.PositiveIntegerField()
    #: The narration the school puts on its transfer, so the payment can be matched.
    reference = models.CharField(max_length=24, unique=True)
    status = models.CharField(
        max_length=24, choices=Status.choices, default=Status.AWAITING_TRANSFER
    )
    transfer_reported_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    objects = PlanOrderManager()
    all_objects = PlanOrderUnscopedManager()

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.reference} ({self.school.name})"


class PlanPrice(TimeStampedModel):
    """What a plan costs and who it is for, as the platform owner set it.

    One row per plan that is sold. The landing page, the checkout, the platform's
    Plans & billing screen and a school owner's settings all read these through
    ``apps.schools.plans.offers()``. A blank price means "quoted". An order keeps the
    amount it was placed at, so changing a price never changes an order already open.
    """

    plan = models.CharField(max_length=20, choices=Plan.choices, unique=True)
    price_naira = models.PositiveIntegerField(null=True, blank=True)
    students_label = models.CharField(max_length=60)
    max_students = models.PositiveIntegerField(null=True, blank=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+",
    )

    def __str__(self) -> str:
        return f"{self.get_plan_display()}: {self.price_naira}"


class PlanPriceChange(models.Model):
    """One saved change to the price list: what changed, by whom, and when."""

    plan = models.CharField(max_length=20, choices=Plan.choices)
    summary = models.CharField(max_length=300)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+",
    )
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-changed_at", "-id"]

    def __str__(self) -> str:
        return f"{self.plan}: {self.summary}"


class SchoolModuleQuerySet(TenantQuerySet):
    # A row belongs to one school and to no campus: a module is bought by the
    # school, not opened at a branch.
    tenant_school_field = "school_id"
    tenant_branch_field = None


class SchoolModuleManager(TenantManager.from_queryset(SchoolModuleQuerySet)):
    """Auto-scoped: a school reads its own switches and nobody else's."""


class SchoolModuleUnscopedManager(
    UnscopedManager.from_queryset(SchoolModuleQuerySet)
):
    """Every row, regardless of caller.

    Used by the platform owner's screens, which are cross-tenant by definition,
    and by ``apps.core.modules``, which is always asked about a school it has
    been handed the id of -- including, for a school's public enquiry page, a
    school that is not the reader's own.
    """


class SchoolModule(TimeStampedModel):
    """Whether one school has one module. Absent means "nobody has decided".

    A row is a *decision*, not a state: with no row, the school gets whatever
    ``apps.core.modules`` declares as that module's default, so a school that
    signs up today needs nothing written for it and a later change of default
    moves every school that never chose. Writing a row is how the platform owner
    overrides that, in either direction.

    Nothing here knows what a module is or does. The registry in
    ``apps.core.modules`` is the authority on which keys exist, which URLs and
    capabilities each one owns, and which cannot be switched off at all -- so a
    key stored here that the registry does not recognise is ignored rather than
    obeyed.
    """

    school = models.ForeignKey(
        School, on_delete=models.CASCADE, related_name="module_states"
    )
    #: A key from ``apps.core.modules.MODULES``. Deliberately a plain string
    #: rather than a choices field: the registry is code, and a migration per
    #: new module would make adding one a database change for no reason.
    key = models.CharField(max_length=40)
    enabled = models.BooleanField()
    #: Who last flipped it. Kept because "who turned our messaging off?" is a
    #: question somebody will ask, and null once that account is deleted.
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="module_changes",
        null=True,
        blank=True,
    )

    objects = SchoolModuleManager()
    all_objects = SchoolModuleUnscopedManager()

    class Meta:
        ordering = ["school__name", "key"]
        base_manager_name = "all_objects"
        default_manager_name = "objects"
        verbose_name = "school module"
        constraints = [
            # One decision per module per school, enforced in the database: two
            # rows disagreeing about the same switch is a state no screen could
            # render honestly.
            models.UniqueConstraint(
                fields=["school", "key"], name="unique_module_per_school"
            )
        ]
        indexes = [models.Index(fields=["school", "key"])]

    def __str__(self) -> str:
        return f"{self.school.name} - {self.key}: {'on' if self.enabled else 'off'}"

    @classmethod
    def set_state(cls, school, key: str, enabled: bool, *, by=None) -> "SchoolModule":
        """Record a decision about one module at one school.

        Refuses a module the registry does not know, and one it says cannot be
        switched off -- a caller that could write either would be creating a row
        that every reader then has to ignore.
        """
        from apps.core.modules import ALWAYS_ON, BY_KEY

        if key not in BY_KEY:
            raise ValueError(f"No such module: {key!r}")
        if key in ALWAYS_ON:
            raise ValueError(f"{key!r} is part of the product and cannot be switched off.")

        row, _ = cls.all_objects.update_or_create(
            school=school,
            key=key,
            defaults={"enabled": bool(enabled), "changed_by": by},
        )
        return row
