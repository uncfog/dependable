"""Authenticator-app sign-in for platform accounts.

A platform account can reach every school's children and money, so a stolen password
alone must not be enough. ``django-otp`` provides the codes and their throttling, and
these screens are the enrolment and sign-in around it.

Only platform accounts for now. Others can be added by widening
``RequirePlatformMFAMiddleware`` and the check here.
"""

from __future__ import annotations

from base64 import b32encode

import segno
from django import forms
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.safestring import mark_safe
from django.views.decorators.cache import never_cache
from django.views.generic import View
from django_otp import devices_for_user, login as otp_login, match_token, user_has_device
from django_otp.plugins.otp_static.models import StaticDevice, StaticToken
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.audit.models import Category
from apps.audit.recording import record
from apps.core import ratelimit
from apps.core.forms import StyledFormMixin
from apps.core.navigation import home_url_for

RECOVERY_CODES = 10


class TokenForm(StyledFormMixin, forms.Form):
    token = forms.CharField(
        label="Code",
        max_length=16,
        widget=forms.TextInput(
            attrs={"autocomplete": "one-time-code", "autofocus": True}
        ),
    )


def record_mfa(user, action: str) -> None:
    record(
        action=action,
        category=Category.ACCESS,
        target_type="accounts.User",
        target_id=user.pk,
        target_label=user.get_username(),
        school_id=user.school_id,
    )


def _next_url(request) -> str:
    wanted = request.POST.get("next") or request.GET.get("next") or ""
    if wanted and url_has_allowed_host_and_scheme(
        wanted, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return wanted
    return home_url_for(request.user)


def _quiet_tour(user) -> dict:
    """The guided tour must not open over a screen holding someone at a gate."""
    return {"role": user.role or "", "userId": user.pk, "autoStart": False}


@method_decorator(never_cache, name="dispatch")
class MFAView(LoginRequiredMixin, View):
    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not request.user.is_platform_staff:
            raise PermissionDenied("Two-step sign-in is for platform accounts.")
        return super().dispatch(request, *args, **kwargs)


class MFASetupView(MFAView):
    """Scan the code, prove it works, and take the recovery codes away."""

    template_name = "accounts/mfa_setup.html"

    def _already_enrolled(self, request):
        # Replacing a device takes the reset command. Otherwise whoever holds a
        # stolen password could swap in their own authenticator from this page.
        if not user_has_device(request.user, confirmed=True):
            return None
        if request.user.is_verified():
            return HttpResponseRedirect(_next_url(request))
        return HttpResponseRedirect(reverse("accounts:mfa_verify"))

    def _device(self, user) -> TOTPDevice:
        device = TOTPDevice.objects.filter(user=user, confirmed=False).first()
        return device or TOTPDevice.objects.create(
            user=user, name="Authenticator app", confirmed=False
        )

    def _render(self, request, device, form, status=200):
        qr = segno.make(device.config_url, error="m")
        return render(
            request,
            self.template_name,
            {
                "form": form,
                "qr_svg": mark_safe(qr.svg_inline(scale=5, border=2)),
                "config_url": device.config_url,
                "secret": b32encode(device.bin_key).decode(),
                "next": _next_url(request),
                "page_title": "Set up two-step sign-in",
                "tour_config": _quiet_tour(request.user),
            },
            status=status,
        )

    def get(self, request):
        enrolled = self._already_enrolled(request)
        if enrolled is not None:
            return enrolled
        return self._render(request, self._device(request.user), TokenForm())

    def post(self, request):
        enrolled = self._already_enrolled(request)
        if enrolled is not None:
            return enrolled

        device = self._device(request.user)
        form = TokenForm(request.POST)
        if not form.is_valid() or not device.verify_token(form.cleaned_data["token"].strip()):
            form.add_error(None, "That code did not match. Check the app and try again.")
            return self._render(request, device, form)

        device.confirmed = True
        device.save()
        otp_login(request, device)
        codes = _new_recovery_codes(request.user)
        record_mfa(request.user, "mfa.enabled")
        return render(
            request,
            "accounts/mfa_recovery_codes.html",
            {
                "codes": codes,
                "next": _next_url(request),
                "page_title": "Save your recovery codes",
                "tour_config": _quiet_tour(request.user),
            },
        )


def _new_recovery_codes(user) -> list[str]:
    device, _ = StaticDevice.objects.get_or_create(
        user=user, name="Recovery codes", defaults={"confirmed": True}
    )
    device.token_set.all().delete()
    codes = [StaticToken.random_token() for _ in range(RECOVERY_CODES)]
    for code in codes:
        device.token_set.create(token=code)
    return codes


class MFAVerifyView(MFAView):
    template_name = "accounts/mfa_verify.html"

    def _render(self, request, form, status=200):
        return render(
            request,
            self.template_name,
            {
                "form": form,
                "next": _next_url(request),
                "page_title": "Enter your sign-in code",
                "tour_config": _quiet_tour(request.user),
            },
            status=status,
        )

    def get(self, request):
        if request.user.is_verified():
            return HttpResponseRedirect(_next_url(request))
        if not user_has_device(request.user, confirmed=True):
            return HttpResponseRedirect(reverse("accounts:mfa_setup"))
        return self._render(request, TokenForm())

    def post(self, request):
        key = str(request.user.pk)
        form = TokenForm(request.POST)
        # On top of django-otp's own per-device back-off, and across recovery codes,
        # which do not have one.
        if ratelimit.MFA_VERIFY.is_exhausted(key):
            form.is_valid()
            form.add_error(None, "Too many attempts. Wait 15 minutes and try again.")
            return self._render(request, form, status=429)

        device = None
        if form.is_valid():
            device = match_token(request.user, form.cleaned_data["token"].strip())
        if device is None:
            ratelimit.MFA_VERIFY.hit(key)
            form.add_error(None, "That code did not match. Check the app and try again.")
            return self._render(request, form)

        ratelimit.MFA_VERIFY.clear(key)
        otp_login(request, device)
        if isinstance(device, StaticDevice):
            record_mfa(request.user, "mfa.recovery_code_used")
            messages.warning(
                request,
                f"Recovery code used. You have {device.token_set.count()} left.",
            )
        return HttpResponseRedirect(_next_url(request))


def remove_devices(user) -> int:
    """Clear every device, for an account that has lost its authenticator."""
    devices = list(devices_for_user(user, confirmed=None))
    for device in devices:
        device.delete()
    record_mfa(user, "mfa.reset")
    return len(devices)
