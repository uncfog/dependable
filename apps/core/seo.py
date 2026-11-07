"""What search engines and AI assistants are told about SCHOOLCORD.

Four surfaces describe the public site to machines -- the ``<head>`` of each
public page, ``robots.txt``, ``sitemap.xml`` and ``llms.txt`` -- and each of
them used to be the kind of file that drifts: a page added to the sitemap but
not to robots, a description rewritten on the page but not in the JSON-LD. So
all four read from the tables in this module:

* :data:`PUBLIC_PAGES` is the whole public site. Its titles and descriptions go
  into the ``<head>``, its URLs into the sitemap and ``llms.txt``, and robots
  explicitly allows each of them.
* :data:`PRIVATE_PATHS` is everything behind a login, plus each school's own
  pages. ``tests_seo`` walks the URLconf and fails if a route is in neither
  list, so a new app cannot become crawlable by being forgotten.
* :data:`FEATURES` is what the product does, shared by the structured data and
  ``llms.txt``.

Absolute URLs are built from ``PUBLIC_BASE_URL`` (``https://www.theschoolcord.com``
in production) rather than from the request, so the Railway-generated domain
that also serves the app points search engines at the real address instead of
competing with it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.sitemaps import Sitemap
from django.templatetags.static import static
from django.urls import reverse
from django.utils.safestring import mark_safe
from django.views.generic import TemplateView

from .branding import PRODUCT_DESCRIPTION, PRODUCT_NAME

# ===========================================================================
# The tables
# ===========================================================================


@dataclass(frozen=True)
class PublicPage:
    url_name: str
    #: Link text in llms.txt.
    label: str
    #: The whole <title>, brand included. Also og:title and twitter:title.
    title: str
    description: str


PUBLIC_PAGES: tuple[PublicPage, ...] = (
    PublicPage(
        url_name="core:landing",
        label="Home",
        title=f"{PRODUCT_NAME} — School Fee Management Software for Nigerian Schools",
        description=PRODUCT_DESCRIPTION,
    ),
    PublicPage(
        url_name="core:signup",
        label="Create a school account",
        title=f"Create your school account · {PRODUCT_NAME}",
        description=(
            f"Set up your school on {PRODUCT_NAME}: create the school, its first "
            "campus and your owner account, then add classes, fees and students. "
            "No card needed."
        ),
    ),
    PublicPage(
        url_name="core:about",
        label="About",
        title=f"About {PRODUCT_NAME}",
        description=(
            f"Run Your School. Not Your Paperwork. Who builds {PRODUCT_NAME}, "
            "why, and how to reach us. School management software for Nigerian "
            "schools."
        ),
    ),
    PublicPage(
        url_name="core:privacy",
        label="Privacy policy",
        title=f"Privacy policy · {PRODUCT_NAME}",
        description=(
            "What student, parent and staff data a school holds in "
            f"{PRODUCT_NAME}, who it is shared with, how long it is kept, and "
            "how to ask to see, correct or delete it under Nigeria's NDPA."
        ),
    ),
    PublicPage(
        url_name="login",
        label="Sign in",
        title=f"Sign in · {PRODUCT_NAME}",
        description=(
            f"Sign in to {PRODUCT_NAME} to manage your school's fees, student "
            "records and parent messages."
        ),
    ),
)

_PAGES_BY_URL_NAME = {page.url_name: page for page in PUBLIC_PAGES}

#: Path prefixes crawlers are asked to stay out of. Every one of them either
#: needs a signed-in account or belongs to a single school rather than to the
#: product. ``/media/`` holds uploaded receipts.
PRIVATE_PATHS: tuple[str, ...] = (
    "/admin/",
    "/accounts/",
    "/branches/",
    "/staff/",
    "/dashboard/",
    "/platform/",
    "/school/",
    "/branch/",
    "/finance/",
    "/welcome/",
    "/settings/",
    "/academics/",
    "/fees/",
    "/students/",
    "/families/",
    "/onboarding/",
    "/messaging/",
    "/payments/",
    "/admissions/",
    "/reports/",
    "/media/",
    # A school's logo, /schools/<school-slug>/logo/. Fetched by browsers on the
    # school's pages; nothing a search index needs.
    "/schools/",
    # Each school's own public enquiry page, /<school-slug>/enquiry/. Public to
    # parents, but it is the school's page, not the product's, and it already
    # carries its own noindex.
    "/*/enquiry/",
)

#: Crawlers named in robots.txt. They get exactly the rules ``*`` gets; naming
#: them is about being explicit. It has to be the *same* rules, repeated: a
#: crawler that finds a group with its own name ignores the ``*`` group
#: entirely, so a bare "Allow: /" here would open the dashboards to it.
NAMED_CRAWLERS: tuple[str, ...] = (
    # Search.
    "Googlebot",
    "Bingbot",
    # Controls whether Gemini and Vertex AI may use the pages. A robots.txt
    # token rather than a crawler of its own; Search itself is Googlebot.
    "Google-Extended",
    # AI search and assistants.
    "OAI-SearchBot",
    "ChatGPT-User",
    "ClaudeBot",
    "Claude-SearchBot",
    "Claude-User",
    "PerplexityBot",
    "Perplexity-User",
)

#: What the product does, in the order a school would care about it.
FEATURES: tuple[tuple[str, str], ...] = (
    (
        "School fee management",
        "Set each class's fees per term, record payments, and see every "
        "student's balance at a glance. Balances are always worked out from the "
        "fee structure and confirmed payments, never typed in.",
    ),
    (
        "Student records",
        "One roster per campus, with Excel import for a school's existing "
        "student list.",
    ),
    (
        "Parent messaging",
        "SMS to parents -- for example, a reminder to every parent who still "
        "owes -- sent under the school's own registered Sender ID.",
    ),
    (
        "Multiple campuses",
        "One school account runs as many branches as the school has.",
    ),
    (
        "Role-based access",
        "Owners, principals and bursars each see only what their role needs; a "
        "bursar records payments without being able to change the fees.",
    ),
    (
        "Admissions (add-on)",
        "A public enquiry page for each school and an enquiry-to-enrolment "
        "pipeline.",
    ),
)


# ===========================================================================
# Addresses
# ===========================================================================


def site_origin(request=None) -> str:
    """``https://www.theschoolcord.com``, or wherever this request came in."""
    if settings.PUBLIC_BASE_URL:
        return settings.PUBLIC_BASE_URL
    if request is not None:
        return f"{request.scheme}://{request.get_host()}"
    return ""


