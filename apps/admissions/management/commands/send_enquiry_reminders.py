"""Email parents whose enquiry is running down, prompting them to continue.

    python manage.py send_enquiry_reminders
    python manage.py send_enquiry_reminders --dry-run

Meant for a nightly cron, alongside ``expire_enquiries``. Run this one first:
reminding somebody the morning their enquiry lapses is better than reminding
them the morning after.

The cut-off is per school -- one school holds an enquiry for three weeks and
nudges at day seven, another has its own numbers -- so this cannot be a single
``filter()``. The command walks the schools that have live enquiries and
applies each one's own :class:`~apps.admissions.models.AdmissionsConfig`.

``reminder_sent_at`` is stamped by the notifier and only on success, so a
message that failed to send is retried by tomorrow's run rather than being
silently marked done. One reminder per enquiry, ever: a second and third
identical email is how a school's mail starts being marked as spam.

Runs unscoped -- there is no signed-in user in a cron job.
"""

from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.admissions import notifications
from apps.admissions.models import AdmissionsConfig, Applicant
from apps.schools.models import School


class Command(BaseCommand):
    help = "Send the 'continue your application' reminder to parents."

    def add_arguments(self, parser):
        parser.add_argument(
            "--school",
            default="",
            help="Limit to one school by name. Default: every school.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List who would be emailed without sending anything.",
        )

    def handle(self, *args, **options):
        now = timezone.now()
        schools = School.all_objects.all()
        if options["school"]:
            schools = schools.filter(name=options["school"])
            if not schools.exists():
                self.stdout.write(
                    self.style.WARNING(f'No school named "{options["school"]}".')
                )
                return

        sent = failed = considered = 0

        for school in schools:
            # Its public page has gone dark, so a reminder would point a parent at
            # nothing. Past-due schools are skipped too, for the same reason.
            if not school.is_active:
                continue
            config = AdmissionsConfig.for_school(school)
            # "Old enough to nudge" is measured from when the enquiry came in,
            # so the cut-off is a created_at ceiling rather than a date on the
            # applicant.
            cutoff = now - timedelta(days=config.reminder_after_days)

            due = (
                Applicant.all_objects.needing_reminder(now)
                .filter(school=school, created_at__lte=cutoff)
                .select_related("school", "branch", "school_class")
            )

            for applicant in due:
                considered += 1
                days = applicant.days_left
                self.stdout.write(
                    f"  {applicant.reference:16} {applicant.full_name:28} "
                    f"{applicant.parent_email:32} "
                    f"({days} day{'' if days == 1 else 's'} left)"
                )
                if options["dry_run"]:
                    continue
                if notifications.send_enquiry_reminder(applicant):
                    sent += 1
                else:
                    failed += 1

        if not considered:
            self.stdout.write("No enquiries are due a reminder.")
            return

        if options["dry_run"]:
            self.stdout.write(
                self.style.WARNING(
                    f"\nDry run: {considered} reminder"
                    f"{'' if considered == 1 else 's'} would be sent."
                )
            )
            return

        self.stdout.write(
            self.style.SUCCESS(f"\nSent {sent} reminder{'' if sent == 1 else 's'}.")
        )
        if failed:
            self.stdout.write(
                self.style.WARNING(
                    f"{failed} could not be sent and will be retried on the "
                    f"next run -- reminder_sent_at is only stamped on success."
                )
            )
