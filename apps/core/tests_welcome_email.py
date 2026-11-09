"""The welcome a school gets for signing up.

Two things it has to keep doing, and one it must never start doing.

It has to **send**, on a real signup, with the branding a mail template cannot get
from a context processor and the absolute URLs it cannot get from a request.

It has to **fail quietly**. The school exists, the owner is signed in, and the
email is the least important thing that happened in that request -- so a mail
backend that is down, or a deployment with no public address configured, must cost
the message and nothing else.

And it must never become a **gate**. There is no confirmation link and nothing to
click before the account works; a test asserts the owner is signed in and on the
onboarding screen whether or not the mail went anywhere.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.core.branding import CONTACT_EMAIL, PRODUCT_NAME
from apps.core.emails import WELCOME_STEPS, send_welcome_email
from apps.schools.models import Branch, School

User = get_user_model()

SITE = "https://www.theschoolcord.com"

SIGNUP = {
    "school_name": "Brand New Academy",
    "full_name": "Chidi Eze",
    "email": "chidi@example.com",
    "password": "correct-horse-battery-staple-42",
    "password_confirm": "correct-horse-battery-staple-42",
}


@override_settings(PUBLIC_BASE_URL=SITE)
class WelcomeEmailTests(TestCase):
    def sign_up(self, **overrides):
        mail.outbox.clear()
        data = dict(SIGNUP)
        data.update(overrides)
        return self.client.post(reverse("core:signup"), data)

    def sent(self):
        self.assertEqual(len(mail.outbox), 1, mail.outbox)
        return mail.outbox[0]

    def html(self) -> str:
        message = self.sent()
        self.assertEqual(len(message.alternatives), 1)
        content, mimetype = message.alternatives[0]
        self.assertEqual(mimetype, "text/html")
        return content

    # -- it sends ----------------------------------------------------------

    def test_signing_up_sends_it(self):
        response = self.sign_up()
        # Sign-up opens Plans & billing first; onboarding follows from there.
        self.assertRedirects(response, reverse("billing:checkout") + "?welcome=1")
        message = self.sent()
        self.assertEqual(message.to, ["chidi@example.com"])
        self.assertEqual(message.subject, f"Welcome to {PRODUCT_NAME}")

    def test_it_goes_out_as_the_product(self):
        self.sign_up()
        self.assertIn(PRODUCT_NAME, self.sent().from_email)
        self.assertIn("noreply@send.theschoolcord.com", self.sent().from_email)

    def test_it_is_multipart_with_a_plain_text_body(self):
        self.sign_up()
        message = self.sent()
        self.assertTrue(message.body.strip())
        self.assertNotIn("<table", message.body)
        self.assertEqual(message.alternatives[0][1], "text/html")

    def test_it_names_the_school_and_its_first_campus(self):
        self.sign_up()
        html = self.html()
        self.assertIn("Brand New Academy", html)
        school = School.all_objects.get(name="Brand New Academy")
        branch = Branch.all_objects.get(school=school)
        self.assertIn(branch.name, html)

    def test_it_greets_the_owner_by_name(self):
        self.sign_up()
        self.assertIn("Chidi Eze", self.html())

    # -- what it points at -------------------------------------------------

    def test_it_links_to_onboarding_absolutely(self):
        self.sign_up()
        expected = SITE + reverse("core:onboarding")
        self.assertIn(expected, self.html())
        self.assertIn(expected, self.sent().body)

    def test_it_lists_the_four_setup_steps(self):
        self.sign_up()
        html = self.html()
        text = self.sent().body
        for label, _ in WELCOME_STEPS:
            with self.subTest(step=label):
                self.assertIn(label, html)
                self.assertIn(label, text)

    def test_those_steps_are_the_ones_the_checklist_shows(self):
        """The email lists the ladder without counting rows -- it goes to a school
        created three lines ago, which has none of them -- so the two lists are
        kept in step by hand. This is what notices when they stop matching.

        Asserted against the real checklist for a real school rather than a mocked
        one: the point is that the labels agree, and mocking out the counts to get
        at them would be testing the mock.
        """
        from apps.core.tenancy import TenantContext, activate, deactivate
        from apps.core.views import onboarding_steps

        self.sign_up()
        owner = User.objects.get(email="chidi@example.com")
        request = self.client.get(reverse("core:onboarding")).wsgi_request

        token = activate(TenantContext.from_user(owner))
        try:
            labels = [step["label"] for step in onboarding_steps(request)]
        finally:
            deactivate(token)
        self.assertEqual(labels, [label for label, _ in WELCOME_STEPS])

    def test_it_gives_them_a_way_to_reply(self):
        self.sign_up()
        self.assertIn(CONTACT_EMAIL, self.html())
        self.assertIn(CONTACT_EMAIL, self.sent().body)

    # -- how it looks ------------------------------------------------------

    def test_it_carries_the_logo_as_an_absolute_png(self):
        import re

        self.sign_up()
        match = re.search(r'<img src="([^"]+)"', self.html())
        self.assertIsNotNone(match, "no image in the email")
        src = match.group(1)
        self.assertTrue(src.startswith("https://"), src)
        self.assertTrue(src.endswith(".png"), src)
        self.assertIn("schoolcord-logo", src)

    def test_no_svg_reaches_a_mail_client(self):
        self.sign_up()
        self.assertNotIn(".svg", self.html())

    def test_it_is_on_the_same_shell_as_the_reset_email(self):
        self.sign_up()
        html = self.html()
        self.assertIn("#0f2547", html)          # the header band
        self.assertIn("max-width:560px", html)  # the mobile column
        self.assertIn(PRODUCT_NAME, html)
        self.assertNotIn("display:flex", html)

    def test_the_branding_reaches_it_despite_there_being_no_request(self):
        self.sign_up()
        html = self.html()
        self.assertNotIn("Welcome to  ", html)
        self.assertNotIn("{{", html)

    # -- it is not a gate --------------------------------------------------

    def test_the_account_works_immediately(self):
        """No confirmation link, nothing to click first."""
        response = self.sign_up(follow=False)
        # Sign-up opens Plans & billing first; onboarding follows from there.
        self.assertRedirects(response, reverse("billing:checkout") + "?welcome=1")
        owner = User.objects.get(email="chidi@example.com")
        self.assertTrue(owner.is_active)
        # Signed in already -- the onboarding screen is behind a login.
        self.assertEqual(
            self.client.get(reverse("core:onboarding")).status_code, 200
        )

    def test_it_says_so(self):
        """Whitespace flattened: the sentence wraps in the template source."""
        self.sign_up()
        flat = " ".join(self.html().split())
        self.assertIn("nothing to confirm", flat)
        self.assertIn("no link you have to click first", flat)

    # -- it fails quietly --------------------------------------------------

    def test_a_mail_backend_that_throws_does_not_break_signup(self):
        with mock.patch(
            "apps.core.emails.EmailMultiAlternatives.send",
            side_effect=OSError("smtp is down"),
        ):
            with self.assertLogs("apps.core.emails", level="ERROR"):
                response = self.sign_up()
        # Sign-up opens Plans & billing first; onboarding follows from there.
        self.assertRedirects(response, reverse("billing:checkout") + "?welcome=1")
        self.assertTrue(School.all_objects.filter(name="Brand New Academy").exists())

    @override_settings(PUBLIC_BASE_URL="")
    def test_no_public_address_means_no_email_rather_than_a_broken_one(self):
        """Every link in it would be relative, and a relative link in an email
        goes nowhere."""
        with self.assertLogs("apps.core.emails", level="WARNING"):
            response = self.sign_up()
        # Sign-up opens Plans & billing first; onboarding follows from there.
        self.assertRedirects(response, reverse("billing:checkout") + "?welcome=1")
        self.assertEqual(mail.outbox, [])
        self.assertTrue(School.all_objects.filter(name="Brand New Academy").exists())

    def test_an_owner_with_no_address_is_not_an_error(self):
        """The platform creates accounts this way for schools it sets up by
        hand. There is simply nowhere to send."""
        school = School.all_objects.create(name="Handmade Academy")
        branch = Branch.all_objects.create(school=school, name="Main")
        owner = User.objects.create_user("handmade", password="pw")
        mail.outbox.clear()
        self.assertFalse(send_welcome_email(school, branch, owner))
        self.assertEqual(mail.outbox, [])

    def test_it_reports_whether_it_sent(self):
        school = School.all_objects.create(name="Reported Academy")
        branch = Branch.all_objects.create(school=school, name="Main")
        owner = User.objects.create_user(
            "reported", email="reported@example.com", password="pw"
        )
        mail.outbox.clear()
        self.assertTrue(send_welcome_email(school, branch, owner))
        self.assertEqual(len(mail.outbox), 1)
