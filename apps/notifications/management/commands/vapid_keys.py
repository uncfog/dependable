"""Make the key pair Web Push needs, and print it once.

    python manage.py vapid_keys

Put the three lines it prints into the server's environment (on Railway, the
service's Variables). The private key is a secret: keep it there and nowhere
else. Changing it later means every device has to turn notifications on again.
"""

from __future__ import annotations

import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from django.core.management.base import BaseCommand


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class Command(BaseCommand):
    help = "Print a new VAPID key pair for Web Push."

    def handle(self, *args, **options):
        key = ec.generate_private_key(ec.SECP256R1())
        private = key.private_numbers().private_value.to_bytes(32, "big")
        public = key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        self.stdout.write(f"VAPID_PUBLIC_KEY={_b64(public)}")
        self.stdout.write(f"VAPID_PRIVATE_KEY={_b64(private)}")
        self.stdout.write("VAPID_SUBJECT=mailto:you@yourschooldomain")
        self.stdout.write(self.style.WARNING(
            "Keep VAPID_PRIVATE_KEY secret. Set all three in the server's environment."
        ))
