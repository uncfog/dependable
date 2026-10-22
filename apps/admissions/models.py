"""Admissions: one prospective child, captured once, moved along until enrolled.

The paid add-on beyond the original scope. Its shape is set by one decision
worth stating up front, because everything else follows from it:

**An applicant is its own entity, not a Student with a draft flag.**
A ``Student`` is a child on the roll -- billed, counted in the head count,
present in every report the school runs. A child whose parent filled a form on
Instagram at midnight is none of those things, and the moment the two share a
table every ``Student.objects`` query in the platform starts returning people
who have never set foot in the school. Enrolment is therefore a *conversion*:
:mod:`apps.admissions.enrolment` creates the Student, and the applicant keeps
its history and a link to who it became.

Second decision: **one record, many stages.** An enquiry does not become a
different row when it becomes an application. The status advances and the
application stage *adds* fields to what was already captured, which is the
whole reason a parent is never asked for their child's date of birth twice.

    enquiry -> application -> assessed -> offered/rejected -> enrolled
                                                           \\-> expired
                                                           \\-> withdrawn

Third: **admission money is not term money.** ``fees.FeeStructure`` is what a
class owes per term and is not touched here. What a family pays to come in --
the one-time admission fee and the one-off intake items -- is
:class:`AdmissionFeeSchedule` and :class:`AdmissionFeeItem`, a separate set
with its own payments, so seeding or repricing one can never overwrite the
other. The two never price the same item: what recurs every term (tuition,
books, development) is on the term structure only, and a new child's bill is
the intake sheet *plus* the current term. When a family pays both in one lump,
:mod:`apps.admissions.split` divides the money between the two ledgers.

Everything is tenant-scoped by inheritance, so no view in this app filters by
school or branch: one school cannot see another's applicants because the
managers already made that impossible.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import IntegrityError, models, transaction
from django.urls import reverse
from django.utils import timezone

from apps.academics.models import Level
from apps.core.models import BranchScopedModel, TenantScopedModel
from apps.core.tenancy import TenantManager, TenantQuerySet, UnscopedManager
from apps.students.validators import (
    format_phone,
    normalise_phone,
    to_international,
    validate_phone,
)

from .requirements import RequirementKind
from .validators import validate_document, validate_photo

ZERO = Decimal("0")


# ===========================================================================
# Vocabulary
# ===========================================================================


class ApplicantStatus(models.TextChoices):
    """Where a prospective child stands with the school.

    The order of the members is the order of the pipeline, and the dashboard
    reads it rather than restating it.

    Nothing is ever deleted. A rejected applicant, an expired enquiry and a
    parent who changed their mind are all facts about the intake, and a school
    asking "how many enquiries did Instagram bring us and how many converted?"
    needs the ones that did not.
    """

    ENQUIRY = "enquiry", "Enquiry"
    APPLICATION = "application", "Application"
    ASSESSED = "assessed", "Assessed"
    OFFERED = "offered", "Offer made"
    REJECTED = "rejected", "Rejected"
    ENROLLED = "enrolled", "Enrolled"
    EXPIRED = "expired", "Expired"
    WITHDRAWN = "withdrawn", "Withdrawn"


#: The stages an applicant walks through, in order. The pipeline board shows
#: exactly these as columns.
PIPELINE_STATUSES: tuple[str, ...] = (
    ApplicantStatus.ENQUIRY,
    ApplicantStatus.APPLICATION,
    ApplicantStatus.ASSESSED,
    ApplicantStatus.OFFERED,
    ApplicantStatus.ENROLLED,
)

#: Still live: someone at the school still has something to do about them.
OPEN_STATUSES: tuple[str, ...] = (
    ApplicantStatus.ENQUIRY,
    ApplicantStatus.APPLICATION,
    ApplicantStatus.ASSESSED,
    ApplicantStatus.OFFERED,
)

#: Finished, one way or another.
CLOSED_STATUSES: tuple[str, ...] = (
    ApplicantStatus.REJECTED,
    ApplicantStatus.ENROLLED,
    ApplicantStatus.EXPIRED,
    ApplicantStatus.WITHDRAWN,
)

#: Design-system pill per status, reusing the four payment-status colours the
#: platform already teaches staff to read.
STATUS_PILLS: dict[str, str] = {
    ApplicantStatus.ENQUIRY: "status-unpaid",
    ApplicantStatus.APPLICATION: "status-partial",
    ApplicantStatus.ASSESSED: "status-partial",
    ApplicantStatus.OFFERED: "status-paid",
    ApplicantStatus.ENROLLED: "status-paid",
    ApplicantStatus.REJECTED: "status-overdue",
    ApplicantStatus.EXPIRED: "status-unpaid",
    ApplicantStatus.WITHDRAWN: "status-unpaid",
}


class ApplicantSource(models.TextChoices):
    """How the record got here -- typed by staff, or filled in by the parent.

    Not the same question as :class:`HeardAbout`, which is where the *family*
    came from. A walk-in who found the school on Instagram is ``WALK_IN`` and
    ``INSTAGRAM``, and both answers are worth having.
    """

    ONLINE = "online", "Online enquiry form"
    WALK_IN = "walk_in", "Walk-in / front desk"
    PHONE = "phone", "Phone call"
    IMPORTED = "imported", "Imported"


class HeardAbout(models.TextChoices):
    """Where the family found the school. The marketing question.

    Free text would make it unanswerable in aggregate, which is the only
    reason anybody asks it, so it is a fixed list with an escape hatch.
    """

    INSTAGRAM = "instagram", "Instagram"
    FACEBOOK = "facebook", "Facebook"
    WHATSAPP = "whatsapp", "WhatsApp"
    WEBSITE = "website", "School website"
    REFERRAL = "referral", "A friend or family member"
    FLYER = "flyer", "Flyer or banner"
    RADIO = "radio", "Radio or TV"
    PLACE_OF_WORSHIP = "place_of_worship", "Church or mosque"
    PASSING_BY = "passing_by", "Passed by the school"
    OTHER = "other", "Somewhere else"


class Sex(models.TextChoices):
    """Deliberately the same two values as ``students.Sex``.

    Enrolment copies this straight across, and a mismatch would mean a
    conversion that silently drops a field the roster requires.
    """

    MALE = "male", "Male"
    FEMALE = "female", "Female"


# ===========================================================================
# Per-school policy
# ===========================================================================


class IntakeMode(models.TextChoices):
    """How a school takes new children in.

    ``PIPELINE`` is enquiry, application, assessment, offer, enrolment -- for a
    school whose parents find it online and come back days later. ``WALK_IN``
    is the parent at the desk who pays and leaves with the child enrolled, in
    one screen. Most schools see both, which is the default.
    """

    BOTH = "both", "Walk-ins and the full pipeline"
    WALK_IN = "walk_in", "Walk-ins only"
    PIPELINE = "pipeline", "The full pipeline only"


class AdmissionsConfig(TenantScopedModel):
    """One school's admissions policy: how long an enquiry lives, and who sees it.

    Every field here is a question the client asked to be able to answer
    differently per school, which is exactly why none of them is a constant in
    a settings file. Where a school has no row, :meth:`for_school` returns an
    unsaved instance carrying the defaults, so the feature works fully on the
    day the app is installed and configuring it is an improvement rather than a
    prerequisite.
    """

    #: A fresh enquiry is good for three weeks. Long enough that a parent who
    #: enquires before a holiday can still act on it afterwards; short enough
    #: that the pipeline does not silt up with people who moved on in March.
    DEFAULT_VALIDITY_DAYS = 21
    #: The nudge lands a week in -- past "I only just asked", well before the
    #: window closes.
    DEFAULT_REMINDER_AFTER_DAYS = 7

    enquiry_validity_days = models.PositiveSmallIntegerField(
        "enquiry valid for (days)",
        default=DEFAULT_VALIDITY_DAYS,
        validators=[MinValueValidator(1)],
        help_text="An enquiry that has not become an application within this "
        "many days expires. It is kept, not deleted.",
    )
    reminder_after_days = models.PositiveSmallIntegerField(
        "send the reminder after (days)",
        default=DEFAULT_REMINDER_AFTER_DAYS,
        validators=[MinValueValidator(1)],
        help_text="How long to wait before emailing the parent to continue "
        "their application. Must be inside the validity window.",
    )
    bursar_can_view_applicants = models.BooleanField(
        "bursar can see applicants",
        default=False,
        help_text="Off by default: a bursar sees admission-fee payments "
        "without seeing the applicant pipeline. Turn on for a school whose "
        "bursar runs the front desk. A bursar never decides on an "
        "application either way.",
    )
    # db_default as well as default on the two below: the column must have a
    # value for a row inserted by code deployed before this field existed, so
    # the migration can land ahead of the deploy.
    intake_mode = models.CharField(
        "how new children are admitted",
        max_length=20,
        choices=IntakeMode.choices,
        default=IntakeMode.BOTH,
        db_default=IntakeMode.BOTH,
        help_text="Walk-ins are enrolled and paid for on one screen. The full "
        "pipeline runs enquiries and applications for parents who apply online.",
    )
    bursar_can_enrol_walk_ins = models.BooleanField(
        "bursar can enrol walk-ins",
        default=False,
        db_default=False,
        help_text="Off by default: enrolling a walk-in admits the child, which "
        "is the owner's and principal's decision. Turn on for a school whose "
        "bursar runs the front desk.",
    )
    require_payment_before_enrolment = models.BooleanField(
        default=True,
        help_text="Refuse to enrol an accepted applicant until the admission "
        "fee has been confirmed. Turn off for a school that admits first and "
        "collects afterwards.",
    )

    class Meta(TenantScopedModel.Meta):
        abstract = False
        verbose_name = "admissions settings"
        verbose_name_plural = "admissions settings"
        ordering = ["school__name"]
        constraints = [
            # One policy per school. Branch is inherited but unused here: an
            # admissions policy that differed per campus would mean two windows
            # on one school's pipeline board, and nobody has asked for that.
            models.UniqueConstraint(
                fields=["school"], name="one_admissions_config_per_school"
            )
        ]

    def __str__(self) -> str:
        return f"Admissions settings for {self.school}"

    @property
    def offers_walk_ins(self) -> bool:
        return self.intake_mode in (IntakeMode.BOTH, IntakeMode.WALK_IN)

    @property
    def offers_pipeline(self) -> bool:
        return self.intake_mode in (IntakeMode.BOTH, IntakeMode.PIPELINE)

    @classmethod
    def for_school(cls, school) -> "AdmissionsConfig":
        """The school's policy, or an unsaved default carrying the same fields.

        Never returns ``None``: callers ask for a policy and get one, so no
        screen has to carry an "if the school has not configured this" branch.
        Read through ``all_objects`` because the public enquiry form asks this
        question with no tenant context at all.
        """
        if school is None:
            return cls()
        school_id = getattr(school, "pk", school)
        existing = cls.all_objects.filter(school_id=school_id).first()
        return existing if existing is not None else cls(school_id=school_id)

    def clean(self):
        super().clean()
        if self.reminder_after_days and self.enquiry_validity_days:
            if self.reminder_after_days >= self.enquiry_validity_days:
                raise ValidationError(
                    {
                        "reminder_after_days": "The reminder has to go out "
                        "before the enquiry expires, so this must be fewer "
                        f"than {self.enquiry_validity_days} days."
                    }
                )


# ===========================================================================
# Configurable requirements
# ===========================================================================


class RequirementSet(TenantScopedModel):
    """A school's own answer to "what does this level have to bring?".

    Optional. A school with no sets runs on the defaults in
    :mod:`apps.admissions.requirements`, and one that wants Primary to stop
    sitting the entrance exam edits Primary rather than every screen that
    mentions it.

    ``branch`` is nullable and part of the uniqueness rule, the same shape as
    the messaging identity: a row with no branch is the school's policy, a row
    with one overrides it for that campus. :func:`requirements.profile_for` is
    the only place that ordering is expressed.
    """

    level = models.PositiveSmallIntegerField(choices=Level.choices)
    is_active = models.BooleanField(default=True)
    note = models.CharField(
        max_length=250,
        blank=True,
        help_text="Optional. Shown to staff on the settings screen.",
    )

    class Meta(TenantScopedModel.Meta):
        abstract = False
        verbose_name = "admission requirement set"
        ordering = ["school__name", "level"]
        constraints = [
            # One set per level per campus...
            models.UniqueConstraint(
                fields=["school", "branch", "level"],
                name="one_requirement_set_per_branch_level",
            ),
            # ...and one school-wide. A second constraint is needed because SQL
            # treats NULLs as distinct, so the rule above would happily allow
            # two school-wide sets for the same level.
            models.UniqueConstraint(
                fields=["school", "level"],
                condition=models.Q(branch__isnull=True),
                name="one_requirement_set_per_school_level",
            ),
        ]

    def __str__(self) -> str:
        where = self.branch.name if self.branch_id else "school-wide"
        return f"{Level(self.level).label} requirements ({where})"


class Requirement(TenantScopedModel):
    """One line of a requirement set: a document or the exam, and whether it is
    compulsory.

    ``is_required`` is the point of the row. "Optional" is a real answer -- a
    school will happily take a transfer letter from a Primary 3 applicant
    without refusing the application for want of one -- and collapsing it into
    "listed or not listed" would lose that.
    """

    requirement_set = models.ForeignKey(
        RequirementSet, on_delete=models.CASCADE, related_name="requirements"
    )
    kind = models.CharField(max_length=32, choices=RequirementKind.choices)
    is_required = models.BooleanField(default=True)
    position = models.PositiveSmallIntegerField(default=0)

    class Meta(TenantScopedModel.Meta):
        abstract = False
        ordering = ["position", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["requirement_set", "kind"],
                name="unique_requirement_kind_per_set",
            )
        ]

    def __str__(self) -> str:
        suffix = "" if self.is_required else " (optional)"
        return f"{RequirementKind(self.kind).label}{suffix}"

    def save(self, *args, **kwargs):
        if self.requirement_set_id and self.school_id is None:
            self.school_id = self.requirement_set.school_id
        if self.requirement_set_id and self.branch_id is None:
            self.branch_id = self.requirement_set.branch_id
        super().save(*args, **kwargs)


# ===========================================================================
# The applicant
# ===========================================================================


def document_upload_to(instance: "Applicant", filename: str) -> str:
    """One folder per school, then per applicant, keyed by the reference.

    The reference is what staff quote on the phone, so a support request that
    starts "ENQ-2026-0043 sent the wrong birth certificate" leads straight to
    the folder.
    """
    return (
        f"admissions/{instance.school_id or 'unassigned'}/"
        f"{instance.reference or instance.pk or 'draft'}/{filename}"
    )


#: What a reference looks like: ENQ-2026-0001, numbered per school per year.
REFERENCE_PREFIX = "ENQ"


class ApplicantQuerySet(TenantQuerySet):
    """The questions the pipeline board and the nightly commands ask."""

    def open(self):
        return self.filter(status__in=OPEN_STATUSES)

    def closed(self):
        return self.filter(status__in=CLOSED_STATUSES)

    def at_enquiry(self):
        return self.filter(status=ApplicantStatus.ENQUIRY)

    def lapsed(self, now=None):
        """Enquiries whose window has closed but which are still marked open.

        Only enquiries: once a family has filled in the application the window
        has done its job, and expiring somebody mid-assessment would be the
        platform closing a door the school is holding open.
        """
        return self.at_enquiry().filter(expires_at__lte=now or timezone.now())

    def needing_reminder(self, now=None):
        """Live enquiries past the nudge date that have not been nudged yet.

        The cut-off is per school, so this cannot be one ``filter`` -- the
        command walks schools and applies each one's own window.
        """
        return self.at_enquiry().filter(
            reminder_sent_at__isnull=True,
            parent_email__gt="",
            expires_at__gt=now or timezone.now(),
        )


class ApplicantManager(TenantManager.from_queryset(ApplicantQuerySet)):
    """Auto-scoped, with the pipeline helpers above.

    This is what makes "one school never sees another's applicants" a property
    of the model layer: ``Applicant.objects.all()`` in a view is already
    narrowed to the caller's school and, for a principal, their campus.
    """


class ApplicantUnscopedManager(UnscopedManager.from_queryset(ApplicantQuerySet)):
    """Every applicant, regardless of caller.

    The public enquiry form and the nightly commands read through this, because
    neither runs as a signed-in user and the scoped manager would hand both an
    empty queryset. Every such use is spelled ``all_objects`` at the call site.
    """


class Applicant(BranchScopedModel):
    """One prospective child, from first enquiry to enrolment or expiry.

    Branch-owned like the roster: an applicant belongs to the campus they
    enquired about, and a principal's pipeline is narrowed to their own without
    a single view filtering for it.
    """

    branch = models.ForeignKey(
        "schools.Branch", on_delete=models.CASCADE, related_name="applicants"
    )

    reference = models.CharField(
        max_length=24,
        blank=True,
        help_text="Generated. What staff and parents quote on the phone.",
    )
    status = models.CharField(
        max_length=20,
        choices=ApplicantStatus.choices,
        default=ApplicantStatus.ENQUIRY,
        db_index=True,
    )
    source = models.CharField(
        max_length=20, choices=ApplicantSource.choices, default=ApplicantSource.ONLINE
    )

    # --- Captured at enquiry, carried forward untouched --------------------
    first_name = models.CharField(max_length=80)
    last_name = models.CharField("surname", max_length=80)
    other_names = models.CharField(max_length=120, blank=True)
    sex = models.CharField(max_length=10, choices=Sex.choices)
    date_of_birth = models.DateField(null=True, blank=True)

    #: The band, stored in its own right rather than only read off the class.
    #: It is what decides the requirements, and it has to survive a class being
    #: renamed, deactivated or deleted underneath a live applicant.
    level = models.PositiveSmallIntegerField(
        choices=Level.choices,
        help_text="The band applied for. Decides which requirements apply.",
    )
    school_class = models.ForeignKey(
        "academics.Class",
        # SET_NULL, not PROTECT: a school reorganising its classes must not be
        # blocked by an enquiry from last term, and `level` keeps the answer to
        # "what were they applying for?" even when the class is gone.
        on_delete=models.SET_NULL,
        related_name="applicants",
        null=True,
        blank=True,
        verbose_name="class applied for",
    )

    parent_name = models.CharField("parent / guardian name", max_length=160)
    parent_phone = models.CharField(
        "parent / guardian phone",
        max_length=20,
        validators=[validate_phone],
        help_text="Mobile number, e.g. 0803 123 4567.",
    )
    parent_email = models.EmailField(
        "parent / guardian email",
        blank=True,
        help_text="Where the reminder and the decision are sent. Without one, "
        "neither can go out.",
    )
    #: Documents handed over on paper and ticked off at the desk, as
    #: requirement kinds. A paper birth certificate seen at the counter meets
    #: the requirement as surely as a scan of it, and the walk-in screen does
    #: not make anyone scan it. Null rather than an empty-list default so the
    #: column can land before the code that writes it.
    requirements_received = models.JSONField(null=True, blank=True)
    previous_school = models.CharField(max_length=200, blank=True)
    heard_about = models.CharField(
        "how they heard about the school",
        max_length=32,
        choices=HeardAbout.choices,
        blank=True,
    )

    # --- The validity window ----------------------------------------------
    expires_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Set from the school's admissions settings when the enquiry "
        "is first saved.",
    )
    reminder_sent_at = models.DateTimeField(null=True, blank=True)

    # --- Added at the application stage ------------------------------------
    # Nothing above is asked for a second time. These are the fields the
    # application *adds*, which is the whole point of one record with stages.
    applied_at = models.DateTimeField(null=True, blank=True)
    address = models.TextField(blank=True)
    nationality = models.CharField(max_length=80, blank=True)
    state_of_origin = models.CharField(max_length=80, blank=True)
    parent_occupation = models.CharField(max_length=120, blank=True)
    notes = models.TextField(
        blank=True,
        help_text="Anything the school should know -- allergies, a sibling "
        "already here, learning support.",
    )

    passport_photo = models.FileField(
        upload_to=document_upload_to,
        validators=[validate_photo],
        null=True,
        blank=True,
    )
    birth_certificate = models.FileField(
        upload_to=document_upload_to,
        validators=[validate_document],
        null=True,
        blank=True,
    )
    previous_results = models.FileField(
        "previous school results",
        upload_to=document_upload_to,
        validators=[validate_document],
        null=True,
        blank=True,
    )
    transfer_letter = models.FileField(
        "transfer letter / testimonial",
        upload_to=document_upload_to,
        validators=[validate_document],
        null=True,
        blank=True,
    )
    immunisation_record = models.FileField(
        upload_to=document_upload_to,
        validators=[validate_document],
        null=True,
        blank=True,
    )

    # --- Decision -----------------------------------------------------------
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="admission_decisions",
        null=True,
        blank=True,
    )
    decision_note = models.TextField(
        blank=True, help_text="The reason, in the school's own words."
    )
    decision_sent_at = models.DateTimeField(
        null=True, blank=True, help_text="When the parent was emailed the outcome."
    )

    # --- Conversion ---------------------------------------------------------
    student = models.OneToOneField(
        "students.Student",
        on_delete=models.SET_NULL,
        related_name="applicant",
        null=True,
        blank=True,
        help_text="Who this applicant became. Set once, at enrolment.",
    )
    enrolled_at = models.DateTimeField(null=True, blank=True)

    # --- Closed without enrolling -------------------------------------------
    expired_at = models.DateTimeField(null=True, blank=True)
    withdrawn_at = models.DateTimeField(null=True, blank=True)
    closing_note = models.CharField(max_length=250, blank=True)

    objects = ApplicantManager()
    all_objects = ApplicantUnscopedManager()

    class Meta(BranchScopedModel.Meta):
        abstract = False
        # Newest first: an admissions officer works the top of the pile.
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["school", "reference"],
                name="unique_applicant_reference_per_school",
                violation_error_message=(
                    "Another applicant at this school already has that reference."
                ),
            )
        ]
        indexes = [
            models.Index(fields=["school", "branch"]),
            models.Index(fields=["branch", "status"]),
            models.Index(fields=["status", "expires_at"]),
            models.Index(fields=["last_name", "first_name"]),
        ]

    def __str__(self) -> str:
        return f"{self.full_name} ({self.reference or 'unsaved'})"

    def get_absolute_url(self) -> str:
        return reverse("admissions:applicant_detail", args=[self.pk])

    # -- names, borrowed wholesale from the roster ---------------------------
    # Same three properties as Student, and deliberately identical: a name
    # renders the same way on the pipeline board as it will on the register the
    # week after enrolment.

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def formal_name(self) -> str:
        given = " ".join(part for part in (self.first_name, self.other_names) if part)
        return f"{self.last_name}, {given}".strip().rstrip(",")

    @property
    def initials(self) -> str:
        return f"{self.first_name[:1]}{self.last_name[:1]}".upper() or "?"

    @property
    def age(self) -> int | None:
        if not self.date_of_birth:
            return None
        today = timezone.localdate()
        had_birthday = (today.month, today.day) >= (
            self.date_of_birth.month,
            self.date_of_birth.day,
        )
        return today.year - self.date_of_birth.year - (0 if had_birthday else 1)

    @property
    def parent_phone_display(self) -> str:
        return format_phone(self.parent_phone)

    @property
    def parent_phone_link(self) -> str:
        return f"tel:{to_international(self.parent_phone)}" if self.parent_phone else ""

    @property
    def whatsapp_link(self) -> str:
        """Half of these families are reached on WhatsApp, not by phone."""
        if not self.parent_phone:
            return ""
        return f"https://wa.me/{to_international(self.parent_phone).lstrip('+')}"

    @property
    def level_label(self) -> str:
        try:
            return Level(self.level).label
        except ValueError:
            return "Unknown"

    @property
    def applying_for(self) -> str:
        """"Primary 3" when a class was chosen, "Primary" when only a band was."""
        if self.school_class_id and self.school_class:
            return self.school_class.display_name
        return self.level_label

    # -- where they stand ----------------------------------------------------

    @property
    def status_label(self) -> str:
        return ApplicantStatus(self.status).label

    @property
    def status_pill(self) -> str:
        return STATUS_PILLS.get(self.status, "status-unpaid")

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    @property
    def is_closed(self) -> bool:
        return self.status in CLOSED_STATUSES

    @property
    def has_lapsed(self) -> bool:
        """Past its window and still sitting at enquiry.

        Derived, not stored, so a board is honest the moment the window closes
        rather than only after the nightly command has run. The command writes
        the status so that reports and history agree; this property is what the
        screen in front of a person shows in the meantime.
        """
        if self.status != ApplicantStatus.ENQUIRY or self.expires_at is None:
            return False
        return self.expires_at <= timezone.now()

    @property
    def days_left(self) -> int | None:
        """Whole days until the window closes; negative once it has, ``None``
        when the window no longer applies."""
        if self.status != ApplicantStatus.ENQUIRY or self.expires_at is None:
            return None
        return (self.expires_at - timezone.now()).days

    # -- requirements --------------------------------------------------------

    @property
    def requirement_profile(self):
        """The requirements in force for this applicant's level.

        Cached per instance: the detail screen, the application form and the
        decision guard all ask, and the answer cannot change mid-request.
        """
        if not hasattr(self, "_requirement_profile"):
            from .requirements import profile_for_applicant

            self._requirement_profile = profile_for_applicant(self)
        return self._requirement_profile

    @property
    def requires_assessment(self) -> bool:
        """False for Nursery and KG, which is what "skips the exam" means."""
        return self.requirement_profile.requires_assessment

    @property
    def has_sat_assessment(self) -> bool:
        assessment = getattr(self, "assessment", None)
        return assessment is not None and assessment.has_been_sat

    @property
    def missing_requirements(self):
        return self.requirement_profile.missing_for(self)

    @property
    def requirement_checklist(self):
        """Every requirement paired with whether it has been satisfied.

        What the detail screen ticks off. Built in
        :mod:`apps.admissions.requirements` so the map from a requirement to
        the field that satisfies it lives with the requirements rather than in
        a template.
        """
        return self.requirement_profile.checklist_for(self)

    @property
    def is_complete(self) -> bool:
        """Nothing compulsory is outstanding, so a decision can be made."""
        return not self.missing_requirements

    # -- admission money -----------------------------------------------------

    @property
    def fee_schedule(self) -> "AdmissionFeeSchedule | None":
        """The admission fee set for the class applied for, if one is priced."""
        if not self.school_class_id:
            return None
        return (
            AdmissionFeeSchedule.all_objects.filter(school_class_id=self.school_class_id)
            .prefetch_related("items")
            .first()
        )

    @property
    def admission_due(self) -> Decimal:
        """What coming in costs: the compulsory items of the class's schedule.

        Never stored on the applicant. A stored figure is a second source of
        truth that goes stale the first time the intake is repriced -- the same
        rule the termly fee structure follows.
        """
        schedule = self.fee_schedule
        return schedule.compulsory_total if schedule else ZERO

    @property
    def admission_paid(self) -> Decimal:
        """Confirmed admission money only. A pending receipt is not money yet."""
        total = self.payments.filter(
            status=AdmissionPaymentStatus.CONFIRMED
        ).aggregate(total=models.Sum("amount"))["total"]
        return total or ZERO

    @property
    def admission_outstanding(self) -> Decimal:
        """Never negative: an overpayment is a credit, not a negative debt."""
        return max(self.admission_due - self.admission_paid, ZERO)

    @property
    def has_paid_admission(self) -> bool:
        """True once the admission fee itself is covered.

        The gate on enrolment is the *admission fee*, not the whole intake
        bill: a school admits a child who has paid to come in and then bills
        the uniform and the books like any other charge. Where no schedule is
        priced, any confirmed payment counts -- refusing to enrol because
        nobody has set up the fee sheet would punish the family for the
        school's setup.
        """
        schedule = self.fee_schedule
        if schedule is None:
            return self.admission_paid > ZERO
        target = schedule.admission_total or schedule.compulsory_total
        return self.admission_paid >= target if target > ZERO else True

    # -- persistence ---------------------------------------------------------

    def save(self, *args, **kwargs):
        # A platform owner has no school of their own, and the public form has
        # no tenant context at all -- the branch always knows the school.
        if self.branch_id and self.school_id is None:
            self.school_id = self.branch.school_id
        if self.school_class_id and not self.level:
            self.level = self.school_class.level
        if self.parent_phone:
            self.parent_phone = normalise_phone(self.parent_phone)
        if self.expires_at is None:
            self.expires_at = self._default_expiry()
        if self.reference:
            return super().save(*args, **kwargs)
        return self._save_with_new_reference(*args, **kwargs)

    def _default_expiry(self):
        config = AdmissionsConfig.for_school(self.school_id)
        return timezone.now() + timedelta(days=config.enquiry_validity_days)

    def _save_with_new_reference(self, *args, **kwargs):
        """Allocate a reference, retrying if two enquiries land together.

        The public form is exactly where that happens -- a school posts its
        link to Instagram and gets six submissions in the same minute. The
        unique constraint is what actually guarantees correctness; this loop is
        what stops it turning into an error page for the parent.
        """
        for attempt in range(5):
            self.reference = self._next_reference()
            try:
                with transaction.atomic():
                    return super().save(*args, **kwargs)
            except IntegrityError:
                if attempt == 4:
                    raise
                # Re-read the highest reference and try the next one. Force an
                # INSERT on the retry rather than an UPDATE of a pk that the
                # failed attempt never wrote.
                kwargs.pop("force_insert", None)
                self.pk = None

    def _next_reference(self) -> str:
        year = timezone.localdate().year
        prefix = f"{REFERENCE_PREFIX}-{year}-"
        # all_objects: an anonymous parent has no tenant context, and a scoped
        # manager would see no existing references and hand out 0001 forever.
        last = (
            Applicant.all_objects.filter(
                school_id=self.school_id, reference__startswith=prefix
            )
            .order_by("-reference")
            .values_list("reference", flat=True)
            .first()
        )
        try:
            counter = int(last.rsplit("-", 1)[1]) + 1 if last else 1
        except (ValueError, IndexError):
            counter = 1
        return f"{prefix}{counter:04d}"

    def clean(self):
        super().clean()
        if self.school_class_id and self.branch_id:
            if self.school_class.branch_id != self.branch_id:
                raise ValidationError(
                    {"school_class": "That class belongs to a different branch."}
                )
        if self.date_of_birth and self.date_of_birth > timezone.localdate():
            raise ValidationError({"date_of_birth": "That date is in the future."})


# ===========================================================================
# Assessment
# ===========================================================================


class AssessmentOutcome(models.TextChoices):
    PENDING = "pending", "Not yet sat"
    PASSED = "passed", "Passed"
    FAILED = "failed", "Did not pass"
    ABSENT = "absent", "Did not attend"


class Assessment(BranchScopedModel):
    """The entrance examination, for the levels that sit one.

    A separate row rather than three more columns on the applicant, because it
    genuinely does not exist for a Nursery applicant -- and a nullable score,
    a nullable date and a nullable outcome on every applicant would say the
    opposite. ``requirement_profile.requires_assessment`` decides whether one
    is expected; the screens never create one for a level that does not.
    """

    branch = models.ForeignKey(
        "schools.Branch", on_delete=models.CASCADE, related_name="admission_assessments"
    )
    applicant = models.OneToOneField(
        Applicant, on_delete=models.CASCADE, related_name="assessment"
    )
    scheduled_for = models.DateField(
        null=True, blank=True, help_text="When the applicant is due to sit it."
    )
    sat_on = models.DateField(null=True, blank=True)
    score = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(ZERO)],
    )
    max_score = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        default=Decimal("100"),
        validators=[MinValueValidator(Decimal("1"))],
    )
    outcome = models.CharField(
        max_length=20,
        choices=AssessmentOutcome.choices,
        default=AssessmentOutcome.PENDING,
    )
    notes = models.TextField(blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="admission_assessments_recorded",
        null=True,
        blank=True,
    )

    class Meta(BranchScopedModel.Meta):
        abstract = False
        ordering = ["-scheduled_for", "-id"]
        indexes = [
            models.Index(fields=["school", "branch"]),
            models.Index(fields=["branch", "outcome"]),
        ]

    def __str__(self) -> str:
        return f"Assessment for {self.applicant.full_name}"

    @property
    def has_been_sat(self) -> bool:
        """Sat means judged: a scheduled date on its own is not a result."""
        return self.outcome != AssessmentOutcome.PENDING

    @property
    def percentage(self) -> Decimal | None:
        if self.score is None or not self.max_score:
            return None
        return (self.score / self.max_score * 100).quantize(Decimal("0.1"))

    def save(self, *args, **kwargs):
        if self.applicant_id and self.branch_id is None:
            self.branch_id = self.applicant.branch_id
        super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        if self.score is not None and self.max_score and self.score > self.max_score:
            raise ValidationError(
                {"score": f"The score cannot be more than {self.max_score:g}."}
            )


# ===========================================================================
# Admission fees -- separate from anything termly
# ===========================================================================


class AdmissionFeeKind(models.TextChoices):
    """What sort of charge a line is.

    Enrolment is gated on the ADMISSION line alone, not on the whole bill, and
    an all-in payment is allocated to it first (see :mod:`apps.admissions.split`).
    BOOKS is kept for a school that chooses to sell a book pack at the door; the
    seeded sheets carry none, because the termly structure bills books.
    """

    ADMISSION = "admission", "Admission fee"
    INTAKE = "intake", "Intake item"
    BOOKS = "books", "Compulsory books"


class AdmissionFeeSchedule(BranchScopedModel):
    """What one class charges a family to come in, for one intake year.

    Not a ``FeeStructure``, and never derived from one. A fee structure is what
    a class owes *per term* and is repriced every term; this is charged once,
    at the door, and holds only items -- uniform, tracksuit, the admission fee
    itself -- that a returning child does not pay again. Sharing a table would mean every
    termly total silently gaining an admission fee.
    """

    branch = models.ForeignKey(
        "schools.Branch",
        on_delete=models.CASCADE,
        related_name="admission_fee_schedules",
    )
    school_class = models.ForeignKey(
        "academics.Class",
        on_delete=models.CASCADE,
        related_name="admission_fee_schedules",
        verbose_name="class",
    )
    academic_year = models.CharField(max_length=20, help_text='e.g. "2025/2026".')
    is_active = models.BooleanField(default=True)
    note = models.CharField(max_length=250, blank=True)

    class Meta(BranchScopedModel.Meta):
        abstract = False
        verbose_name = "admission fee schedule"
        ordering = [
            "-academic_year",
            "school_class__level",
            "school_class__year_in_level",
            "school_class__stream",
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["school_class", "academic_year"],
                name="one_admission_schedule_per_class_year",
            )
        ]
        indexes = [
            models.Index(fields=["school", "branch"]),
            models.Index(fields=["branch", "academic_year"]),
        ]

    def __str__(self) -> str:
        return f"{self.school_class} admission fees ({self.academic_year})"

    def _sum(self, *kinds: str) -> Decimal:
        """Summed in Python, not by the database.

        Every caller has already prefetched ``items`` -- the seeder, the
        detail screen and the enrolment guard all read several totals off the
        same schedule -- so one round trip answers all of them.
        """
        return sum(
            (item.amount for item in self.items.all() if item.kind in kinds), ZERO
        )

    @property
    def admission_total(self) -> Decimal:
        """The one-time admission fee alone. What enrolment is gated on."""
        return self._sum(AdmissionFeeKind.ADMISSION)

    @property
    def intake_total(self) -> Decimal:
        return self._sum(AdmissionFeeKind.INTAKE)

    @property
    def books_total(self) -> Decimal:
        return self._sum(AdmissionFeeKind.BOOKS)

    @property
    def compulsory_total(self) -> Decimal:
        """Admission plus intake -- the sheet's own total line."""
        return self._sum(AdmissionFeeKind.ADMISSION, AdmissionFeeKind.INTAKE)

    @property
    def total(self) -> Decimal:
        """Everything, books included. Computed every time, stored never."""
        return sum((item.amount for item in self.items.all()), ZERO)

    def save(self, *args, **kwargs):
        if self.school_class_id and self.branch_id is None:
            self.branch_id = self.school_class.branch_id
        super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        if self.school_class_id and self.branch_id:
            if self.school_class.branch_id != self.branch_id:
                raise ValidationError(
                    {"school_class": "That class belongs to a different branch."}
                )