def social_image_url(request=None) -> str:
    """The product logo, absolute -- link previews will not follow a relative URL."""
    return site_origin(request) + static("img/schoolcord-logo.png")


# ===========================================================================
# The <head>
# ===========================================================================


def seo(request) -> dict:
    """Context processor: title, description and canonical URL for the <head>.

    The public pages get theirs from :data:`PUBLIC_PAGES`; every other page
    gets ``seo_title`` as None, and base.html falls back to its own title
    block and the product description.
    """
    match = getattr(request, "resolver_match", None)
    page = _PAGES_BY_URL_NAME.get(match.view_name) if match else None
    origin = site_origin(request)
    return {
        "seo_title": page.title if page else None,
        "seo_description": page.description if page else PRODUCT_DESCRIPTION,
        # request.path, not the full URL: ?next=... on the login page must not
        # become a second address for the same page.
        "canonical_url": origin + request.path,
        "social_image_url": social_image_url(request),
    }


def structured_data(request) -> str:
    """schema.org JSON-LD for the home page: the organisation and its product.

    Returned ready to drop inside ``<script type="application/ld+json">``.
    ``<``, ``>`` and ``&`` are escaped the way Django's ``json_script`` does it,
    so no string in here can close the script tag early.
    """
    origin = site_origin(request)
    logo = social_image_url(request)
    organization_id = f"{origin}/#organization"
    data = {
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "Organization",
                "@id": organization_id,
                "name": PRODUCT_NAME,
                "url": f"{origin}/",
                "logo": {"@type": "ImageObject", "url": logo},
                "description": PRODUCT_DESCRIPTION,
                "areaServed": {"@type": "Country", "name": "Nigeria"},
            },
            {
                "@type": "SoftwareApplication",
                "@id": f"{origin}/#software",
                "name": PRODUCT_NAME,
                "url": f"{origin}/",
                "image": logo,
                "description": PRODUCT_DESCRIPTION,
                "applicationCategory": "BusinessApplication",
                "applicationSubCategory": "School management software",
                "operatingSystem": "Web browser",
                "featureList": [name for name, _ in FEATURES],
                "audience": {
                    "@type": "Audience",
                    "audienceType": "Nursery, primary and secondary schools in Nigeria",
                },
                "publisher": {"@id": organization_id},
            },
        ],
    }
    encoded = json.dumps(data, ensure_ascii=False, indent=2)
    for char, escape in (("<", "\\u003C"), (">", "\\u003E"), ("&", "\\u0026")):
        encoded = encoded.replace(char, escape)
    return mark_safe(encoded)


# ===========================================================================
# sitemap.xml, robots.txt, llms.txt
# ===========================================================================


class PublicPageSitemap(Sitemap):
    """Every page in :data:`PUBLIC_PAGES`, at its canonical address.

    Without the sites framework Django takes the domain from the request, which
    on the Railway domain would publish Railway URLs. ``PUBLIC_BASE_URL`` wins
    when it is set.
    """

    def items(self):
        return [page.url_name for page in PUBLIC_PAGES]

    def location(self, item):
        return reverse(item)

    def get_protocol(self, protocol=None):
        return urlsplit(settings.PUBLIC_BASE_URL).scheme or super().get_protocol(protocol)

    def get_domain(self, site=None):
        return urlsplit(settings.PUBLIC_BASE_URL).netloc or super().get_domain(site)


SITEMAPS = {"public": PublicPageSitemap}


def robots_rules() -> list[str]:
    """The Allow and Disallow lines every group in robots.txt carries.

    Allows first. Modern crawlers apply the most specific matching rule wherever
    it sits, but older parsers take the first match -- and for those,
    ``/accounts/login/`` has to be seen before ``/accounts/`` shuts it.
    """
    allows = ["/$"]  # The home page itself, not everything beneath it.
    allows += [reverse(page.url_name) for page in PUBLIC_PAGES if reverse(page.url_name) != "/"]
    allows += ["/llms.txt", "/static/"]
    return [f"Allow: {path}" for path in allows] + [
        f"Disallow: {path}" for path in PRIVATE_PATHS
    ]


class RobotsTxtView(TemplateView):
    template_name = "seo/robots.txt"
    content_type = "text/plain; charset=utf-8"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            named_crawlers=NAMED_CRAWLERS,
            rules=robots_rules(),
            sitemap_url=site_origin(self.request) + reverse("sitemap"),
        )
        return context


class LlmsTxtView(TemplateView):
    """``/llms.txt``: a short Markdown summary for language models. See llmstxt.org."""

    template_name = "seo/llms.txt"
    content_type = "text/plain; charset=utf-8"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        origin = site_origin(self.request)
        context.update(
            product_description=PRODUCT_DESCRIPTION,
            features=FEATURES,
            pages=[
                {
                    "label": page.label,
                    "url": origin + reverse(page.url_name),
                    "description": page.description,
                }
                for page in PUBLIC_PAGES
            ],
            sitemap_url=origin + reverse("sitemap"),
        )
        return context
