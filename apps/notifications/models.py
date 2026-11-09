"""In-app notifications and the reminders that become them.

A notification belongs to one person and is only ever read through its
recipient (``Notification.for_user``), so it does not use the tenant-scoped
base classes: those fill in the *actor's* campus on save, and a notice about
North campus sent to the owner must not be stamped with whichever campus the
bursar who caused it works at. ``school`` and ``branch`` are stored for
display and clean-up, never for access.

Nothing here leaves the app: no email, no SMS, nothing to parents.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q


class Kind(models.TextChoices):
    PAYMENT_WAITING = "payment_waiting", "A payment is waiting to be checked"
    PAYMENT_RECORDED = "payment_recorded", "A payment was recorded and checked"
    PAYMENT_CHECKED = "payment_checked", "A payment was checked"
    PAYMENT_VOIDED = "payment_voided", "A payment was voided"
    IMPORT = "import", "Records were imported or an import was undone"
    STAFF = "staff", "Staff accounts were added or changed"
    SECURITY = "security", "Two-step sign-in was reset or a recovery code was used"
    OWNERSHIP = "ownership", "Ownership of the school"
    REMINDER_UNCHECKED = "reminder_unchecked", "Payments waiting more than a day"
    REMINDER_DUE = "reminder_due", "Fees due within a week"
    REMINDER_ENQUIRY = "reminder_enquiry", "Enquiries waiting for a reply"
    REMINDER_PERSONAL = "reminder_personal", "Your own reminders"


#: Always delivered: who touched your account, who owns the school, and the
#: reminders you set yourself. Everything else can be turned off.
ALWAYS_ON = frozenset({Kind.SECURITY, Kind.OWNERSHIP, Kind.REMINDER_PERSONAL})

REMINDER_KINDS = frozenset({
    Kind.REMINDER_UNCHECKED, Kind.REMINDER_DUE, Kind.REMINDER_ENQUIRY, Kind.REMINDER_PERSONAL,
})

#: How long a notification is kept, read or not.
KEEP_DAYS = 90


class NotificationQuerySet(models.QuerySet):
    def for_user(self, user):
        if user is None or not user.is_authenticated:
            return self.none()
        return self.filter(recipient=user)

    def unread(self):
        return self.filter(read_at__isnull=True)


class Notification(models.Model):
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications"
    )
    school = models.ForeignKey(
        "schools.School", on_delete=models.CASCADE, null=True, blank=True,
        related_name="notifications",
    )
    branch = models.ForeignKey(
        "schools.Branch", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="notifications",
    )
    kind = models.CharField(max_length=32, choices=Kind.choices)
    title = models.CharField(max_length=160)
    body = models.CharField(max_length=300, blank=True)
    #: A path inside the app. Opened only if it starts with "/".
    url = models.CharField(max_length=300, blank=True)
    #: The audit event this came from, when it came from one. A plain number, not
    #: a foreign key: a notice is a copy for one person, and must never stand in
    #: the way of whatever happens to the audit log's own rows.
    event_id = models.PositiveBigIntegerField(null=True, blank=True)
    #: Daily reminders carry one so a second run on the same day adds nothing.
    dedupe_key = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    read_at = models.DateTimeField(null=True, blank=True)

    objects = NotificationQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["recipient", "read_at"]),
            models.Index(fields=["recipient", "-created_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["recipient", "dedupe_key"],
                condition=~Q(dedupe_key=""),
                name="one_notification_per_dedupe_key",
            ),
        ]

    def __str__(self) -> str:
        return self.title

    @property
    def is_reminder(self) -> bool:
        return self.kind in REMINDER_KINDS

    @property
    def safe_url(self) -> str:
        """The link, if it stays inside the app. Never another site."""
        url = self.url or ""
        return url if url.startswith("/") and not url.startswith("//") else ""


class NotificationPreference(models.Model):
    """The kinds one person has turned off. No row means everything is on."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="notification_preference",
    )
    muted = models.JSONField(default=list, blank=True)

    def __str__(self) -> str:
        return f"Notification settings for {self.user}"

    def is_muted(self, kind: str) -> bool:
        return kind not in ALWAYS_ON and kind in (self.muted or [])


class ReminderTarget(models.TextChoices):
    STUDENT = "student", "Student"
    PAYMENT = "payment", "Payment"


class Reminder(models.Model):
    """'Remind me' on a student or a payment: a date and a note, for oneself.

    The daily run turns it into a notification on ``due_on`` and stamps
    ``sent_at``. It is never shown to anyone else.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="reminders"
    )
    school = models.ForeignKey(
        "schools.School", on_delete=models.CASCADE, related_name="reminders"
    )
    due_on = models.DateField()
    note = models.CharField(max_length=200, blank=True)
    target_type = models.CharField(max_length=16, choices=ReminderTarget.choices)
    target_id = models.PositiveIntegerField()
    target_label = models.CharField(max_length=160)
    url = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["due_on", "id"]
        indexes = [models.Index(fields=["sent_at", "due_on"])]

    def __str__(self) -> str:
        return f"{self.target_label} on {self.due_on:%d %b %Y}"


class PushSubscription(models.Model):
    """One browser or phone that agreed to show this person's notifications.

    Made by the browser when the person turns notifications on for a device
    (Notification settings), and removed when they turn it off, sign out of that
    browser, or the push service says the address is gone. ``endpoint`` is the
    push service's address for that browser; ``p256dh`` and ``auth`` are the
    keys the message is encrypted with, so only that browser can read it.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="push_subscriptions"
    )
    endpoint = models.CharField(max_length=500, unique=True)
    p256dh = models.CharField(max_length=200)
    auth = models.CharField(max_length=100)
    user_agent = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_sent_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"Push for {self.user} ({self.user_agent[:40] or 'a browser'})"