class AdmissionFeeItem(BranchScopedModel):
    """One line of an admission fee schedule, e.g. "Tracksuit" at 12,000."""

    branch = models.ForeignKey(
        "schools.Branch",
        on_delete=models.CASCADE,
        related_name="admission_fee_items",
    )
    schedule = models.ForeignKey(
        AdmissionFeeSchedule, on_delete=models.CASCADE, related_name="items"
    )
    name = models.CharField(max_length=120)
    amount = models.DecimalField(
        max_digits=12, decimal_places=2, validators=[MinValueValidator(ZERO)]
    )
    kind = models.CharField(
        max_length=20, choices=AdmissionFeeKind.choices, default=AdmissionFeeKind.INTAKE
    )
    position = models.PositiveSmallIntegerField(default=0)

    class Meta(BranchScopedModel.Meta):
        abstract = False
        ordering = ["kind", "position", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["schedule", "name"],
                name="unique_admission_item_name_per_schedule",
            )
        ]
        indexes = [
            models.Index(fields=["school", "branch"]),
            models.Index(fields=["schedule", "position"]),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.amount})"

    def save(self, *args, **kwargs):
        if self.schedule_id and self.branch_id is None:
            self.branch_id = self.schedule.branch_id
        super().save(*args, **kwargs)


class AdmissionPaymentStatus(models.TextChoices):
    """Deliberately the same three states as a termly payment.

    A bursar who has learned that pending means "handed in, not yet checked"
    on one screen must not have to learn something else on this one.
    """

    PENDING = "pending", "Pending"
    CONFIRMED = "confirmed", "Confirmed"
    VOID = "void", "Voided"


