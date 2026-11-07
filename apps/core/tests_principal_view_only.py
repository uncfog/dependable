"""A principal reads the school's setup and does not change it.

From pilot testing: fee structures, school settings and classes are the school
owner's to change (and the platform's, on their behalf). A principal still sees
all three, and keeps the day-to-day money work -- recording payments, reading
them and the receipts attached to them, and reading the roster.

The bursar's access is not part of this change, and the last class pins it so
that it stays that way.
"""

from __future__ import annotations

import shutil
import tempfile
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.academics.models import Class, Level
from apps.fees.models import FeeComponent, FeeStructure, Term, TermSequence
from apps.payments.models import Payment, PaymentStatus
from apps.schools.models import Branch, School
from apps.students.models import Sex, Student

from .permissions import Capability, capabilities_for, has_capability
from .roles import Role

User = get_user_model()


class SetupFixtures(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = School.all_objects.create(
            name="Alpha Schools", contact_email="office@alpha.test",
            contact_phone="08031234567",
        )
        cls.north = Branch.all_objects.create(school=cls.school, name="North")

        cls.platform = User.objects.create_user(
            "platform", password="pw", role=Role.PLATFORM_OWNER,
        )
        cls.owner = User.objects.create_user(
            "owner", password="pw", role=Role.SCHOOL_OWNER, school=cls.school,
        )
        cls.principal = User.objects.create_user(
            "principal", password="pw", role=Role.PRINCIPAL,
            school=cls.school, branch=cls.north,
        )
        cls.bursar = User.objects.create_user(
            "bursar", password="pw", role=Role.BURSAR,
            school=cls.school, branch=cls.north,
        )

        cls.jss1 = Class.all_objects.create(
            branch=cls.north, name="JSS 1", level=Level.JUNIOR_SECONDARY,
            year_in_level=1,
        )
        cls.term = Term.all_objects.create(
            branch=cls.north, name="First Term 2025/2026", academic_year="2025/2026",
            sequence=TermSequence.FIRST, is_current=True,
        )
        cls.structure = FeeStructure.all_objects.create(
            school_class=cls.jss1, term=cls.term,
        )
        FeeComponent.all_objects.create(
            fee_structure=cls.structure, name="School Fees", amount=Decimal("50000"),
        )
        cls.ada = Student.all_objects.create(
            branch=cls.north, school_class=cls.jss1, admission_number="N/001",
            first_name="Ada", last_name="Obi", sex=Sex.FEMALE,
            parent_name="Parent of Ada", parent_phone="08031234567",
        )

    #: Every screen that changes fees, classes or the school's profile, as
    #: (url name, args) -- args are resolved against the fixture by name.
    EDIT_SCREENS = (
        ("fees:structure_create", ()),
        ("fees:structure_update", ("structure",)),
        ("fees:structure_delete", ("structure",)),
        ("fees:structure_import", ()),
        ("fees:term_create", ()),
        ("fees:term_update", ("term",)),
        ("academics:class_create", ()),
        ("academics:class_bulk_create", ()),
        ("academics:class_edit_all", ()),
        ("academics:class_update", ("jss1",)),
        ("academics:class_delete", ("jss1",)),
        ("academics:class_import", ()),
        ("academics:subject_create", ()),
    )

    def edit_urls(self):
        for name, args in self.EDIT_SCREENS:
            yield reverse(name, args=[getattr(self, a).pk for a in args])


class CapabilityTableTests(TestCase):
    def test_principal_views_but_does_not_manage_the_setup(self):
        held = capabilities_for(Role.PRINCIPAL)
        for capability in (
            Capability.VIEW_FEES,
            Capability.VIEW_ACADEMICS,
            Capability.VIEW_SCHOOL_PROFILE,
        ):
            self.assertIn(capability, held)
        for capability in (
            Capability.MANAGE_FEES,
            Capability.MANAGE_ACADEMICS,
            Capability.MANAGE_SCHOOL_PROFILE,
        ):
            self.assertNotIn(capability, held)

    def test_principal_keeps_the_money_work_and_the_roster(self):
        held = capabilities_for(Role.PRINCIPAL)
        for capability in (
            Capability.RECORD_PAYMENTS,
            Capability.VIEW_PAYMENTS,
            Capability.VIEW_STUDENTS,
        ):
            self.assertIn(capability, held)

    def test_school_owner_and_platform_manage_the_setup(self):
        for role in (Role.SCHOOL_OWNER, Role.PLATFORM_OWNER):
            held = capabilities_for(role)
            for capability in (
                Capability.MANAGE_FEES,
                Capability.MANAGE_ACADEMICS,
                Capability.MANAGE_SCHOOL_PROFILE,
                Capability.VIEW_SCHOOL_PROFILE,
            ):
                self.assertIn(capability, held, (role, capability))


class PrincipalScreenTests(SetupFixtures):
    def setUp(self):
        self.client.force_login(self.principal)

    def test_reads_fee_structures_terms_and_classes(self):
        for name in (
            "fees:structure_list", "fees:term_list",
            "academics:class_list", "academics:subject_list",
        ):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)

    def test_is_refused_every_screen_that_edits_them(self):
        for url in self.edit_urls():
            self.assertEqual(self.client.get(url).status_code, 403, url)

    def test_cannot_post_a_change_either(self):
        """Refused on dispatch, so a hand-built POST fares no better."""
        before = (FeeStructure.all_objects.count(), Class.all_objects.count())
        self.assertEqual(
            self.client.post(reverse("fees:structure_delete",
                                     args=[self.structure.pk])).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(reverse("academics:class_create"), {
                "branch": self.north.pk, "name": "Primary 4",
                "level": Level.PRIMARY, "year_in_level": 4, "is_active": "on",
            }).status_code,
            403,
        )
        self.assertEqual(
            (FeeStructure.all_objects.count(), Class.all_objects.count()), before
        )

    def test_lists_offer_no_edit_controls(self):
        fees = self.client.get(reverse("fees:structure_list")).content.decode()
        self.assertNotIn(reverse("fees:structure_create"), fees)
        classes = self.client.get(reverse("academics:class_list")).content.decode()
        self.assertNotIn(reverse("academics:class_create"), classes)

    def test_reads_school_settings_without_a_form(self):
        response = self.client.get(reverse("core:school_settings"))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["can_edit"])
        self.assertContains(response, "Alpha Schools")
        self.assertContains(response, "office@alpha.test")
        self.assertContains(response, "View only")
        self.assertNotContains(response, "Save changes")
        self.assertNotContains(response, 'name="name"')

    def test_cannot_post_school_settings(self):
        response = self.client.post(reverse("core:school_settings"), {
            "name": "Renamed By Principal",
            "contact_email": "x@alpha.test", "contact_phone": "",
        })
        self.assertEqual(response.status_code, 403)
        self.school.refresh_from_db()
        self.assertEqual(self.school.name, "Alpha Schools")

    def test_keeps_recording_and_reading_payments_and_students(self):
        for name in (
            "payments:record", "payments:index", "payments:pending",
            "students:student_list",
        ):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)

    def test_sees_the_receipt_attached_to_a_payment(self):
        media = tempfile.mkdtemp()
        try:
            with override_settings(MEDIA_ROOT=media):
                payment = Payment.all_objects.create(
                    school=self.school, branch=self.north, student=self.ada,
                    term=self.term, amount=Decimal("20000"),
                    status=PaymentStatus.PENDING,
                    receipt=SimpleUploadedFile(
                        "slip.png", b"\x89PNG\r\n\x1a\n", content_type="image/png"
                    ),
                )
                response = self.client.get(
                    reverse("payments:payment_detail", args=[payment.pk])
                )
                self.assertEqual(response.status_code, 200)
                # The gated page, not the storage URL -- and it opens.
                gated = reverse("payments:receipt_file", args=[payment.pk])
                self.assertContains(response, gated)
                self.assertNotContains(response, payment.receipt.url)
                opened = self.client.get(gated)
                self.assertEqual(opened.status_code, 200)
                self.assertEqual(opened["Content-Type"], "image/png")
                self.assertEqual(
                    b"".join(opened.streaming_content), b"\x89PNG\r\n\x1a\n"
                )
                opened.close()
        finally:
            shutil.rmtree(media, ignore_errors=True)


