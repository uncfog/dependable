"""Deploy-time warnings for the things that fail silently at runtime.

Every check here exists because something was configured wrong and nothing said
so. The welcome email is the worked example: it is best-effort by design -- a
failed send must not turn a completed signup into an error page -- so a missing
``PUBLIC_BASE_URL`` or an absent Resend key produced a log line nobody was reading
and a school that never heard from us. "Best effort" has to mean the *request*
survives, not that the misconfiguration stays hidden.

``manage.py check --deploy`` runs these, and so does every ``manage.py`` command,
so a bad deploy says so on the way up rather than on the first signup.

Warnings rather than errors throughout: none of these stops the app serving pages,
and a check that refused to boot over an unsendable email would be a worse failure
than the one it is reporting.
"""

from __future__ import annotations

from django.conf import settings
from django.core.checks import Tags, Warning, register

#: Prefix for this project's own check ids, so they can be silenced individually
#: with SILENCED_SYSTEM_CHECKS.
_ID = "schoolcord"


def _is_production() -> bool:
    """Whether these checks have anything to complain about.

    DEBUG is the signal rather than the settings module's name: a developer
    running with DEBUG off to test something has the same problems, and a
    deployment with DEBUG on has worse ones than these.
    """
    return not settings.DEBUG


@register(Tags.urls)
def mail_links_need_a_public_address(app_configs, **kwargs):
    """Mail has no request to resolve a relative URL against.

    Without ``PUBLIC_BASE_URL`` the welcome email is not sent at all -- see
    ``apps.core.emails`` -- and the password-reset link would point nowhere, so
    the invitation flow stops working too.
    """
    if not _is_production() or (settings.PUBLIC_BASE_URL or "").strip():
        return []
    return [
        Warning(
            "PUBLIC_BASE_URL is not set, so emails cannot build links.",
            hint=(
                "Set it to the address people actually visit, e.g. "
                "PUBLIC_BASE_URL=https://www.theschoolcord.com. Until then the "
                "welcome email is skipped rather than sent with broken links, "
                "and a password-reset link has no host."
            ),
            id=f"{_ID}.W001",
        )
    ]


@register(Tags.compatibility)
def sending_mail_needs_a_key(app_configs, **kwargs):
    """Anymail raises on send with no API key, and the welcome email swallows it."""
    backend = getattr(settings, "EMAIL_BACKEND", "")
    if not _is_production() or "anymail" not in backend:
        return []
    if (getattr(settings, "ANYMAIL", {}) or {}).get("RESEND_API_KEY", "").strip():
        return []
    return [
        Warning(
            "EMAIL_BACKEND is Anymail/Resend but RESEND_API_KEY is empty.",
            hint=(
                "Set RESEND_API_KEY. Every send will otherwise fail -- the "
                "welcome email logs the failure and carries on, and a password "
                "reset reaches nobody."
            ),
            id=f"{_ID}.W002",
        )
    ]


@register(Tags.compatibility)
def the_sender_domain_has_to_be_verified(app_configs, **kwargs):
    """Resend refuses to send from a domain it has not verified.

    Not something a check can confirm -- only Resend knows -- so this only fires
    on the shape of mistake that is visible from here: a From address on a domain
    nobody has told us about.
    """
    if not _is_production():
        return []
    sender = (getattr(settings, "DEFAULT_FROM_EMAIL", "") or "")
    address = sender[sender.find("<") + 1: sender.find(">")] if "<" in sender else sender
    if "@" not in address:
        return [
            Warning(
                f"DEFAULT_FROM_EMAIL does not contain an address: {sender!r}",
                id=f"{_ID}.W003",
            )
        ]
    return []


@register(Tags.files)
def uploads_need_somewhere_to_live(app_configs, **kwargs):
    """The 404 that took two rounds to find.

    With no bucket configured, uploads go to the container's own disk -- which
    nothing serves in production (whitenoise covers ``static/`` only, and Django
    refuses to serve media with DEBUG off) and which does not survive a restart.
    So every school logo and every payment receipt 404s, and then disappears.
    """
    if not _is_production() or getattr(settings, "USE_OBJECT_STORAGE", False):
        return []
    return [
        Warning(
            "Uploads are being written to the local filesystem, which is not "
            "served in production and does not survive a redeploy.",
            hint=(
                "Set MEDIA_BUCKET_NAME and its four companions -- see "
                ".env.example -- to send school logos and payment receipts to "
                "object storage instead. Without them every uploaded file will "
                "404, and will be gone after the next deploy."
            ),
            id=f"{_ID}.W004",
        )
    ]


@register(Tags.compatibility)
def console_messaging_sends_nothing(app_configs, **kwargs):
    """The console provider marks every text delivered and sends none of them.

    Right for a developer and for a deployment waiting on Sender ID approval, but a
    school looking at that log believes its parents were told. It also writes
    parents' numbers and message text to the logs. So it has to be a known choice.
    """
    if not _is_production():
        return []
    if (getattr(settings, "MESSAGING_PROVIDER", "") or "").strip() != "console":
        return []
    return [
        Warning(
            "MESSAGING_PROVIDER is console: no SMS leaves the platform, every text "
            "is marked delivered, and parents' numbers go to the logs.",
            hint=(
                "Keep it only while no school is relying on messaging. To go live, "
                "set MESSAGING_PROVIDER to empty and TERMII_API_KEY to the real key."
            ),
            id=f"{_ID}.W006",
        )
    ]


@register(Tags.security)
def one_account_is_out_of_reach(app_configs, **kwargs):
    """Without ``PLATFORM_OWNER_EMAIL`` every platform owner can be deactivated.

    Including by each other -- which is how a platform locks itself out. The
    address is environment-only, so a deployment that never set it has no
    protected account, and this is the only place that says so.
    """
    if not _is_production() or (settings.PLATFORM_OWNER_EMAIL or "").strip():
        return []
    return [
        Warning(
            "PLATFORM_OWNER_EMAIL is not set, so no account is protected from "
            "deactivation.",
            hint=(
                "Set PLATFORM_OWNER_EMAIL to the email address of the platform "
                "owner account that must never be switched off."
            ),
            id=f"{_ID}.W005",
        )
    ]