class AdmissionPaymentMethod(models.TextChoices):
    """The same four methods as ``payments.PaymentMethod``, declared again.

    Importing the enum would make admissions unusable without the payments app
    installed -- a coupling the dashboards already take care to avoid -- and
    the values are identical so a later report can union the two.
    """

    CASH = "cash", "Cash"
    TRANSFER = "transfer", "Bank transfer"
    POS = "pos", "POS"
    OTHER = "other", "Other"


class AdmissionPayment(BranchScopedModel):
    """Money received against an admission, before there is a student to bill.

    This is why admission money cannot live in ``payments.Payment``: that model
    requires a ``Student``, and the whole point of an admission fee is that it
    is paid by a family who does not have one yet. The two stay separate after
    enrolment too, so a term's collection figure never quietly includes intake
    money.
    """

    branch = models.ForeignKey(
        "schools.Branch", on_delete=models.CASCADE, related_name="admission_payments"
    )
    applicant = models.ForeignKey(
        Applicant, on_delete=models.CASCADE, related_name="payments"
    )
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    method = models.CharField(
        max_length=20,
        choices=AdmissionPaymentMethod.choices,
        default=AdmissionPaymentMethod.TRANSFER,
    )
    status = models.CharField(
        max_length=20,
        choices=AdmissionPaymentStatus.choices,
        default=AdmissionPaymentStatus.CONFIRMED,
    )
    received_on = models.DateField(default=timezone.localdate)
    reference = models.CharField(
        max_length=120, blank=True, help_text="Teller number, transfer reference."
    )
    note = models.CharField(max_length=250, blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="admission_payments_recorded",
        null=True,
        blank=True,
    )
    #: The term payment this row was paid together with, when a family paid
    #: intake and term fees in one lump. Set by :mod:`apps.admissions.split`,
    #: never by hand: the two rows are one sum handed over, divided between the
    #: two ledgers, and they share the term payment's receipt number.
    term_payment = models.ForeignKey(
        "payments.Payment",
        on_delete=models.SET_NULL,
        related_name="intake_portions",
        null=True,
        blank=True,
    )

    class Meta(BranchScopedModel.Meta):
        abstract = False
        ordering = ["-received_on", "-id"]
        indexes = [
            models.Index(fields=["school", "branch"]),
            models.Index(fields=["applicant", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.amount} from {self.applicant.parent_name}"

    @property
    def receipt_number(self) -> str:
        """The term payment's number when paid together with one, else its own.

        One sum handed over is one receipt, whichever ledger each part of it
        landed in.
        """
        if self.term_payment_id:
            return self.term_payment.receipt_number
        return f"ADM-{self.pk:06d}" if self.pk else "ADM-------"

    @property
    def status_pill(self) -> str:
        return {
            AdmissionPaymentStatus.PENDING: "status-partial",
            AdmissionPaymentStatus.CONFIRMED: "status-paid",
            AdmissionPaymentStatus.VOID: "status-unpaid",
        }.get(self.status, "status-unpaid")

    def save(self, *args, **kwargs):
        if self.applicant_id and self.branch_id is None:
            self.branch_id = self.applicant.branch_id
        super().save(*args, **kwargs)
