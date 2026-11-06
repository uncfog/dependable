"""Limits on the public doors: sign-in, password reset, signup and enquiries."""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from apps.academics.models import Class, Level
from apps.core import ratelimit
from apps.core.roles import Role
from apps.schools.models import Branch, School, SchoolModule, SchoolStatus

User = get_user_model()
PASSWORD = "correct-horse-battery-41"


@override_settings(RATE_LIMITS_ENABLED=True)
class RateLimitTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)


class ClientAddressTests(TestCase):
    def address(self, **meta):
        return ratelimit.client_ip(RequestFactory().get("/", **meta))

    def test_the_proxys_real_ip_header_wins(self):
        self.assertEqual(
            self.address(
                HTTP_X_REAL_IP="203.0.113.9",
                HTTP_X_FORWARDED_FOR="198.51.100.1",
                REMOTE_ADDR="10.0.0.1",
            ),
            "203.0.113.9",
        )

    def test_then_the_first_forwarded_address(self):
        self.assertEqual(
            self.address(
                HTTP_X_FORWARDED_FOR="198.51.100.1, 10.1.1.1", REMOTE_ADDR="10.0.0.1"
            ),
            "198.51.100.1",
        )

    def test_then_the_socket(self):
        self.assertEqual(self.address(REMOTE_ADDR="10.0.0.1"), "10.0.0.1")


class TheSwitchTests(TestCase):
    @override_settings(RATE_LIMITS_ENABLED=False)
    def test_switched_off_nothing_is_ever_refused(self):
        limit = ratelimit.Limit("test", 1, 60)
        for _ in range(5):
            self.assertTrue(limit.allow("key"))
        self.assertFalse(limit.is_exhausted("key"))


class SignInLimitTests(RateLimitTestCase):
    def setUp(self):
        super().setUp()
        school = School.all_objects.create(name="Alpha")
        self.user = User.objects.create_user(
            "alpha.owner", email="owner@alpha.test", password=PASSWORD,
            role=Role.SCHOOL_OWNER, school=school,
        )
        self.url = reverse("login")

    def attempt(self, password="wrong", username=None, **extra):
        return self.client.post(
            self.url, {"username": username or self.user.username, "password": password},
            **extra,
        )

    def exhaust(self, username=None, **extra):
        for _ in range(ratelimit.LOGIN_PAIR.limit):
            response = self.attempt(username=username, **extra)
        return response

    def test_repeated_wrong_passwords_are_answered_with_429(self):
        response = self.exhaust()

        self.assertEqual(response.status_code, 429)
        self.assertContains(response, "Too many sign-in attempts", status_code=429)

    def test_the_right_password_does_not_work_once_blocked(self):
        self.exhaust()

        self.assertEqual(self.attempt(password=PASSWORD).status_code, 429)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_a_good_sign_in_before_the_limit_clears_the_count(self):
        for _ in range(ratelimit.LOGIN_PAIR.limit - 1):
            self.attempt()
        self.assertEqual(self.attempt(password=PASSWORD).status_code, 302)
        self.client.post(reverse("logout"))

        self.assertEqual(self.attempt().status_code, 200)

    def test_the_email_and_the_username_are_counted_as_typed(self):
        response = self.exhaust(username="OWNER@alpha.test")

        self.assertEqual(response.status_code, 429)
        self.assertEqual(self.attempt(password=PASSWORD).status_code, 302)

    def test_somebody_elsewhere_can_still_sign_in_to_that_account(self):
        self.exhaust(REMOTE_ADDR="198.51.100.1")

        response = self.attempt(password=PASSWORD, REMOTE_ADDR="198.51.100.2")

        self.assertEqual(response.status_code, 302)

    def test_guessing_one_account_from_many_addresses_is_limited(self):
        for number in range(ratelimit.LOGIN_ACCOUNT.limit):
            self.attempt(REMOTE_ADDR=f"198.51.{number // 250}.{number % 250 + 1}")

        response = self.attempt(password=PASSWORD, REMOTE_ADDR="203.0.113.5")

        self.assertEqual(response.status_code, 429)

    def test_one_address_guessing_many_accounts_is_limited(self):
        for number in range(ratelimit.LOGIN_CLIENT.limit):
            self.attempt(username=f"person{number}")

        self.assertEqual(self.attempt(password=PASSWORD).status_code, 429)

    def test_the_admin_sign_in_is_covered_too(self):
        admin = User.objects.create_superuser("root", password=PASSWORD)
        url = reverse("admin:login")
        for _ in range(ratelimit.LOGIN_PAIR.limit):
            self.client.post(url, {"username": admin.username, "password": "wrong"})

        self.client.post(url, {"username": admin.username, "password": PASSWORD})

        self.assertNotIn("_auth_user_id", self.client.session)


