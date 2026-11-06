"""Error reporting: how a production failure reaches somebody.

Off unless ``SENTRY_DSN`` is set, so nothing leaves the platform until the owner
decides it should. This system holds children's records and parents' financial
details, so the settings below keep them out of error reports:

* ``send_default_pii=False`` -- no user details, cookies or client addresses;
* ``include_local_variables=False`` -- stack frames would otherwise carry whatever
  the code was holding, such as a student or a payment;
* ``max_request_body_size="never"`` -- no form contents;
* :func:`strip_request_data` -- no query strings, where a search for a child's name
  would otherwise travel.

An exception's own message can still mention what it was about, so keep names out
of messages raised in this codebase.
"""

from __future__ import annotations


def strip_request_data(event, hint):
    request = event.get("request")
    if request:
        for key in ("query_string", "data", "cookies", "env"):
            request.pop(key, None)
    return event


def options(dsn: str, environment: str) -> dict:
    return {
        "dsn": dsn,
        "environment": environment,
        "send_default_pii": False,
        "include_local_variables": False,
        "max_request_body_size": "never",
        # Errors only. Performance tracing would need its own privacy review.
        "traces_sample_rate": 0.0,
        "before_send": strip_request_data,
    }


def start(dsn: str, environment: str = "production") -> bool:
    """Start reporting if a DSN was given. Returns whether it did."""
    dsn = (dsn or "").strip()
    if not dsn:
        return False
    import sentry_sdk
    from sentry_sdk.integrations.django import DjangoIntegration

    sentry_sdk.init(integrations=[DjangoIntegration()], **options(dsn, environment))
    return True
