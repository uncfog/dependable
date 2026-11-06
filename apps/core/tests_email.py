"""How mail actually leaves the building.

Production sent through Django's SMTP backend until the host turned out to
block outbound 587. Nothing errored: the send *hung*, holding a web worker
until the request timed out, so a password reset looked like a broken site.
Mail now goes over HTTPS through Anymail to Resend.

Two things are worth a test. That production is wired to Anymail at all -- the
settings module is not exercised by any other test, and this is the file
somebody will "tidy" an SMTP variable back into. And that a real password reset
ends up as a request to Resend's API rather than a socket to a mail server,
which is the part that was broken.
"""

from __future__ import annotations

import importlib
import json
import os
import smtplib
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.core.branding import PRODUCT_NAME, SUPPORT_EMAIL
from apps.core.roles import Role
from apps.schools.models import School

User = get_user_model()

#: The one domain verified in Resend. The root domain is not, and sending from
#: it is refused -- which is why this is asserted rather than assumed.
VERIFIED_SENDING_DOMAIN = "send.theschoolcord.com"

RESEND_BACKEND = "anymail.backends.resend.EmailBackend"

#: Enough environment for config.settings.prod to import. SQLite keeps it from
#: reaching for a database that is not this test run's.
PRODUCTION_ENV = {
    "DB_ENGINE": "sqlite",
    "ALLOWED_HOSTS": "www.theschoolcord.com",
    "RESEND_API_KEY": "re_test_key",
    "SECRET_KEY": "not-the-real-one",
}


def _import_production_settings():
    with mock.patch.dict(os.environ, PRODUCTION_ENV, clear=False):
        # base first: it is what reads SECRET_KEY, and it was imported long ago with
        # whatever this machine's environment held, which may be nothing.
        importlib.reload(importlib.import_module("config.settings.base"))
        return importlib.reload(importlib.import_module("config.settings.prod"))


class ProductionMailSettingsTests(TestCase):
    def setUp(self):
        self.prod = _import_production_settings()

    def test_production_sends_through_resend(self):
        self.assertEqual(self.prod.EMAIL_BACKEND, RESEND_BACKEND)
        self.assertEqual(self.prod.ANYMAIL["RESEND_API_KEY"], "re_test_key")

    def test_no_smtp_setting_survives_to_take_precedence(self):
        for name in [
            "EMAIL_HOST", "EMAIL_PORT", "EMAIL_HOST_USER",
            "EMAIL_HOST_PASSWORD", "EMAIL_USE_TLS", "EMAIL_USE_SSL",
        ]:
            with self.subTest(setting=name):
                self.assertFalse(hasattr(self.prod, name))

    def test_even_an_smtp_host_in_the_environment_changes_nothing(self):
        # The variables left on the host from the SMTP days cannot quietly
        # bring it back: nothing reads them any more.
        with mock.patch.dict(
            os.environ,
            dict(PRODUCTION_ENV, EMAIL_HOST="smtp.example.com", EMAIL_PORT="587"),
            clear=False,
        ):
            prod = importlib.reload(importlib.import_module("config.settings.prod"))
        self.assertEqual(prod.EMAIL_BACKEND, RESEND_BACKEND)
        self.assertFalse(hasattr(prod, "EMAIL_HOST"))

    def test_the_from_address_is_on_the_verified_sending_domain(self):
        self.assertTrue(
            self.prod.DEFAULT_FROM_EMAIL.endswith(f"@{VERIFIED_SENDING_DOMAIN}>"),
            self.prod.DEFAULT_FROM_EMAIL,
        )
        self.assertIn(PRODUCT_NAME, self.prod.DEFAULT_FROM_EMAIL)
        # The root domain is not verified in Resend, so it must not appear as
        # the whole domain of the sender.
        self.assertNotIn("@theschoolcord.com", self.prod.DEFAULT_FROM_EMAIL)

    def test_the_product_from_address_agrees(self):
        self.assertTrue(SUPPORT_EMAIL.endswith(f"@{VERIFIED_SENDING_DOMAIN}"))


class _FakeResendAPI:
    """Stands in for requests.Session.request, recording what Resend was sent."""

    def __init__(self):
        self.calls: list[tuple] = []

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = mock.Mock(status_code=200, text='{"id": "fake-id"}')
        response.json.return_value = {"id": "fake-id"}
        return response

    @property
    def payload(self) -> dict:
        """The message as Resend receives it -- Anymail posts it serialised."""
        kwargs = self.calls[0][2]
        if "json" in kwargs:
            return kwargs["json"]
        return json.loads(kwargs["data"])


@override_settings(
    EMAIL_BACKEND=RESEND_BACKEND,
    ANYMAIL={"RESEND_API_KEY": "re_test_key"},
    DEFAULT_FROM_EMAIL=f"{PRODUCT_NAME} <{SUPPORT_EMAIL}>",
)
class PasswordResetGoesOutThroughResendTests(TestCase):
    """The flow that was hanging, end to end, with the network stubbed."""

    @classmethod
    def setUpTestData(cls):
        cls.school = School.all_objects.create(name="Bright Future Academy")
        cls.user = User.objects.create_user(
            "owner", email="owner@example.com", password="pw",
            role=Role.SCHOOL_OWNER, school=cls.school,
        )

    def reset(self) -> _FakeResendAPI:
        api = _FakeResendAPI()
        # Any attempt to open an SMTP socket fails the test rather than
        # waiting on a blocked port.
        with mock.patch("requests.Session.request", api), mock.patch.object(
            smtplib, "SMTP", side_effect=AssertionError("mail went to SMTP")
        ):
            response = self.client.post(
                reverse("password_reset"), {"email": self.user.email}
            )
        self.assertEqual(response.status_code, 302)
        return api

    def test_it_is_posted_to_the_resend_api(self):
        api = self.reset()
        self.assertEqual(len(api.calls), 1)
        method, url, kwargs = api.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, "https://api.resend.com/emails")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer re_test_key")

    def test_it_is_sent_from_the_verified_domain_to_the_user(self):
        payload = self.reset().payload
        self.assertTrue(
            payload["from"].endswith(f"@{VERIFIED_SENDING_DOMAIN}>"), payload["from"]
        )
        self.assertEqual(payload["to"], [self.user.email])

    def test_the_reset_link_is_in_the_message_that_leaves(self):
        payload = self.reset().payload
        body = payload.get("text", "") + payload.get("html", "")
        self.assertIn("/accounts/reset/", body)
        self.assertIn(PRODUCT_NAME, payload["subject"])

    def test_the_backend_in_use_is_anymail_and_not_django_smtp(self):
        connection = mail.get_connection()
        self.assertEqual(
            f"{type(connection).__module__}.{type(connection).__qualname__}",
            "anymail.backends.resend.EmailBackend",
        )
