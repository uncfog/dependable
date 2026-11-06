"""What a school's status actually does to the people who work there.

Suspended and cancelled schools are locked out. A past-due school can read its
records and export them, but changes nothing. The platform is never locked out,
and nothing is deleted either way.
"""

from __future__ import annotations

from io import StringIO
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.academics.models import Class, Level
from apps.admissions.models import Applicant
from apps.core.roles import Role
from apps.schools.models import Branch, School, SchoolStatus
from apps.students.models import Sex

User = get_user_model()


class SchoolStatusTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = School.all_objects.create(
            name="Alpha", status=SchoolStatus.ACTIVE
        )
        cls.branch = Branch.all_objects.create(school=cls.school, name="North")
        cls.bursar = User.objects.create_user(
            "bursar", password="pw", role=Role.BURSAR, school=cls.school, branch=cls.branch
        )
        cls.owner = User.objects.create_user(
            "owner", password="pw", role=Role.SCHOOL_OWNER, school=cls.school
        )
        cls.platform = User.objects.create_user(
            "platform", password="pw", role=Role.PLATFORM_OWNER
        )

    def set_status(self, status):
        School.all_objects.filter(pk=self.school.pk).update(status=status)


class LockedOutTests(SchoolStatusTestCase):
    def test_suspended_and_cancelled_staff_see_the_notice_instead_of_their_screens(self):
        for status in (SchoolStatus.SUSPENDED, SchoolStatus.CANCELLED):
            with self.subTest(status=status):
                self.set_status(status)
                self.client.force_login(self.bursar)
                for url in ("/finance/", "/payments/", "/students/", "/accounts/settings/"):
                    response = self.client.get(url)
                    self.assertEqual(response.status_code, 403, url)
                    self.assertContains(
                        response, "is not available", status_code=403
                    )

    def test_nothing_can_be_changed(self):
        self.set_status(SchoolStatus.SUSPENDED)
        self.client.force_login(self.bursar)

        response = self.client.post(reverse("payments:record"), {"amount": "1"})

        self.assertEqual(response.status_code, 403)

    def test_the_notice_says_nothing_was_deleted_and_offers_sign_out(self):
        self.set_status(SchoolStatus.SUSPENDED)
        self.client.force_login(self.owner)

        response = self.client.get("/school/")

        self.assertContains(response, "Nothing has been deleted", status_code=403)
        self.assertContains(response, reverse("logout"), status_code=403)

    def test_signing_in_and_out_still_work(self):
        self.set_status(SchoolStatus.SUSPENDED)

        login = self.client.post(
            reverse("login"), {"username": "bursar", "password": "pw"}
        )
        self.assertEqual(login.status_code, 302)

        logout = self.client.post(reverse("logout"))
        self.assertEqual(logout.status_code, 302)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_public_pages_are_unaffected(self):
        self.set_status(SchoolStatus.SUSPENDED)
        self.client.force_login(self.bursar)

        for url in ("/privacy/", "/about/", "/robots.txt"):
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_a_held_temporary_password_does_not_get_round_the_lockout(self):
        self.set_status(SchoolStatus.SUSPENDED)
        self.bursar.must_change_password = True
        self.bursar.save()
        self.client.force_login(self.bursar)

        self.assertEqual(self.client.get("/finance/").status_code, 403)


class PastDueIsReadOnlyTests(SchoolStatusTestCase):
    def setUp(self):
        self.set_status(SchoolStatus.PAST_DUE)
        self.client.force_login(self.bursar)

    def test_screens_still_open(self):
        for url in ("/finance/", "/payments/", "/students/"):
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_changes_are_refused_and_explained(self):
        response = self.client.post(reverse("payments:record"), {"amount": "1"})

        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "read-only", status_code=403)

    def test_a_person_can_still_look_after_their_own_account(self):
        response = self.client.post(
            reverse("accounts:settings"),
            {"form": "profile", "first_name": "Ada", "last_name": "Obi", "job_title": ""},
        )

        self.assertEqual(response.status_code, 302)

    def test_signing_out_still_works(self):
        self.assertEqual(self.client.post(reverse("logout")).status_code, 302)


class UnaffectedTests(SchoolStatusTestCase):
    def test_active_and_trialing_schools_work_normally(self):
        for status in (SchoolStatus.ACTIVE, SchoolStatus.TRIALING):
            with self.subTest(status=status):
                self.set_status(status)
                self.client.force_login(self.bursar)
                self.assertEqual(self.client.get("/finance/").status_code, 200)

    def test_the_platform_can_still_open_a_locked_school(self):
        self.set_status(SchoolStatus.SUSPENDED)
        self.client.force_login(self.platform)

        response = self.client.get(
            reverse("core:platform_school", args=[self.school.pk])
        )

        self.assertEqual(response.status_code, 200)

    def test_another_schools_status_is_not_this_schools(self):
        other = School.all_objects.create(name="Bravo", status=SchoolStatus.SUSPENDED)
        self.assertNotEqual(other.pk, self.school.pk)
        self.client.force_login(self.bursar)

        self.assertEqual(self.client.get("/finance/").status_code, 200)

    def test_signed_out_visitors_are_sent_to_sign_in_as_before(self):
        self.set_status(SchoolStatus.SUSPENDED)

        self.assertEqual(self.client.get("/payments/").status_code, 302)


class ParentMailStopsTests(SchoolStatusTestCase):
    @override_settings(PUBLIC_BASE_URL="https://example.test")
    def test_a_suspended_school_does_not_chase_parents(self):
        klass = Class.all_objects.create(
            branch=self.branch, name="JSS 1", level=Level.JUNIOR_SECONDARY, year_in_level=1
        )
        Applicant.all_objects.create(
            branch=self.branch, school=self.school, first_name="New", last_name="Child",
            sex=Sex.FEMALE, level=Level.JUNIOR_SECONDARY, school_class=klass,
            parent_name="P", parent_phone="08031234567",
            parent_email="parent@example.test",
        )
        Applicant.all_objects.update(created_at=timezone.now() - timedelta(days=30))
        self.set_status(SchoolStatus.SUSPENDED)

        call_command("send_enquiry_reminders", stdout=StringIO())

        self.assertEqual(len(mail.outbox), 0)