class OwnerScreenTests(SetupFixtures):
    def test_school_owner_reaches_every_edit_screen(self):
        self.client.force_login(self.owner)
        for url in self.edit_urls():
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_school_owner_edits_school_settings(self):
        self.client.force_login(self.owner)
        page = self.client.get(reverse("core:school_settings"))
        self.assertTrue(page.context["can_edit"])
        self.assertContains(page, "Save changes")

        response = self.client.post(reverse("core:school_settings"), {
            "name": "Alpha Schools Ltd",
            "contact_email": "office@alpha.test", "contact_phone": "",
        })
        self.assertRedirects(response, reverse("core:school_settings"))
        self.school.refresh_from_db()
        self.assertEqual(self.school.name, "Alpha Schools Ltd")


class BursarUnchangedTests(SetupFixtures):
    """Pinned exactly, so tightening the principal cannot quietly move them."""

    def test_the_bursars_capabilities_are_what_they_were(self):
        self.assertEqual(
            capabilities_for(Role.BURSAR),
            frozenset({
                Capability.VIEW_ACADEMICS,
                Capability.VIEW_FEES,
                Capability.VIEW_FAMILIES,
                Capability.VIEW_STUDENTS,
                Capability.VIEW_MESSAGES,
                Capability.SEND_MESSAGES,
                Capability.VIEW_PAYMENTS,
                Capability.RECORD_PAYMENTS,
                Capability.VIEW_ADMISSION_PAYMENTS,
                Capability.RECORD_ADMISSION_PAYMENTS,
            }),
        )

    def test_bursar_reads_the_setup_and_edits_none_of_it(self):
        self.client.force_login(self.bursar)
        for name in ("fees:structure_list", "academics:class_list"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)
        for url in self.edit_urls():
            self.assertEqual(self.client.get(url).status_code, 403, url)

    def test_bursar_still_has_no_school_settings(self):
        self.client.force_login(self.bursar)
        self.assertFalse(has_capability(self.bursar, Capability.VIEW_SCHOOL_PROFILE))
        self.assertEqual(
            self.client.get(reverse("core:school_settings")).status_code, 403
        )