class PasswordResetLimitTests(RateLimitTestCase):
    def setUp(self):
        super().setUp()
        User.objects.create_user("person", email="person@alpha.test", password=PASSWORD)
        self.url = reverse("password_reset")

    def request_reset(self, email="person@alpha.test", **extra):
        return self.client.post(self.url, {"email": email}, **extra)

    def test_one_person_cannot_be_flooded(self):
        for _ in range(ratelimit.RESET_EMAIL.limit + 5):
            self.request_reset()

        self.assertEqual(len(mail.outbox), ratelimit.RESET_EMAIL.limit)

    def test_the_answer_never_changes(self):
        """A refusal would say the address exists or that somebody is trying it."""
        for _ in range(ratelimit.RESET_EMAIL.limit + 2):
            response = self.request_reset()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("password_reset_done"))

    def test_one_address_cannot_walk_through_many_mailboxes(self):
        for number in range(ratelimit.RESET_CLIENT.limit):
            self.request_reset(email=f"nobody{number}@alpha.test")

        self.request_reset()

        self.assertEqual(len(mail.outbox), 0)

    def test_address_case_does_not_buy_more_emails(self):
        for number in range(ratelimit.RESET_EMAIL.limit + 2):
            self.request_reset(email="PERSON@alpha.test" if number % 2 else "person@alpha.test")

        self.assertEqual(len(mail.outbox), ratelimit.RESET_EMAIL.limit)


class SignupLimitTests(RateLimitTestCase):
    def signup(self, number):
        return self.client.post(
            reverse("core:signup"),
            {
                "school_name": f"School {number}",
                "full_name": "Ada Obi",
                "email": f"owner{number}@example.test",
                "password": PASSWORD,
                "password_confirm": PASSWORD,
            },
        )

    def test_one_address_cannot_create_schools_without_limit(self):
        for number in range(ratelimit.SIGNUP_CLIENT.limit):
            self.signup(number)
            self.client.logout()
        created = School.all_objects.count()

        response = self.signup("over")

        self.assertEqual(response.status_code, 429)
        self.assertEqual(School.all_objects.count(), created)

    def test_reading_the_form_is_never_limited(self):
        for number in range(ratelimit.SIGNUP_CLIENT.limit):
            self.signup(number)
            self.client.logout()

        self.assertEqual(self.client.get(reverse("core:signup")).status_code, 200)


class EnquiryLimitTests(RateLimitTestCase):
    def setUp(self):
        super().setUp()
        self.school = School.all_objects.create(
            name="Alpha", status=SchoolStatus.ACTIVE
        )
        self.other = School.all_objects.create(
            name="Bravo", status=SchoolStatus.ACTIVE
        )
        for school in (self.school, self.other):
            SchoolModule.all_objects.create(school=school, key="admissions", enabled=True)
            branch = Branch.all_objects.create(school=school, name="Main")
            Class.all_objects.create(
                branch=branch, name="JSS 1", level=Level.JUNIOR_SECONDARY, year_in_level=1
            )

    def enquire(self, school=None):
        url = reverse(
            "admissions_public:public_enquiry", args=[(school or self.school).slug]
        )
        # Invalid on purpose: the limit counts what is sent, not what is accepted,
        # because an upload has cost the platform the moment it arrives.
        return self.client.post(url, {"first_name": "A"})

    def test_one_address_cannot_flood_a_school(self):
        for _ in range(ratelimit.ENQUIRY_CLIENT.limit):
            self.assertEqual(self.enquire().status_code, 200)

        response = self.enquire()

        self.assertEqual(response.status_code, 429)
        self.assertContains(response, "Too many enquiries", status_code=429)

    def test_the_limit_is_per_school(self):
        for _ in range(ratelimit.ENQUIRY_CLIENT.limit + 1):
            self.enquire()

        self.assertEqual(self.enquire(self.other).status_code, 200)

    def test_a_school_that_is_not_taking_enquiries_is_still_a_404(self):
        self.school.status = SchoolStatus.SUSPENDED
        self.school.save()

        self.assertEqual(self.enquire().status_code, 404)
