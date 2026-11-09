"""The daily reminders: what is still waiting, turned into notifications.

Run once a day by ``python manage.py run_reminders``. Every reminder carries a
dedupe key that names the day (or the term, or the 'Remind me'), so running it
twice, or after a missed day, tells nobody twice.

Runs without a signed-in user, so every query names its school and campus.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from django.db.models import Count
from django.urls import reverse
from django.utils import timezone

from apps.core.roles import Role

from .delivery import campus_staff, notify, owners
from .models import KEEP_DAYS, Kind, Notification, Reminder

#: A payment left unchecked this long gets a nudge.
UNCHECKED_AFTER = timedelta(hours=24)
#: How far ahead of a term's fee due date the school is told.
DUE_AHEAD_DAYS = 7
#: An enquiry still at "Enquiry" after this many days is waiting for the school.
ENQUIRY_WAIT_DAYS = 3


@dataclass
class Summary:
    notified: int = 0
    lines: list[str] = field(default_factory=list)

    def add(self, written, line: str):
        if written:
            self.notified += len(written)
            self.lines.append(f"{line}: told {len(written)}")


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def unchecked_payments(today: date, summary: Summary) -> None:
    from apps.payments.models import Payment, PaymentStatus, PaymentTransaction, TransactionKind

    cutoff = timezone.now() - UNCHECKED_AFTER
    counts: dict[tuple, int] = {}
    one_child = (
        Payment.all_objects.filter(status=PaymentStatus.PENDING, created_at__lte=cutoff)
        .exclude(transaction__kind=TransactionKind.FAMILY)
        .values("school_id", "branch_id").annotate(n=Count("id"))
    )
    shared = (
        PaymentTransaction.all_objects.filter(
            status=PaymentStatus.PENDING, kind=TransactionKind.FAMILY, created_at__lte=cutoff
        ).values("school_id", "branch_id").annotate(n=Count("id"))
    )
    for row in list(one_child) + list(shared):
        key = (row["school_id"], row["branch_id"])
        counts[key] = counts.get(key, 0) + row["n"]

    for (school_id, branch_id), count in counts.items():
        written = notify(
            campus_staff(school_id, branch_id),
            kind=Kind.REMINDER_UNCHECKED,
            title=f"{_plural(count, 'payment has', 'payments have')} waited more than a day to be checked",
            body="Check each one against the bank so it counts toward the balance.",
            url=reverse("payments:pending"), school_id=school_id, branch_id=branch_id,
            dedupe_key=f"unchecked:{branch_id}:{today.isoformat()}",
        )
        summary.add(written, f"unchecked payments at campus {branch_id}")


def fees_due_soon(today: date, summary: Summary) -> None:
    from apps.fees.models import Term
    from apps.payments.balances import outstanding
    from apps.students.models import Student, StudentStatus

    due_on = today + timedelta(days=DUE_AHEAD_DAYS)
    for term in Term.all_objects.filter(is_current=True, due_date=due_on).select_related("branch"):
        students = Student.all_objects.filter(
            branch_id=term.branch_id, status=StudentStatus.ACTIVE
        ).select_related("school_class", "branch")
        owing = len(outstanding(students))
        if not owing:
            continue
        written = notify(
            owners(term.school_id) + campus_staff(term.school_id, term.branch_id),
            kind=Kind.REMINDER_DUE,
            title=f"{term.name} fees are due on {due_on.day} {due_on:%b}",
            body=f"{_plural(owing, 'student still owes', 'students still owe')} at {term.branch.name}.",
            url=reverse("payments:outstanding"), school_id=term.school_id,
            branch_id=term.branch_id, dedupe_key=f"due:{term.pk}:{due_on.isoformat()}",
        )
        summary.add(written, f"fees due for term {term.pk}")


def waiting_enquiries(today: date, summary: Summary) -> None:
    from apps.admissions.models import Applicant, ApplicantStatus
    from apps.core.modules import enabled_for_school

    cutoff = timezone.now() - timedelta(days=ENQUIRY_WAIT_DAYS)
    rows = (
        Applicant.all_objects.filter(status=ApplicantStatus.ENQUIRY, created_at__lte=cutoff)
        .values("school_id", "branch_id").annotate(n=Count("id"))
    )
    enabled: dict = {}
    for row in rows:
        school_id, branch_id, count = row["school_id"], row["branch_id"], row["n"]
        if school_id not in enabled:
            enabled[school_id] = "admissions" in enabled_for_school(school_id)
        if not enabled[school_id]:
            continue
        written = notify(
            owners(school_id) + campus_staff(school_id, branch_id, roles=(Role.PRINCIPAL,)),
            kind=Kind.REMINDER_ENQUIRY,
            title=f"{_plural(count, 'enquiry has', 'enquiries have')} waited {ENQUIRY_WAIT_DAYS} days or more",
            body="A quick reply while the parent is still deciding.",
            url=reverse("admissions:pipeline"), school_id=school_id, branch_id=branch_id,
            dedupe_key=f"enquiry:{branch_id}:{today.isoformat()}",
        )
        summary.add(written, f"waiting enquiries at campus {branch_id}")


def personal_reminders(today: date, summary: Summary) -> None:
    due = Reminder.objects.filter(sent_at__isnull=True, due_on__lte=today).select_related("user")
    now = timezone.now()
    for reminder in due:
        written = notify(
            [reminder.user], kind=Kind.REMINDER_PERSONAL,
            title=f"Reminder: {reminder.target_label}",
            body=reminder.note or "You asked to be reminded today.",
            url=reminder.url, school_id=reminder.school_id,
            dedupe_key=f"reminder:{reminder.pk}",
        )
        # Sent, or the person's account is off: either way it is done.
        Reminder.objects.filter(pk=reminder.pk).update(sent_at=now)
        summary.add(written, f"reminder {reminder.pk}")


def tidy(today: date, summary: Summary) -> None:
    cutoff = timezone.now() - timedelta(days=KEEP_DAYS)
    gone, _ = Notification.objects.filter(created_at__lt=cutoff).delete()
    done, _ = Reminder.objects.filter(sent_at__lt=cutoff).delete()
    if gone or done:
        summary.lines.append(f"removed {gone} old notifications and {done} old reminders")


def run(today: date | None = None) -> Summary:
    today = today or timezone.localdate()
    summary = Summary()
    for step in (unchecked_payments, fees_due_soon, waiting_enquiries,
                 personal_reminders, tidy):
        step(today, summary)
    return summary