class RecordPaymentCardTests(SetupFixtures):
    """The most frequent action, on the dashboard of every role that does it."""

    DASHBOARDS = (
        ("owner", "core:school_dashboard"),
        ("principal", "core:branch_dashboard"),
        ("bursar", "core:bursar_dashboard"),
    )

    def test_every_money_taking_dashboard_offers_it(self):
        for who, url_name in self.DASHBOARDS:
            with self.subTest(who=who):
                self.client.force_login(getattr(self, who))
                response = self.client.get(reverse(url_name))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'data-tour="record-payment"')
                self.assertContains(response, reverse("payments:record"))

    def test_it_points_at_receipts_waiting_to_be_checked(self):
        Payment.all_objects.create(
            school=self.school, branch=self.north, student=self.ada,
            term=self.term, amount=Decimal("20000"), status=PaymentStatus.PENDING,
        )
        self.client.force_login(self.principal)
        response = self.client.get(reverse("core:branch_dashboard"))
        self.assertEqual(response.context["receipts_waiting"], 1)
        self.assertContains(response, "Check 1 receipt")
        self.assertContains(response, reverse("payments:pending"))

    def test_nothing_waiting_offers_no_queue_link(self):
        self.client.force_login(self.bursar)
        response = self.client.get(reverse("core:bursar_dashboard"))
        self.assertEqual(response.context["receipts_waiting"], 0)
        self.assertNotContains(response, "Check 0 receipt")
