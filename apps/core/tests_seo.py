"""What crawlers and AI assistants are allowed to see, and what they are told.

The expensive failures here are silent ones: a stray noindex that drops the
home page out of Google, a robots.txt that opens the dashboards to a crawler
because nobody added the new app to it, a sitemap full of Railway URLs. None of
them raises an error, so each has a test.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from urllib.robotparser import RobotFileParser

from django.test import TestCase, override_settings
from django.urls import URLPattern, URLResolver, get_resolver, reverse

from apps.core.branding import PRODUCT_DESCRIPTION, PRODUCT_NAME
from apps.core.seo import NAMED_CRAWLERS, PRIVATE_PATHS, PUBLIC_PAGES

ORIGIN = "https://www.schoolcord.test"

PUBLIC_PATHS = [reverse(page.url_name) for page in PUBLIC_PAGES]

#: Crawler-facing files, which are public by definition.
MACHINE_PATHS = ["/robots.txt", "/sitemap.xml", "/llms.txt", "/sw.js", "/manifest.webmanifest"]


class _HeadParser(HTMLParser):
    """The <title>, <meta>, <link> and JSON-LD of a page, parsed rather than grepped."""

    def __init__(self):
        super().__init__()
        self.title = ""
        self.meta: dict[str, str] = {}
        self.links: dict[str, str] = {}
        self.json_ld: list[str] = []
        self._in_title = False
        self._in_json_ld = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = attrs.get("property") or attrs.get("name")
            if key:
                self.meta[key] = attrs.get("content", "")
        elif tag == "link" and attrs.get("rel"):
            self.links[attrs["rel"]] = attrs.get("href", "")
        elif tag == "script" and attrs.get("type") == "application/ld+json":
            self._in_json_ld = True
            self.json_ld.append("")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "script":
            self._in_json_ld = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif self._in_json_ld:
            self.json_ld[-1] += data


def _parse(html: str) -> _HeadParser:
    parser = _HeadParser()
    parser.feed(html)
    return parser


def _robots_regex(path_rule: str) -> re.Pattern:
    """A robots.txt path rule as a regex: ``*`` is any run, a trailing ``$`` anchors."""
    anchored = path_rule.endswith("$")
    body = re.escape(path_rule.rstrip("$")).replace(r"\*", ".*")
    return re.compile("^" + body + ("$" if anchored else ""))


def _longest_match_allows(rules: list[str], path: str) -> bool:
    """RFC 9309: the most specific matching rule wins, and Allow wins a tie."""
    best_length, allowed = -1, True
    for rule in rules:
        verb, _, pattern = rule.partition(": ")
        if not _robots_regex(pattern).match(path):
            continue
        is_allow = verb == "Allow"
        if len(pattern) > best_length or (len(pattern) == best_length and is_allow):
            best_length, allowed = len(pattern), is_allow
    return allowed


def _every_route(patterns=None, prefix=""):
    """Every route in the URLconf as a path, converters kept as ``<...>``.

    Stops descending once a route has a literal first segment -- the admin alone
    has hundreds of routes, and robots.txt only ever needed the prefix.
    """
    for pattern in get_resolver().url_patterns if patterns is None else patterns:
        # lstrip: the DEBUG-only media route is a regex, "^media/...".
        route = prefix + str(pattern.pattern).lstrip("^")
        if isinstance(pattern, URLResolver) and not route:
            yield from _every_route(pattern.url_patterns, route)
        elif isinstance(pattern, (URLPattern, URLResolver)):
            yield "/" + route


@override_settings(PUBLIC_BASE_URL=ORIGIN)
class RobotsTxtTests(TestCase):
    def setUp(self):
        self.response = self.client.get("/robots.txt")
        self.body = self.response.content.decode()
        self.rules = [
            line for line in self.body.splitlines()
            if line.startswith(("Allow: ", "Disallow: "))
        ]

    def test_it_is_served_as_plain_text(self):
        self.assertEqual(self.response.status_code, 200)
        self.assertEqual(self.response["Content-Type"], "text/plain; charset=utf-8")

    def test_it_points_at_the_sitemap_on_the_canonical_domain(self):
        self.assertIn(f"Sitemap: {ORIGIN}/sitemap.xml", self.body.splitlines())

    def test_the_named_crawlers_are_each_given_a_group(self):
        for crawler in ["Googlebot", "OAI-SearchBot", "ClaudeBot", "PerplexityBot",
                        "Google-Extended"]:
            with self.subTest(crawler=crawler):
                self.assertIn(f"User-agent: {crawler}", self.body.splitlines())

    def test_named_crawlers_get_the_same_rules_as_everyone_else(self):
        # A crawler ignores `*` once a group names it, so a named group without
        # the Disallows would be an open door to the dashboards.
        groups = re.split(r"\n\s*\n", self.body)
        rule_sets = [
            [line for line in group.splitlines() if line.startswith(("Allow", "Disallow"))]
            for group in groups
            if "User-agent:" in group
        ]
        self.assertEqual(len(rule_sets), 2)
        self.assertEqual(rule_sets[0], rule_sets[1])
        self.assertTrue(any(line.startswith("Disallow") for line in rule_sets[0]))

    def test_public_pages_are_crawlable(self):
        for path in PUBLIC_PATHS + MACHINE_PATHS + ["/static/img/schoolcord-logo.png"]:
            with self.subTest(path=path):
                self.assertTrue(_longest_match_allows(self.rules, path))

    def test_private_areas_are_not(self):
        for path in [
            "/admin/", "/dashboard/", "/platform/", "/school/", "/finance/",
            "/students/", "/fees/", "/payments/", "/accounts/settings/",
            "/accounts/password_reset/", "/media/receipts/1.png",
            "/fulfilled-academy/enquiry/",
        ]:
            with self.subTest(path=path):
                self.assertFalse(_longest_match_allows(self.rules, path))

    def test_first_match_parsers_reach_the_same_answer(self):
        # Python's parser applies the first matching rule and knows no
        # wildcards -- the older behaviour some crawlers still have.
        parser = RobotFileParser()
        parser.parse(self.body.splitlines())
        for crawler in NAMED_CRAWLERS + ("SomeOtherBot",):
            with self.subTest(crawler=crawler):
                for path in PUBLIC_PATHS:
                    self.assertTrue(parser.can_fetch(crawler, path), path)
                for path in ["/dashboard/", "/accounts/settings/", "/admin/"]:
                    self.assertFalse(parser.can_fetch(crawler, path), path)

    def test_every_route_is_either_public_or_disallowed(self):
        # The guard against a new app being crawlable because nobody remembered
        # robots.txt. Add its prefix to seo.PRIVATE_PATHS, or its page to
        # seo.PUBLIC_PAGES.
        forgotten = []
        for route in _every_route():
            path = re.sub(r"<[^>]+>", "*", route)
            if path in PUBLIC_PATHS or path in MACHINE_PATHS:
                continue
            if not any(_robots_regex(rule).match(path) for rule in PRIVATE_PATHS):
                forgotten.append(route)
        self.assertEqual(forgotten, [])


@override_settings(PUBLIC_BASE_URL=ORIGIN)
class SitemapTests(TestCase):
    def test_it_lists_the_public_pages_on_the_canonical_domain(self):
        response = self.client.get("/sitemap.xml")
        self.assertEqual(response.status_code, 200)
        locations = re.findall(r"<loc>([^<]+)</loc>", response.content.decode())
        self.assertEqual(locations, [ORIGIN + path for path in PUBLIC_PATHS])

    @override_settings(PUBLIC_BASE_URL="")
    def test_without_a_configured_origin_it_uses_the_request(self):
        response = self.client.get("/sitemap.xml", HTTP_HOST="localhost")
        self.assertIn("<loc>http://localhost/</loc>", response.content.decode())

    def test_every_listed_page_is_public(self):
        for path in PUBLIC_PATHS:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)


@override_settings(PUBLIC_BASE_URL=ORIGIN)
class LlmsTxtTests(TestCase):
    def setUp(self):
        self.response = self.client.get("/llms.txt")
        self.body = self.response.content.decode()

    def test_it_is_markdown_served_as_text(self):
        self.assertEqual(self.response.status_code, 200)
        self.assertEqual(self.response["Content-Type"], "text/plain; charset=utf-8")
        self.assertTrue(self.body.startswith(f"# {PRODUCT_NAME}\n"))
        self.assertIn(f"> {PRODUCT_DESCRIPTION}", self.body)

    def test_it_links_every_public_page(self):
        for page in PUBLIC_PAGES:
            with self.subTest(page=page.url_name):
                self.assertIn(f"]({ORIGIN}{reverse(page.url_name)})", self.body)

    def test_it_is_not_html_escaped(self):
        self.assertNotIn("&#x27;", self.body)
        self.assertIn("school's", self.body)


@override_settings(PUBLIC_BASE_URL=ORIGIN)
class PublicPageHeadTests(TestCase):
    def head(self, path):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        return response, _parse(response.content.decode())

    def test_each_public_page_has_its_own_title_and_description(self):
        for page in PUBLIC_PAGES:
            with self.subTest(page=page.url_name):
                _, head = self.head(reverse(page.url_name))
                self.assertEqual(head.title, page.title)
                self.assertEqual(head.meta["description"], page.description)
                self.assertEqual(head.meta["og:title"], page.title)
                self.assertEqual(head.meta["og:description"], page.description)
                self.assertEqual(head.meta["twitter:title"], page.title)

    def test_the_home_page_describes_the_product(self):
        _, head = self.head("/")
        self.assertIn(PRODUCT_NAME, head.title)
        self.assertIn("nigerian schools", head.title.lower())
        self.assertTrue(
            head.meta["description"].startswith("Run Your School. Not Your Paperwork.")
        )
        for phrase in ["school fees", "student records", "communicate with parents",
                       "Nigerian schools"]:
            self.assertIn(phrase, head.meta["description"])

    def test_link_previews_use_the_logo_at_an_absolute_url(self):
        _, head = self.head("/")
        self.assertEqual(head.meta["og:image"], f"{ORIGIN}/static/img/schoolcord-logo.png")
        self.assertEqual(head.meta["twitter:image"], head.meta["og:image"])
        self.assertEqual(head.meta["twitter:card"], "summary")
        self.assertEqual(head.meta["og:url"], f"{ORIGIN}/")

    def test_the_canonical_url_drops_the_query_string(self):
        _, head = self.head("/accounts/login/?next=/fees/")
        self.assertEqual(head.links["canonical"], f"{ORIGIN}/accounts/login/")

    def test_no_public_page_is_noindexed(self):
        # sitemap.xml is left out on purpose: Django sends it with
        # `X-Robots-Tag: noindex`, which keeps the XML itself out of search
        # results without stopping crawlers reading the URLs in it.
        for path in PUBLIC_PATHS + ["/robots.txt", "/llms.txt"]:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertNotIn("X-Robots-Tag", response.headers)
                if path in PUBLIC_PATHS:
                    robots = _parse(response.content.decode()).meta.get("robots", "")
                    self.assertNotIn("noindex", robots)
                    self.assertNotIn("nofollow", robots)

    def test_public_pages_are_server_rendered(self):
        # What a crawler that runs no JavaScript receives: the real copy, not a
        # shell waiting for a script to fill it in.
        expected = {
            # The hero headline. The snippet is here to prove the copy is in
            # the HTML a crawler receives, so it has to be the copy that is
            # actually on the page.
            "/": ["Run Your School. Not Your Paperwork.",
                  "One school term, without the paperwork.",
                  "How much could your school recover?"],
            "/signup/": ["Create your school account", 'name="school_name"'],
            "/accounts/login/": ["Sign in", "Email or username"],
        }
        for path, snippets in expected.items():
            with self.subTest(path=path):
                response = self.client.get(path)
                for snippet in snippets:
                    self.assertContains(response, snippet)

    def test_other_pages_fall_back_to_the_product_description(self):
        _, head = self.head(reverse("password_reset"))
        self.assertEqual(head.title, f"Reset your password · {PRODUCT_NAME}")
        self.assertEqual(head.meta["description"], PRODUCT_DESCRIPTION)
        self.assertEqual(head.meta["og:title"], PRODUCT_NAME)


@override_settings(PUBLIC_BASE_URL=ORIGIN)
class StructuredDataTests(TestCase):
    def setUp(self):
        head = _parse(self.client.get("/").content.decode())
        self.assertEqual(len(head.json_ld), 1)
        self.data = json.loads(head.json_ld[0])
        self.nodes = {node["@type"]: node for node in self.data["@graph"]}

    def test_it_marks_up_the_organization_and_the_software(self):
        self.assertEqual(self.data["@context"], "https://schema.org")
        self.assertEqual(set(self.nodes), {"Organization", "SoftwareApplication"})
        for node in self.nodes.values():
            self.assertEqual(node["name"], PRODUCT_NAME)
            self.assertEqual(node["description"], PRODUCT_DESCRIPTION)
            self.assertEqual(node["url"], f"{ORIGIN}/")

    def test_the_logo_is_absolute(self):
        logo = f"{ORIGIN}/static/img/schoolcord-logo.png"
        self.assertEqual(self.nodes["Organization"]["logo"]["url"], logo)
        self.assertEqual(self.nodes["SoftwareApplication"]["image"], logo)

    def test_the_software_is_published_by_the_organization(self):
        self.assertEqual(
            self.nodes["SoftwareApplication"]["publisher"]["@id"],
            self.nodes["Organization"]["@id"],
        )

    def test_only_the_home_page_carries_it(self):
        for path in ["/signup/", "/accounts/login/"]:
            with self.subTest(path=path):
                self.assertEqual(_parse(self.client.get(path).content.decode()).json_ld, [])

    def test_a_script_tag_in_the_data_cannot_close_the_block(self):
        from apps.core import seo

        with self.settings(PUBLIC_BASE_URL="https://x.test/</script><b>"):
            encoded = seo.structured_data(None)
        self.assertNotIn("</script>", encoded)
        self.assertIn("\\u003C/script\\u003E", encoded)
