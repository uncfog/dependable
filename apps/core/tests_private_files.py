"""Private uploads: the logo view, the private-bucket default, and the signing key.

Every upload is streamed by the app (apps/core/files.py), so the bucket can be
private. These are the pieces of that which are not specific to one module --
the school logo, which is the one upload meant to be seen by anybody, and the
settings that make the bucket private and production refuse a placeholder key.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
from io import BytesIO
from unittest import mock

from django.conf import settings
from django.core.files import File
from django.core.files.base import ContentFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import User
from apps.admissions.tests_private_documents import PNG_1PX, S3_STORAGES
from apps.core.roles import Role
from apps.schools.models import Branch, School

SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


class SchoolLogoViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = School.all_objects.create(name="Fulfilled Academy")
        cls.branch = Branch.all_objects.create(school=cls.school, name="Main Campus")
        cls.owner = User.objects.create_user(
            "owner", password="pw", role=Role.SCHOOL_OWNER, school=cls.school
        )

    def setUp(self):
        self.media = tempfile.mkdtemp()
        self.override = override_settings(MEDIA_ROOT=self.media)
        self.override.enable()
        self.school.logo.save("crest.png", ContentFile(PNG_1PX), save=True)

    def tearDown(self):
        self.override.disable()
        shutil.rmtree(self.media, ignore_errors=True)

    def test_anybody_can_fetch_it(self):
        """It is on the school's public enquiry page, signed out."""
        response = self.client.get(self.school.logo_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), PNG_1PX)
        self.assertEqual(response["Content-Type"], "image/png")

    def test_the_current_version_is_cached_for_a_year(self):
        response = self.client.get(self.school.logo_url)
        self.assertEqual(response["Cache-Control"], "public, max-age=31536000, immutable")

    def test_a_stale_or_missing_version_is_cached_briefly(self):
        path = reverse("core:school_logo", args=[self.school.slug])
        for query in ("", "?v=stale"):
            with self.subTest(query=query):
                response = self.client.get(path + query)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response["Cache-Control"], "public, max-age=300")

    def test_a_new_logo_gets_a_new_url(self):
        before = self.school.logo_url
        self.school.logo.save("crest.png", ContentFile(PNG_1PX), save=True)
        self.assertNotEqual(self.school.logo_url, before)

    def test_an_svg_logo_cannot_run_as_a_page(self):
        self.school.logo.save("crest.svg", ContentFile(SVG), save=True)
        response = self.client.get(self.school.logo_url)
        self.assertEqual(response["Content-Type"], "image/svg+xml")
        self.assertIn("sandbox", response["Content-Security-Policy"])
        self.assertIn("default-src 'none'", response["Content-Security-Policy"])

    def test_no_logo_and_no_school_are_404s(self):
        other = School.all_objects.create(name="Rival College")
        self.assertEqual(other.logo_url, "")
        for path in (
            reverse("core:school_logo", args=[other.slug]),
            reverse("core:school_logo", args=["no-such-school"]),
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)


@override_settings(STORAGES=S3_STORAGES)
class LogoOnAPrivateBucketTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = School.all_objects.create(name="Fulfilled Academy")
        cls.branch = Branch.all_objects.create(school=cls.school, name="Main Campus")
        cls.owner = User.objects.create_user(
            "owner", password="pw", role=Role.SCHOOL_OWNER, school=cls.school
        )
        School.all_objects.filter(pk=cls.school.pk).update(
            logo="logos/fulfilled-academy/crest.png"
        )
        cls.school.refresh_from_db()

    def test_signed_in_screens_carry_no_storage_url(self):
        self.client.force_login(self.owner)
        for path in (reverse("core:school_dashboard"), reverse("core:school_settings")):
            with self.subTest(path=path):
                page = self.client.get(path).content.decode()
                self.assertIn(self.school.logo_url.replace("&", "&amp;"), page)
                self.assertNotIn("storage.example.invalid", page)
                self.assertNotIn("X-Amz-", page)

    def test_the_view_streams_it_from_the_bucket(self):
        from storages.backends.s3 import S3Storage

        stored = File(BytesIO(PNG_1PX), name="logos/fulfilled-academy/crest.png")
        with mock.patch.object(S3Storage, "_open", return_value=stored):
            response = self.client.get(self.school.logo_url)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(b"".join(response.streaming_content), PNG_1PX)


class PrivateBucketSettingTests(SimpleTestCase):
    def test_a_configured_bucket_is_private_unless_said_otherwise(self):
        source = (
            pathlib.Path(settings.BASE_DIR) / "config" / "settings" / "base.py"
        ).read_text(encoding="utf-8")
        self.assertIn('AWS_QUERYSTRING_AUTH = env_bool("MEDIA_PRIVATE_BUCKET", True)', source)

    def test_no_template_links_an_upload_by_its_storage_url(self):
        """``.url`` on an upload is a bucket link; every one goes through a view."""
        root = pathlib.Path(settings.BASE_DIR) / "templates"
        offenders = []
        for template in root.rglob("*.html"):
            text = template.read_text(encoding="utf-8")
            for field in ("logo.url", "receipt.url", "passport_photo.url",
                          "birth_certificate.url", "previous_results.url",
                          "transfer_letter.url", "immunisation_record.url"):
                if field in text:
                    offenders.append(f"{template.relative_to(root)}: {field}")
        self.assertEqual(offenders, [])


class SecretKeyTests(SimpleTestCase):
    def test_placeholders_are_recognised(self):
        from config.settings.base import is_placeholder_secret_key

        for value in (None, "", "   ", "dev-insecure-change-me",
                      "django-insecure-0123456789abcdef"):
            with self.subTest(value=value):
                self.assertTrue(is_placeholder_secret_key(value))
        self.assertFalse(is_placeholder_secret_key("k3J9-real-random-value-" + "x" * 40))

    def boot_production(self, secret_key: str) -> subprocess.CompletedProcess:
        """Load the production settings in a fresh interpreter.

        A subprocess because settings run once per process. ``SECRET_KEY`` is
        set explicitly -- even when empty -- so a developer's ``.env`` cannot
        fill it in (python-dotenv never overrides a variable that is present).
        """
        env = {**os.environ, "SECRET_KEY": secret_key, "DB_ENGINE": "sqlite",
               "DJANGO_SETTINGS_MODULE": "config.settings.prod"}
        return subprocess.run(
            [sys.executable, "-c", "import django; django.setup()"],
            cwd=settings.BASE_DIR, env=env, capture_output=True, text=True,
            timeout=120,
        )

    def test_production_refuses_to_start_without_a_real_key(self):
        for value in ("", "dev-insecure-change-me"):
            with self.subTest(value=value):
                result = self.boot_production(value)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("ImproperlyConfigured", result.stderr)
                self.assertIn("SECRET_KEY", result.stderr)

    def test_production_starts_with_one(self):
        result = self.boot_production("k3J9-real-random-value-" + "x" * 40)
        self.assertEqual(result.returncode, 0, result.stderr)
