"""Installing to a home screen, without keeping anything private on the device."""

from __future__ import annotations

import json

from django.contrib.staticfiles import finders
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.core.roles import Role
from apps.schools.models import School, SchoolStatus


@override_settings(PWA_ENABLED=True)
class ManifestTests(TestCase):
    def manifest(self):
        return self.client.get("/manifest.webmanifest")

    def test_anyone_can_read_it(self):
        response = self.manifest()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/manifest+json")

    def test_it_opens_like_an_app_at_the_dashboard(self):
        data = json.loads(self.manifest().content)
        self.assertEqual(data["display"], "standalone")
        self.assertEqual(data["start_url"], "/dashboard/")

    def test_its_icons_exist(self):
        data = json.loads(self.manifest().content)
        sizes = {icon["sizes"] for icon in data["icons"]}
        self.assertTrue({"192x192", "512x512"} <= sizes)
        self.assertTrue(any(icon.get("purpose") == "maskable" for icon in data["icons"]))
        for name in ("icon-192.png", "icon-512.png", "icon-maskable-512.png"):
            self.assertIsNotNone(finders.find(f"img/{name}"), name)

    def test_every_page_links_it(self):
        self.assertContains(self.client.get("/accounts/login/"), 'rel="manifest"')


class ServiceWorkerTests(TestCase):
    def worker(self):
        return self.client.get("/sw.js")

    @override_settings(PWA_ENABLED=True)
    def test_it_is_served_from_the_root_so_it_covers_the_site(self):
        response = self.worker()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/javascript")

    @override_settings(PWA_ENABLED=True)
    def test_browsers_are_told_to_keep_checking_it(self):
        self.assertEqual(self.worker()["Cache-Control"], "no-cache")

    @override_settings(PWA_ENABLED=True)
    def test_only_static_files_are_ever_stored(self):
        body = self.worker().content.decode()
        self.assertEqual(body.count("cache.put("), 1)
        before_put = body[: body.index("cache.put(")]
        self.assertIn("startsWith(STATIC_PREFIX)", before_put)
        after_put = body[body.index("cache.put("):]
        self.assertNotIn("cache.put(", after_put[1:])
        self.assertNotIn("cache.add(request", body)

    @override_settings(PWA_ENABLED=True)
    def test_non_get_requests_are_left_alone(self):
        self.assertIn("request.method !== 'GET'", self.worker().content.decode())

    @override_settings(PWA_ENABLED=False)
    def test_the_off_switch_removes_it_from_devices(self):
        body = self.worker().content.decode()
        self.assertIn("unregister()", body)
        self.assertIn("caches.delete", body)
        self.assertNotIn("cache.put(", body)
        self.assertEqual(self.worker()["Cache-Control"], "no-cache")

    @override_settings(PWA_ENABLED=True)
    def test_pages_are_told_to_register_it(self):
        self.assertContains(
            self.client.get("/accounts/login/"), 'name="schoolcord-pwa" content="on"'
        )

    @override_settings(PWA_ENABLED=False)
    def test_pages_stop_registering_when_it_is_off(self):
        self.assertNotContains(self.client.get("/accounts/login/"), "schoolcord-pwa")

    def test_the_offline_page_holds_nothing_private(self):
        with open(finders.find("offline.html"), encoding="utf-8") as page:
            text = page.read()
        self.assertIn("offline", text)
        self.assertNotIn("{%", text)


class PrivatePagesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        school = School.all_objects.create(name="Alpha", status=SchoolStatus.ACTIVE)
        cls.owner = get_user_model().objects.create_user(
            "owner", password="pw", role=Role.SCHOOL_OWNER, school=school
        )

    def test_a_signed_in_page_is_not_kept_by_the_browser(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("students:student_list"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response["Cache-Control"])

    def test_a_public_page_is_not_affected_for_a_visitor(self):
        response = self.client.get(reverse("core:about"))
        self.assertNotIn("no-store", response.get("Cache-Control", ""))
