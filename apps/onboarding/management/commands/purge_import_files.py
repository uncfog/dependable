"""Remove uploaded workbooks and their personal data once they're no longer needed.

What was imported stays. This removes the file and the copied rows behind it, from
imports finished more than ``--days`` ago, and deletes drafts nobody came back to.

    python manage.py purge_import_files --days 30
"""

from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from apps.onboarding.models import BatchStatus, ImportBatch


class Command(BaseCommand):
    help = "Remove old import files and their copied rows. Imported records are kept."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=30)

    def handle(self, *args, days, **options):
        cutoff = timezone.now() - timedelta(days=days)
        finished = ImportBatch.all_objects.filter(
            Q(status=BatchStatus.COMMITTED, committed_at__lt=cutoff)
            | Q(status=BatchStatus.UNDONE, undone_at__lt=cutoff)
        )
        cleared = 0
        for batch in finished:
            if batch.file:
                batch.file.delete(save=False)
                batch.file = ""
                batch.save(update_fields=["file", "updated_at"])
            batch.rows.update(raw={}, label="", messages=[])
            cleared += 1

        drafts = ImportBatch.all_objects.filter(status=BatchStatus.DRAFT, updated_at__lt=cutoff)
        dropped = 0
        for batch in drafts:
            if batch.file:
                batch.file.delete(save=False)
            batch.delete()
            dropped += 1
        self.stdout.write(f"Cleared {cleared} finished import(s), removed {dropped} old draft(s).")
