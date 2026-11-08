"""The public landing page, rebuilt from the SCHOOLCORD_Landing_Page.html design.

What matters functionally: every way in reaches a real route, the page is
light-only, and the demo is a demo -- labelled, and unable to reach the server.
"""

from __future__ import annotations

import re

from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.core.branding import SUPPORT_WHATSAPP_URL
from apps.core.roles import Role

User = get_user_model()


class LandingTestCase(TestCase):
    def setUp(self):
        response = self.client.get(reverse("core:landing"))
        self.assertEqual(response.status_code, 200)
        self.html = response.content.decode()

    def demo(self) -> str:
        """The demo section, plus its three dialogs and the page script."""
        section = self.html[
            self.html.index('id="live-demo"'):self.html.index('id="pricing"')
        ]
        dialogs_and_script = self.html[
            self.html.index("<!-- DEMO DIALOG: RECORD PAYMENT"):self.html.index("<footer")
        ]
        return section + dialogs_and_script


class CallToActionTests(LandingTestCase):
    def test_login_goes_to_the_login_route_and_has_no_arrow(self):
        login = reverse("login")
        self.assertRegex(
            self.html, rf'<a href="{re.escape(login)}"[^>]*>\s*Log in'
        )
        self.assertNotIn("Login ↗", self.html)

    def test_every_set_up_and_plan_button_goes_to_signup(self):
        signup = reverse("core:signup")
        for label in ("Set up my school", "Recover your unpaid fees",
                      "Start free", "Choose Starter", "Choose Growth",
                      "Choose Professional", "Create my school account"):
            with self.subTest(label=label):
                pattern = (
                    rf'<a href="{re.escape(signup)}"[^>]*>\s*(<span>)?'
                    rf"{re.escape(label)}"
                )
                self.assertRegex(self.html, pattern)

    def test_whatsapp_links_read_the_branding_config(self):
        self.assertEqual(
            self.html.count(f'href="{SUPPORT_WHATSAPP_URL}" target="_blank" rel="noopener"'),
            2,  # Enterprise's "Talk to us" and "Chat with us on WhatsApp"
        )

    def test_whatsapp_and_footer_use_the_real_support_line(self):
        self.assertEqual(SUPPORT_WHATSAPP_URL, "https://wa.me/2348135513401")
        self.assertIn('<a href="tel:+2348135513401"', self.html)
        self.assertIn("+234 813 551 3401", self.html)
        self.assertNotIn("800 SCHOOLCORD", self.html)

    def test_no_call_to_action_dead_ends_on_the_demo_anchor(self):
        """The design pointed every button at #live-demo; only demo links may."""
        for label in ("SET UP MY SCHOOL NOW", "CHOOSE STARTER",
                      "CREATE MY SCHOOL ACCOUNT", "<span>Login</span>"):
            with self.subTest(label=label):
                self.assertNotRegex(
                    self.html, rf'href="#live-demo"[^>]*>\s*{re.escape(label)}'
                )


class LightOnlyTests(LandingTestCase):
    def test_the_page_is_locked_to_the_light_theme(self):
        self.assertIn('<html lang="en" class="h-full" data-theme="light" data-theme-lock>',
                      self.html)
        # The script that follows a stored choice or the OS is not on the page.
        self.assertNotIn("schoolcord-theme", self.html)

    def test_there_is_no_theme_switch(self):
        self.assertNotIn("data-theme-toggle", self.html)
        self.assertNotIn("data-theme-set", self.html)

    def test_other_pages_still_follow_the_theme(self):
        body = self.client.get(reverse("login")).content.decode()
        self.assertIn("schoolcord-theme", body)
        self.assertNotIn("data-theme-lock", body)


class DesignCleanupTests(LandingTestCase):
    def test_no_placeholder_badges_or_stock_photos(self):
        self.assertNotIn("Background Placeholder", self.html)
        self.assertNotIn("📷", self.html)
        self.assertNotIn("unsplash", self.html.lower())
        self.assertNotIn('src="image.png"', self.html)

    def test_no_cdn_tailwind_and_the_designs_fonts_are_loaded(self):
        self.assertNotIn("cdn.tailwindcss.com", self.html)
        # Served by us now, and declared in the stylesheet rather than the page.
        self.assertNotIn("fonts.googleapis.com", self.html)
        source = Path(settings.BASE_DIR, "assets", "app.css").read_text(encoding="utf-8")
        self.assertIn('font-family: "Plus Jakarta Sans"', source)
        self.assertIn('font-family: "Playfair Display"', source)

    def test_the_testimonial_is_readable_size(self):
        quote = re.search(r'<blockquote class="([^"]*)"', self.html).group(1)
        self.assertIn("text-sm", quote)
        self.assertIn("sm:text-base", quote)
        self.assertNotIn("text-[11px]", quote)

    def test_the_testimonial_card_is_the_block_and_holds_the_badge(self):
        figure = self.html[self.html.index("<figure"):self.html.index("</figure>")]
        self.assertIn("Mrs. G. Oveh", figure)
        self.assertIn("98.4% Fee Reconciled", figure)
        self.assertIn("bg-white", re.search(r'<figure class="([^"]*)"', figure).group(1))
        # The navy photo panel it used to float on is gone.
        self.assertNotIn("bg-slate-900", self.html[:self.html.index("<figure")][-600:])

    def test_the_structured_data_is_still_there(self):
        self.assertIn('<script type="application/ld+json">', self.html)


class DemoIsADemoTests(LandingTestCase):
    def test_it_is_labelled_as_a_demo(self):
        demo = self.demo()
        self.assertIn("Demo only", demo)
        self.assertIn("Nothing you do here is saved, sent or charged.", demo)
        self.assertIn("Demo receipt", demo)

    def test_it_cannot_reach_the_server(self):
        demo = self.demo()
        self.assertNotIn("<form", demo)
        for call in ("fetch(", "XMLHttpRequest", "sendBeacon", "navigator.send",
                     ".submit(", "action="):
            with self.subTest(call=call):
                self.assertNotIn(call, demo)

    def test_it_links_to_no_app_route(self):
        demo = self.demo()
        for name in ("payments:record", "payments:index", "messaging:compose"):
            with self.subTest(route=name):
                self.assertNotIn(f'href="{reverse(name)}"', demo)

    def test_the_whatsapp_button_shows_the_message_instead_of_opening_whatsapp(self):
        demo = self.demo()
        self.assertNotIn("wa.me", demo)
        self.assertNotIn("window.open", demo)
        self.assertIn('id="whatsapp-modal"', demo)

    def test_every_demo_button_is_a_plain_button(self):
        demo = self.demo()
        self.assertEqual(re.findall(r"<button(?![^>]*type=\"button\")", demo), [])


class SignedInVisitorTests(TestCase):
    def test_a_signed_in_user_is_still_sent_to_their_dashboard(self):
        user = User.objects.create_user(
            "platform", password="pw", role=Role.PLATFORM_OWNER
        )
        self.client.force_login(user)
        response = self.client.get(reverse("core:landing"))
        self.assertEqual(response.status_code, 302)


class PricingMatchesTheCheckoutTests(LandingTestCase):
    """The page advertises exactly what apps/schools/plans.py charges."""

    def test_every_plan_and_price_is_the_checkouts(self):
        from apps.schools.plans import offers

        pricing = self.html[self.html.index('id="pricing"'):self.html.index('id="faq"')]
        for offer in offers():
            with self.subTest(plan=offer.plan):
                self.assertIn(offer.name, pricing)
                self.assertIn(offer.price_display, pricing)
                self.assertIn(offer.students, pricing)
