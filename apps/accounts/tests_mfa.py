"""Authenticator-app sign-in for the accounts that can reach every school."""

from __future__ import annotations

from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django_otp.oath import TOTP
from django_otp.plugins.otp_static.models import StaticDevice
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.audit.models import AuditEvent
from apps.core.roles import Role
from apps.schools.models import School, SchoolStatus

User = get_user_model()
HOME = "/platform/"


def code_for(device) -> str:
    totp = TOTP(device.bin_key, device.step, device.t0, device.digits, device.drift)
    return f"{totp.token():06d}"


@override_settings(MFA_REQUIRED_FOR_PLATFORM=True)
class MFATestCase(TestCase):
    def setUp(self):
        self.platform = User.objects.create_user(
            "platform", password="pw", role=Role.PLATFORM_OWNER
        )
        self.client.force_login(self.platform)

    def enrol(self) -> TOTPDevice:
        self.client.get(reverse("accounts:mfa_setup"))
        device = TOTPDevice.objects.get(user=self.platform, confirmed=False)
        self.client.post(reverse("accounts:mfa_setup"), {"token": code_for(device)})
        device.refresh_from_db()
        return device


class EnrolmentTests(MFATestCase):
    def test_a_platform_account_with_no_device_is_sent_to_set_one_up(self):
        response = self.client.get(HOME)

        self.assertRedirects(
            response, f"{reverse('accounts:mfa_setup')}?next={HOME}", fetch_redirect_response=False
        )

    def test_the_admin_is_covered_too(self):
        root = User.objects.create_superuser("root", password="pw")
        self.client.force_login(root)

        response = self.client.get(reverse("admin:index"))

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:mfa_setup"), response["Location"])

    def test_the_setup_page_offers_a_code_to_scan_and_a_key_to_type(self):
        response = self.client.get(reverse("accounts:mfa_setup"))

        self.assertContains(response, "<svg")
        self.assertContains(response, "otpauth://totp/")

    def test_a_wrong_code_does_not_confirm_it(self):
        self.client.get(reverse("accounts:mfa_setup"))

        response = self.client.post(reverse("accounts:mfa_setup"), {"token": "000000"})

        self.assertEqual(response.status_code, 200)
        self.assertFalse(TOTPDevice.objects.filter(user=self.platform, confirmed=True).exists())
        self.assertEqual(self.client.get(HOME).status_code, 302)

    def test_a_right_code_confirms_it_and_opens_the_platform(self):
        device = self.enrol()

        self.assertTrue(device.confirmed)
        self.assertEqual(self.client.get(HOME).status_code, 200)

    def test_recovery_codes_are_shown_once(self):
        self.client.get(reverse("accounts:mfa_setup"))
        device = TOTPDevice.objects.get(user=self.platform, confirmed=False)

        response = self.client.post(reverse("accounts:mfa_setup"), {"token": code_for(device)})

        tokens = StaticDevice.objects.get(user=self.platform).token_set.all()
        self.assertEqual(tokens.count(), 10)
        for token in tokens:
            self.assertContains(response, token.token)
        self.assertNotContains(self.client.get(reverse("accounts:mfa_setup"), follow=True), tokens[0].token)

    def test_setting_it_up_is_recorded(self):
        self.enrol()

        event = AuditEvent.all_objects.get(action="mfa.enabled")
        self.assertEqual(event.actor, self.platform)
        self.assertEqual(event.target_label, "platform")

    def test_an_existing_device_cannot_be_replaced_from_a_stolen_session(self):
        self.enrol()
        self.client.logout()
        self.client.force_login(self.platform)

        response = self.client.post(reverse("accounts:mfa_setup"), {"token": "000000"})

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:mfa_verify"), response["Location"])
        self.assertEqual(TOTPDevice.objects.filter(user=self.platform).count(), 1)


