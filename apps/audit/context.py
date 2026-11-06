"""Who is acting right now, for whichever model save happens to be recorded.

A context variable for the same reason tenancy uses one: the save that writes an
audit event is many calls away from the request, and threading the user through
every form and service would mean one forgotten argument loses the record. Outside
a request there is no actor, and the event says it came from the system.
"""

from __future__ import annotations

from contextvars import ContextVar

from django.urls import reverse

from apps.core.ratelimit import client_ip

_request: ContextVar = ContextVar("audit_request", default=None)


def current_actor():
    """``(user or None, source, ip address)``, read when an event is written."""
    request = _request.get()
    if request is None:
        return None, "system", ""
    user = getattr(request, "user", None)
    user = user if user is not None and user.is_authenticated else None
    source = "admin" if request.path.startswith(reverse("admin:index")) else "web"
    return user, source, client_ip(request)[:45]


class AuditContextMiddleware:
    """Publish the request for the audit log. Follows AuthenticationMiddleware."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = _request.set(request)
        try:
            return self.get_response(request)
        finally:
            _request.reset(token)
