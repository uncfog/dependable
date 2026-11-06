"""Limits on the doors anyone can knock on.

Counted in Django's cache. With no ``CACHES`` configured that is local memory, so
each gunicorn worker counts for itself and a restart forgets: with three workers
the true ceiling can be up to three times the number written here. That is enough
to make guessing passwords and flooding inboxes impractical, and it needs no new
table or service. Point ``CACHES`` at something shared when one exists and these
become exact, with no change here.

Cache keys are hashed, so email addresses and IP addresses are not left in the
cache in the clear.

``RATE_LIMITS_ENABLED`` switches every limit off. It is off under the test runner,
where hundreds of requests share one address and one process-wide cache. Tests of
the limits themselves turn it back on.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache

MINUTE = 60
HOUR = 60 * MINUTE


def client_ip(request) -> str:
    """Who is asking, as well as this deployment can tell.

    ``X-Real-IP`` first: Railway's edge sets it and replaces any copy the caller
    sent. Then the first ``X-Forwarded-For`` entry, then the socket, which is what a
    machine with no proxy in front of it has.

    The headers can be forged where no proxy overwrites them, so the limits that
    matter most also count by account or mailbox, which an attacker cannot vary at
    will. Limits by address alone are set high, because a whole school can share
    one address and a proxy fault could put many schools behind one.
    """
    real_ip = request.META.get("HTTP_X_REAL_IP", "").strip()
    if real_ip:
        return real_ip
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "unknown")


@dataclass(frozen=True)
class Limit:
    """No more than ``limit`` hits per key in any window of ``window`` seconds."""

    name: str
    limit: int
    window: int

    def _cache_key(self, key: str) -> str:
        digest = hashlib.sha256(key.encode()).hexdigest()
        return f"ratelimit:{self.name}:{digest}"

    def count(self, key: str) -> int:
        return cache.get(self._cache_key(key), 0)

    def is_exhausted(self, key: str) -> bool:
        return settings.RATE_LIMITS_ENABLED and self.count(key) >= self.limit

    def hit(self, key: str) -> None:
        if not settings.RATE_LIMITS_ENABLED:
            return
        cache_key = self._cache_key(key)
        # add() starts the clock on the first hit; incr() leaves it running.
        cache.add(cache_key, 0, self.window)
        try:
            cache.incr(cache_key)
        except ValueError:
            # Expired between the two calls: this is the first hit of a new window.
            cache.set(cache_key, 1, self.window)

    def allow(self, key: str) -> bool:
        """Count this request, and say whether it is within the limit."""
        if self.is_exhausted(key):
            return False
        self.hit(key)
        return True

    def clear(self, key: str) -> None:
        cache.delete(self._cache_key(key))


# --- Signing in ----------------------------------------------------------
# Counted on failures only. The first is the everyday limit. The second stops one
# account being guessed from many addresses, at the price that someone determined
# can keep that one account out for a while, so it is high and short-lived. The
# third stops one address trying many accounts, and allows for a school's office
# sharing an address.
LOGIN_PAIR = Limit("login-pair", 10, 15 * MINUTE)
LOGIN_ACCOUNT = Limit("login-account", 50, 15 * MINUTE)
LOGIN_CLIENT = Limit("login-client", 100, 15 * MINUTE)

# --- Other public doors ---------------------------------------------------
RESET_EMAIL = Limit("reset-email", 3, HOUR)
RESET_CLIENT = Limit("reset-client", 20, HOUR)
SIGNUP_CLIENT = Limit("signup-client", 20, HOUR)
ENQUIRY_CLIENT = Limit("enquiry-client", 20, HOUR)
MFA_VERIFY = Limit("mfa-verify", 10, 15 * MINUTE)


def _identifier(value) -> str:
    return str(value or "").strip().lower()


def _login_keys(request, identifier) -> tuple:
    identifier = _identifier(identifier)
    address = client_ip(request)
    return (
        (LOGIN_PAIR, f"{identifier}|{address}"),
        (LOGIN_ACCOUNT, identifier),
        (LOGIN_CLIENT, address),
    )


def login_is_blocked(request, identifier) -> bool:
    return any(limit.is_exhausted(key) for limit, key in _login_keys(request, identifier))


def record_login_failure(request, identifier) -> None:
    for limit, key in _login_keys(request, identifier):
        limit.hit(key)


def clear_login_failures(request, *identifiers) -> None:
    """A correct password wipes this person's own slate, not the address's."""
    for identifier in identifiers:
        limit, key = _login_keys(request, identifier)[0]
        limit.clear(key)


def allow_password_reset(request, email) -> bool:
    """Count one reset request. Both counts move, whichever one refuses."""
    within_mailbox = RESET_EMAIL.allow(_identifier(email))
    within_client = RESET_CLIENT.allow(client_ip(request))
    return within_mailbox and within_client
