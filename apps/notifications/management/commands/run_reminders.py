"""Turn what is still waiting into in-app reminders. Run once a day.

    python manage.py run_reminders
    python manage.py run_reminders --date 2026-11-20

Tells campus staff about payments left unchecked for a day, owners and
principals about fees due in a week and enquiries left waiting, and each person
about the 'Remind me' notes they set for today. Also clears notifications older
than 90 days. Safe to run more than once a day: nobody is told the same thing
twice. Everything stays inside the app.
"""

from __future__ import annotations

from datetime import date

from django.core.management.base import BaseCommand, CommandError

from apps.notifications.reminders import run


class Command(BaseCommand):
    help = "Write today's in-app reminders."

    def add_arguments(self, parser):
        parser.add_argument("--date", help="Run as if today were this date (YYYY-MM-DD).")

    def handle(self, *args, **options):
        today = None
        if options["date"]:
            try:
                today = date.fromisoformat(options["date"])
            except ValueError as error:
                raise CommandError("--date must be YYYY-MM-DD.") from error
        summary = run(today)
        for line in summary.lines:
            self.stdout.write(line)
        self.stdout.write(self.style.SUCCESS(f"Reminders done: {summary.notified} written."))
