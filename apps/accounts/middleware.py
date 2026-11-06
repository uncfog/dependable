"""Holds an account on a temporary password at Account settings until it picks its own."""

from __future__ import annotations

from urllib.parse import quote

from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse
from django_otp import user_has_device


class RequirePlatformMFAMiddleware:
    """Hold a platform account at its two-step code until this session has passed it.

    A middleware so the Django admin and every screen written later are covered. It
    follows the temporary-password hold, which goes first: a person sorting out a
    temporary password is on Account settings, and this must not send them away.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if (
            settings.MFA_REQUIRED_FOR_PLATFORM
            and user is not None
            and user.is_authenticated
            and user.is_platform_staff
            and not user.must_change_password
            and not user.is_verified()
            and not self._exempt(request.path_info)
        ):
            name = (
                "accounts:mfa_verify"
                if user_has_device(user, confirmed=True)
                else "accounts:mfa_setup"
            )
            target = reverse(name)
            if request.method == "GET":
                target += f"?next={quote(request.get_full_path(), safe='/')}"
            return redirect(target)
        return self.get_response(request)

    @staticmethod
    def _exempt(path: str) -> bool:
        allowed = (
            reverse("accounts:mfa_setup"),
            reverse("accounts:mfa_verify"),
            reverse("logout"),
            settings.STATIC_URL,
            settings.MEDIA_URL,
        )
        return any(path.startswith(prefix) for prefix in allowed if prefix)


class RequirePasswordChangeMiddleware:
    """Send an account with ``must_change_password`` to Account settings.

    Every screen redirects there, the admin included, except the few that
    choosing a password needs: the settings page itself, the generator behind
    its Generate button, signing out, the reset-by-email flow, and static files.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if (
            user is not None
            and user.is_authenticated
            and getattr(user, "must_change_password", False)
            and not self._exempt(request.path_info)
        ):
            # Say why the click went somewhere else. The settings page carries a
            # persistent notice about the state -- which is the right place for
            # something the user *must* act on -- but nothing there explains why
            # pressing "Students" landed them on Account settings.
            #
            # Safe to add here: MessageMiddleware is listed ahead of this one, so
            # `request._messages` exists. One message per redirect, and the
            # redirect renders the settings page immediately, which consumes it.
            messages.warning(
                request, "Choose your own password before carrying on."
            )
            return redirect("accounts:settings")
        return self.get_response(request)

    @staticmethod
    def _exempt(path: str) -> bool:
        allowed = (
            reverse("accounts:settings"),
            reverse("accounts:generate_password"),
            reverse("logout"),
            reverse("password_reset"),
            # /accounts/reset/<uid>/<token>/ and /accounts/reset/done/.
            reverse("password_reset_complete").removesuffix("done/"),
            settings.STATIC_URL,
            settings.MEDIA_URL,
        )
        return any(path.startswith(prefix) for prefix in allowed if prefix)