class VerifyingTests(MFATestCase):
    def setUp(self):
        super().setUp()
        self.device = self.enrol()
        self.client.logout()
        self.client.force_login(self.platform)

    def test_a_new_session_must_verify_first(self):
        response = self.client.get(HOME)

        self.assertRedirects(
            response, f"{reverse('accounts:mfa_verify')}?next={HOME}", fetch_redirect_response=False
        )

    def test_a_right_code_opens_the_way_they_were_going(self):
        self.device.last_t = -1
        self.device.save()

        response = self.client.post(
            f"{reverse('accounts:mfa_verify')}?next=/platform/schools/",
            {"token": code_for(self.device)},
        )

        self.assertRedirects(response, "/platform/schools/", fetch_redirect_response=False)
        self.assertEqual(self.client.get(HOME).status_code, 200)

    def test_a_code_cannot_be_used_twice(self):
        """It was used a moment ago to enrol."""
        response = self.client.post(
            reverse("accounts:mfa_verify"), {"token": code_for(self.device)}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get(HOME).status_code, 302)

    def test_a_wrong_code_gets_no_further(self):
        response = self.client.post(reverse("accounts:mfa_verify"), {"token": "000000"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "did not match")
        self.assertEqual(self.client.get(HOME).status_code, 302)

    def test_an_outside_address_in_next_is_ignored(self):
        self.device.last_t = -1
        self.device.save()

        response = self.client.post(
            f"{reverse('accounts:mfa_verify')}?next=https://evil.example/",
            {"token": code_for(self.device)},
        )

        self.assertEqual(response.status_code, 302)
        self.assertNotIn("evil.example", response["Location"])

    def test_a_recovery_code_works_once_and_is_recorded(self):
        token = StaticDevice.objects.get(user=self.platform).token_set.first().token

        self.client.post(reverse("accounts:mfa_verify"), {"token": token})
        self.assertEqual(self.client.get(HOME).status_code, 200)
        self.assertTrue(AuditEvent.all_objects.filter(action="mfa.recovery_code_used").exists())

        self.client.logout()
        self.client.force_login(self.platform)
        self.client.post(reverse("accounts:mfa_verify"), {"token": token})
        self.assertEqual(self.client.get(HOME).status_code, 302)

    def test_signing_out_still_works(self):
        self.assertEqual(self.client.post(reverse("logout")).status_code, 302)


class RepeatedGuessesTests(MFATestCase):
    @override_settings(RATE_LIMITS_ENABLED=True)
    def test_guessing_is_stopped(self):
        from django.core.cache import cache

        from apps.core.ratelimit import MFA_VERIFY

        cache.clear()
        self.addCleanup(cache.clear)
        device = self.enrol()
        self.client.logout()
        self.client.force_login(self.platform)

        for _ in range(MFA_VERIFY.limit):
            self.client.post(reverse("accounts:mfa_verify"), {"token": "000000"})
        device.refresh_from_db()
        device.throttle_reset()
        device.last_t = -1
        device.save()
        response = self.client.post(
            reverse("accounts:mfa_verify"), {"token": code_for(device)}
        )

        self.assertEqual(response.status_code, 429)
        self.assertEqual(self.client.get(HOME).status_code, 302)


class WhoItAppliesToTests(TestCase):
    @override_settings(MFA_REQUIRED_FOR_PLATFORM=True)
    def test_school_staff_are_not_asked(self):
        school = School.all_objects.create(name="Alpha", status=SchoolStatus.ACTIVE)
        owner = User.objects.create_user(
            "owner", password="pw", role=Role.SCHOOL_OWNER, school=school
        )
        self.client.force_login(owner)

        self.assertEqual(self.client.get("/school/").status_code, 200)

    @override_settings(MFA_REQUIRED_FOR_PLATFORM=True)
    def test_school_staff_cannot_open_the_setup_page(self):
        school = School.all_objects.create(name="Alpha", status=SchoolStatus.ACTIVE)
        owner = User.objects.create_user(
            "owner", password="pw", role=Role.SCHOOL_OWNER, school=school
        )
        self.client.force_login(owner)

        self.assertEqual(self.client.get(reverse("accounts:mfa_setup")).status_code, 403)

    @override_settings(MFA_REQUIRED_FOR_PLATFORM=False)
    def test_the_switch_turns_it_off(self):
        platform = User.objects.create_user(
            "platform", password="pw", role=Role.PLATFORM_OWNER
        )
        self.client.force_login(platform)

        self.assertEqual(self.client.get(HOME).status_code, 200)

    @override_settings(MFA_REQUIRED_FOR_PLATFORM=True)
    def test_a_temporary_password_comes_first_without_a_redirect_loop(self):
        platform = User.objects.create_user(
            "platform", password="pw", role=Role.PLATFORM_OWNER, must_change_password=True
        )
        self.client.force_login(platform)

        response = self.client.get(reverse("accounts:settings"))

        self.assertEqual(response.status_code, 200)


class LostDeviceTests(MFATestCase):
    def test_the_command_clears_a_lost_device_and_records_it(self):
        self.enrol()

        call_command("reset_platform_mfa", email="", username="platform", stdout=StringIO())

        self.assertFalse(TOTPDevice.objects.filter(user=self.platform).exists())
        self.assertFalse(StaticDevice.objects.filter(user=self.platform).exists())
        self.assertTrue(AuditEvent.all_objects.filter(action="mfa.reset").exists())
