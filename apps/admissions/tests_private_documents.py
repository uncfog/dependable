"""An applicant's documents are reached through the app, never the bucket.

A child's birth certificate and photograph were linked straight from storage --
the passport photo by its bucket URL, on a bucket that was public, at a path
built from the school id and a sequential reference. These tests hold the
replacement in place: one gated view, the same tenant scoping as the applicant
itself, and no storage URL in the page.
"""

from __future__ import annotations

import shutil
import tempfile
from io import BytesIO
from unittest import mock

from django.core.files import File
from django.core.files.base import ContentFile
from django.test import override_settings
from django.urls import reverse

from apps.admissions.tests import AdmissionsTestCase

PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6360000002000100ffff03000006"
    "000557bfabd40000000049454e44ae426082"
)

#: An S3 backend that is never contacted: building one makes no request, and
#: the one test that reads through it patches the read.
S3_STORAGES = {
    "default": {
        "BACKEND": "storages.backends.s3.S3Storage",
        "OPTIONS": {
            "bucket_name": "media",
            "endpoint_url": "https://storage.example.invalid/storage/v1/s3",
            "access_key": "test-key",
            "secret_key": "test-secret",
            "region_name": "us-east-1",
            "addressing_style": "path",
            "querystring_auth": True,
        },
    },
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


class ApplicantDocumentViewTests(AdmissionsTestCase):
    def setUp(self):
        self.media = tempfile.mkdtemp()
        self.override = override_settings(MEDIA_ROOT=self.media)
        self.override.enable()
        self.applicant = self.make_applicant()
        self.applicant.passport_photo.save("photo.png", ContentFile(PNG_1PX), save=True)

    def tearDown(self):
        self.override.disable()
        shutil.rmtree(self.media, ignore_errors=True)

    def url(self, kind="passport_photo", applicant=None):
        return reverse(
            "admissions:applicant_document",
            args=[(applicant or self.applicant).pk, kind],
        )

    def test_staff_who_can_read_the_pipeline_get_the_file(self):
        for user in (self.alpha_owner, self.north_principal):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                response = self.client.get(self.url())
                self.assertEqual(response.status_code, 200)
                self.assertEqual(b"".join(response.streaming_content), PNG_1PX)
                self.assertEqual(response["Content-Type"], "image/png")

    def test_it_is_never_cached_and_never_runs_as_a_page(self):
        self.client.force_login(self.alpha_owner)
        response = self.client.get(self.url())
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertIn("sandbox", response["Content-Security-Policy"])

    def test_another_school_gets_a_404(self):
        self.client.force_login(self.beta_principal)
        self.assertEqual(self.client.get(self.url()).status_code, 404)

    def test_another_campus_gets_a_404(self):
        from apps.accounts.models import User
        from apps.core.roles import Role

        south = User.objects.create_user(
            "south.principal", password="pw", role=Role.PRINCIPAL,
            school=self.alpha, branch=self.south,
        )
        self.client.force_login(south)
        self.assertEqual(self.client.get(self.url()).status_code, 404)

    def test_signed_out_is_sent_to_sign_in(self):
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_a_bursar_the_school_has_not_let_in_is_refused(self):
        self.client.force_login(self.north_bursar)
        self.assertEqual(self.client.get(self.url()).status_code, 403)

    def test_only_document_fields_can_be_asked_for(self):
        """The kind is looked up on the model, so it must not reach any attribute."""
        self.client.force_login(self.alpha_owner)
        for kind in ("first_name", "parent_phone", "school", "delete"):
            with self.subTest(kind=kind):
                self.assertEqual(self.client.get(self.url(kind)).status_code, 404)

    def test_a_document_that_was_not_uploaded_is_a_404(self):
        self.client.force_login(self.alpha_owner)
        self.assertEqual(
            self.client.get(self.url("birth_certificate")).status_code, 404
        )

    def test_the_detail_page_shows_the_photo_through_the_gate(self):
        self.client.force_login(self.alpha_owner)
        page = self.client.get(
            reverse("admissions:applicant_detail", args=[self.applicant.pk])
        ).content.decode()
        self.assertIn(self.url(), page)
        self.assertNotIn(self.applicant.passport_photo.name, page)
        self.assertNotIn("/media/", page)


@override_settings(STORAGES=S3_STORAGES)
class ApplicantDocumentsOnAPrivateBucketTests(AdmissionsTestCase):
    """The production shape: the file lives in a private S3 bucket."""

    def setUp(self):
        from apps.admissions.models import Applicant

        self.applicant = self.make_applicant()
        Applicant.all_objects.filter(pk=self.applicant.pk).update(
            passport_photo="admissions/1/ENQ-2026-0001/photo.png"
        )
        self.applicant.refresh_from_db()

    def test_the_page_carries_no_storage_url(self):
        self.client.force_login(self.alpha_owner)
        page = self.client.get(
            reverse("admissions:applicant_detail", args=[self.applicant.pk])
        ).content.decode()
        self.assertNotIn("storage.example.invalid", page)
        self.assertNotIn("X-Amz-", page)
        self.assertIn(
            reverse("admissions:applicant_document", args=[self.applicant.pk, "passport_photo"]),
            page,
        )

    def test_the_view_streams_it_with_the_apps_own_credentials(self):
        from storages.backends.s3 import S3Storage

        stored = File(BytesIO(PNG_1PX), name="admissions/1/ENQ-2026-0001/photo.png")
        with mock.patch.object(S3Storage, "_open", return_value=stored) as opened:
            self.client.force_login(self.north_principal)
            response = self.client.get(
                reverse(
                    "admissions:applicant_document",
                    args=[self.applicant.pk, "passport_photo"],
                )
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(b"".join(response.streaming_content), PNG_1PX)
        opened.assert_called_once()
        self.assertEqual(opened.call_args.args[0], "admissions/1/ENQ-2026-0001/photo.png")
