"""Start an ownership transfer for a school whose owner cannot do it themselves.

    python manage.py transfer_school_ownership --school fulfilled-academy \\
        --email new.owner@example.com --name "Their Name"

    python manage.py transfer_school_ownership --school fulfilled-academy \\
        --email new.owner@example.com --name "Their Name" \\
        --old-owner principal --branch "Main Campus"

    python manage.py transfer_school_ownership --school fulfilled-academy --cancel

For an owner who has died, left, or lost the email they sign in with. It starts
the same transfer Account settings does, and the new owner still accepts from the
emailed link: nobody running this sets or learns their password. When they accept,
the current owner's account is switched off (the default) or, with
``--old-owner principal``, made principal of ``--branch``.

Run where outgoing mail is configured. The link points at PUBLIC_BASE_URL, or at
``--base-url``.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.accounts.models import OwnershipTransfer
from apps.accounts.ownership import (
    TransferError,
    cancel_transfer,
    current_owners,
    open_transfer,
    start_transfer,
)
from apps.schools.models import Branch, School

Afterwards = OwnershipTransfer.Afterwards


class Command(BaseCommand):
    help = "Start (or cancel) an emailed ownership transfer for a school."

    def add_arguments(self, parser):
        parser.add_argument("--school", required=True, help="The school's slug.")
        parser.add_argument("--email", help="The new owner's email address.")
        parser.add_argument("--name", help='The new owner\'s full name, e.g. "Ada Obi".')
        parser.add_argument(
            "--old-owner", choices=["leave", "principal"], default="leave",
            help="What happens to the current owner's account when the transfer is accepted.",
        )
        parser.add_argument("--branch", help='The campus name, e.g. "Main Campus", with --old-owner principal.')
        parser.add_argument("--base-url", help="The site the link points at. Default: PUBLIC_BASE_URL.")
        parser.add_argument("--cancel", action="store_true", help="Cancel the school's waiting transfer.")

    def handle(self, *args, **options):
        school = School.all_objects.filter(slug=options["school"]).first()
        if school is None:
            raise CommandError(f"No school with the slug {options['school']!r}.")

        if options["cancel"]:
            transfer = open_transfer(school)
            if transfer is None:
                raise CommandError(f"{school.name} has no transfer waiting.")
            cancel_transfer(transfer, by=None)
            self.stdout.write(self.style.SUCCESS(
                f"Cancelled the transfer to {transfer.to_email}. Its link no longer works."
            ))
            return

        if not options["email"] or not options["name"]:
            raise CommandError("--email and --name are needed to start a transfer.")

        afterwards, branch = Afterwards.LEAVE, None
        if options["old_owner"] == "principal":
            if not options["branch"]:
                raise CommandError("--old-owner principal needs --branch.")
            branch = Branch.all_objects.filter(
                school=school, is_active=True, name__iexact=options["branch"]
            ).first()
            if branch is None:
                raise CommandError(f"{school.name} has no open campus called {options['branch']!r}.")
            afterwards = Afterwards.STAY_PRINCIPAL

        try:
            started = start_transfer(
                school=school,
                email=options["email"],
                name=options["name"],
                afterwards=afterwards,
                stay_branch=branch,
                started_by=None,
                origin=options["base_url"],
            )
        except TransferError as error:
            raise CommandError(str(error))

        owners = ", ".join(o.get_username() for o in current_owners(school)) or "nobody"
        self.stdout.write(self.style.SUCCESS(
            f"Transfer of {school.name} to {started.transfer.to_email} started. "
            f"Current owner: {owners}."
        ))
        if started.emailed:
            self.stdout.write(
                f"The link was emailed. It works once, until "
                f"{started.transfer.expires_at:%d %b %Y %H:%M} UTC."
            )
        else:
            self.stdout.write(self.style.WARNING(
                "The email could not be sent (is PUBLIC_BASE_URL or --base-url set, and "
                "is mail configured?). Cancel with --cancel and try again."
            ))
