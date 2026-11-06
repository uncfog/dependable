"""Clear a platform account's two-step sign-in after it has lost its phone.

The way back for someone with no phone and no recovery codes, and deliberately only
from a shell. A screen for this would be a way round the protection. They set it up
again at their next sign-in. Check who is asking before running it. It is recorded.

    python manage.py reset_platform_mfa --email you@theschoolcord.com
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.accounts.mfa_views import remove_devices


class Command(BaseCommand):
    help = "Clear a platform account's authenticator so it can be set up again."

    def add_arguments(self, parser):
        parser.add_argument("--email", default="")
        parser.add_argument("--username", default="")

    def handle(self, *args, **options):
        User = get_user_model()
        if options["username"]:
            matches = User.objects.filter(username=options["username"])
        elif options["email"]:
            matches = User.objects.filter(email__iexact=options["email"])
        else:
            raise CommandError("Name the account with --email or --username.")
        if matches.count() != 1:
            raise CommandError(f"Expected one account, found {matches.count()}.")

        user = matches.get()
        removed = remove_devices(user)
        self.stdout.write(
            f"Removed {removed} device(s) from {user.get_username()}. They set it up "
            f"again at their next sign-in."
        )
