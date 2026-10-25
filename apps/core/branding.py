"""The product's own name and mark, in one place.

The platform has been called three things during development -- an internal
codename, a working title, and now SCHOOLCORD -- and the last rename left the
old names scattered across page titles, the sidebar, password-reset email and
the admin header. One constant, read by settings, the admin and every template
through a context processor, is what stops that happening a fourth time.

This is the *product's* identity. A tenant school's own name and logo are on
:class:`apps.schools.models.School`, and the two are never interchangeable: a
parent reading a receipt wants their school's name on it, and a proprietor
signing in wants to know whose software they are signing in to.
"""

from __future__ import annotations

#: What every user-facing surface calls this software.
PRODUCT_NAME = "SCHOOLCORD"

#: One line: the landing-page headline, the sign-in panel, the footer and the
#: email sign-off. It carries its own full stops, so templates print it as-is
#: and never add one after it.
PRODUCT_TAGLINE = "Run Your School. Not Your Paperwork."

#: The sentence that sits under the tagline wherever the tagline is a headline.
PRODUCT_SUBHEAD = (
    "Track school fees, manage student records and communicate with parents, "
    "all in one place."
)

#: What it is, for someone who has never heard of it: the meta description on
#: the home page, link previews, structured data and llms.txt. The tagline and
#: subhead, so search results say what the landing page says.
PRODUCT_DESCRIPTION = (
    f"{PRODUCT_TAGLINE} {PRODUCT_SUBHEAD} Built for Nigerian schools."
)

#: The letter in the brand mark when the full logo will not fit.
PRODUCT_INITIAL = "S"

#: The general way in: the footer, the About page, and anywhere a school is
#: invited to get in touch. A real mailbox a person reads, unlike the no-reply
#: sender below -- and deliberately separate from PRIVACY_EMAIL, which is a
#: statutory contact point named in the policy and should not collect sales
#: questions.
CONTACT_EMAIL = "hello@theschoolcord.com"

#: The support line: the landing footer prints it, and the "Chat with us on
#: WhatsApp" and "Contact sales" links open a chat with it. The URL is the same
#: number in international format with no plus sign or spaces.
SUPPORT_PHONE = "+234 813 551 3401"
SUPPORT_PHONE_TEL = "+2348135513401"
SUPPORT_WHATSAPP_URL = "https://wa.me/2348135513401"

#: Where privacy requests go -- printed on the privacy policy, and a mailbox a
#: person can actually reply to, unlike the no-reply sender below.
PRIVACY_EMAIL = "privacy@theschoolcord.com"

#: The product's own accounts, in the order the footer draws them.
#:
#: One list, read by the footer and by the About page, so an account added here
#: appears in both and neither can carry a link the other does not. ``icon`` names
#: a glyph in templates/partials/_social_icon.html.
#:
#: The URLs are the share links as supplied, query strings and all -- ``?s=11`` on
#: X and ``stkn=...&utm_source=qr`` on Instagram. Both are what those apps hand you
#: when you share a profile from a phone, and both are kept on purpose: the tail is
#: the platform's own attribution, and stripping it would quietly drop whatever it
#: reports back.
#:
#: The ``&`` in the Instagram URL is left as a literal ampersand here. Django
#: escapes it to ``&amp;`` on the way into the ``href``, which is correct HTML and
#: resolves to the same address -- so anything asserting on the rendered page has
#: to escape it too, and the tests do.
SOCIAL_LINKS: tuple[dict[str, str], ...] = (
    {
        "icon": "x",
        "label": "X",
        "handle": "@theschoolcord",
        "url": "https://x.com/theschoolcord?s=11",
    },
    {
        "icon": "instagram",
        "label": "Instagram",
        "handle": "@theschoolcord",
        "url": (
            "https://www.instagram.com/theschoolcord"
            "?stkn=MWdxbmx3NXhxZXNhdg%3D%3D&utm_source=qr"
        ),
    },
    {
        "icon": "mail",
        "label": "Email",
        "handle": CONTACT_EMAIL,
        "url": f"mailto:{CONTACT_EMAIL}",
    },
)

#: Sent as the From: address on transactional mail.
#:
#: It has to be on a domain verified with Resend, which is the *subdomain*
#: send.theschoolcord.com and not the root domain -- Resend refuses to send
#: from an unverified sender, so an address at @theschoolcord.com here would
#: fail every password reset. Verify the root domain there before changing it.
SUPPORT_EMAIL = "noreply@send.theschoolcord.com"


def branding(request=None) -> dict:
    """Context processor: the product name on every page, signed in or out.

    Deliberately separate from ``core.context_processors.tenancy``, which
    returns early for anonymous visitors -- the landing page and the login form
    are exactly where the product's name matters most.
    """
    return {
        "product_name": PRODUCT_NAME,
        "product_tagline": PRODUCT_TAGLINE,
        "product_subhead": PRODUCT_SUBHEAD,
        "product_initial": PRODUCT_INITIAL,
        # The footer is on signed-out pages too, and it needs both.
        "contact_email": CONTACT_EMAIL,
        "social_links": SOCIAL_LINKS,
        "support_phone": SUPPORT_PHONE,
        "support_phone_tel": SUPPORT_PHONE_TEL,
        "support_whatsapp_url": SUPPORT_WHATSAPP_URL,
    }
