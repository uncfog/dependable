"""Tests for the admissions pipeline.

The rules that must never regress, and why each one is here:

* the public form writes to the school in its **URL** and nowhere else,
* an enquiry becoming an application **adds** fields and re-asks for none,
* requirements follow the **level** -- KG skips the exam, Primary and Senior
  sit it -- and a school's own set overrides the default,
* enrolment produces a **Student** in the right class and branch, and keeps the
  link back,
* admission money and termly money never touch,
* an enquiry past its window is **closed, not deleted**,
* and one school cannot see another's applicants.

The last one is the reason several tests look paranoid. Tenant isolation is a
property of the managers, so it holds by construction -- which is exactly why it
needs a test that would notice if somebody replaced a scoped manager with
``all_objects`` to fix an unrelated bug.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.exceptions import ValidationError
from unittest import mock

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.academics.models import Class, Level
from apps.core.permissions import Capability, has_capability
from apps.core.roles import Role
from apps.core.tenancy import scope_to
from apps.fees.models import FeeComponent, FeeStructure, Term, TermSequence
from apps.schools.models import Branch, School
from apps.students.models import Student

from . import enrolment, notifications
from .access import can_decide, can_view_applicants
from .admission_pricing import (
    ADMISSION_FEES,
    AdmissionSpec,
    line,
    pending_report,
)
from .models import (
    AdmissionFeeItem,
    AdmissionFeeKind,
    AdmissionFeeSchedule,
    AdmissionPayment,
    AdmissionPaymentStatus,
    AdmissionsConfig,
    Applicant,
    ApplicantSource,
    ApplicantStatus,
    Assessment,
    AssessmentOutcome,
    Requirement,
    RequirementSet,
)
from .requirements import RequirementKind, profile_for

User = get_user_model()


def enquiry_post(**overrides) -> dict:
    """A complete, valid POST body for the public enquiry form."""
    data = {
        "first_name": "Chinaza",
        "last_name": "Okonkwo",
        "other_names": "Adaeze",
        "sex": "female",
        "date_of_birth": "2019-04-12",
        "parent_name": "Mrs. Ngozi Okonkwo",
        "parent_phone": "08034129876",
        "parent_email": "ngozi.okonkwo@example.com",
        "previous_school": "Little Steps Nursery",
        "heard_about": "instagram",
    }
    data.update(overrides)
    return data


class AdmissionsTestCase(TestCase):
    """One school with two campuses, and a second school to stay out of."""

    @staticmethod
    def enable_admissions(*schools):
        """Give these schools the admissions module.

        A helper rather than a line in every fixture: a suite that forgot it
        would fail with a 403 from ``ModuleAccessMiddleware``, which looks like a
        permissions bug and is not one.
        """
        from apps.schools.models import SchoolModule

        for school in schools:
            SchoolModule.set_state(school, "admissions", True)

    @classmethod
    def setUpTestData(cls):
        cls.alpha = School.all_objects.create(
            name="Alpha Schools", contact_email="office@alpha.example"
        )
        cls.north = Branch.all_objects.create(school=cls.alpha, name="North")
        cls.south = Branch.all_objects.create(school=cls.alpha, name="South")

        cls.beta = School.all_objects.create(name="Beta Academy")
        cls.beta_main = Branch.all_objects.create(school=cls.beta, name="Main")

        # Admissions is a paid add-on and off by default -- see
        # apps/core/modules.py. Both schools here are being tested for their
        # pipelines, so both have bought it. Turning it off is tested on its own,
        # in apps/core/tests_modules.py.
        cls.enable_admissions(cls.alpha, cls.beta)

        cls.alpha_owner = User.objects.create_user(
            "alpha.owner", password="pw", role=Role.SCHOOL_OWNER, school=cls.alpha
        )
        cls.north_principal = User.objects.create_user(
            "north.principal", password="pw", role=Role.PRINCIPAL,
            school=cls.alpha, branch=cls.north,
        )
        cls.north_bursar = User.objects.create_user(
            "north.bursar", password="pw", role=Role.BURSAR,
            school=cls.alpha, branch=cls.north,
        )
        cls.beta_principal = User.objects.create_user(
            "beta.principal", password="pw", role=Role.PRINCIPAL,
            school=cls.beta, branch=cls.beta_main,
        )

        # A class per band, so the level-conditional requirements have
        # something real to be conditional about.
        cls.north_kg2 = Class.all_objects.create(
            branch=cls.north, name="KG 2", level=Level.NURSERY, year_in_level=2
        )
        cls.north_p1 = Class.all_objects.create(
            branch=cls.north, name="Primary 1", level=Level.PRIMARY, year_in_level=1
        )
        cls.north_sss1 = Class.all_objects.create(
            branch=cls.north, name="SSS 1", level=Level.SENIOR_SECONDARY,
            year_in_level=1, stream="Science",
        )
        cls.south_p1 = Class.all_objects.create(
            branch=cls.south, name="Primary 1", level=Level.PRIMARY, year_in_level=1
        )
        cls.beta_p1 = Class.all_objects.create(
            branch=cls.beta_main, name="Primary 1", level=Level.PRIMARY,
            year_in_level=1,
        )

    def as_user(self, user):
        return scope_to(
            school_id=user.school_id, branch_id=user.branch_id, role=user.role
        )

    def assertWindowIs(self, applicant, *, days: int) -> None:
        """The expiry is ``days`` from now, within a tolerance.

        Asserting on ``(expires_at - now()).days`` is what this replaced, and
        it was flaky: the model stamps the expiry from its own ``now()`` and
        the test reads a later one, so the difference is a whisker under the
        window and truncates to ``days - 1`` -- except when the two calls land
        close enough together that it does not, and then it is ``days``. Which
        of those you get depends on how busy the machine is.
        """
        expected = timezone.now() + timedelta(days=days)
        drift = abs((applicant.expires_at - expected).total_seconds())
        self.assertLess(
            drift, 60,
            f"expiry is {applicant.expires_at}, expected about {expected}",
        )

    def make_applicant(self, **overrides) -> Applicant:
        data = {
            "branch": self.north,
            "school_class": self.north_p1,
            "level": Level.PRIMARY,
            "first_name": "Chinaza",
            "last_name": "Okonkwo",
            "sex": "female",
            "date_of_birth": date(2019, 4, 12),
            "parent_name": "Mrs. Ngozi Okonkwo",
            "parent_phone": "08034129876",
            "parent_email": "ngozi.okonkwo@example.com",
        }
        data.update(overrides)
        return Applicant.all_objects.create(**data)


# ===========================================================================
# The public enquiry page
# ===========================================================================


class PublicEnquiryTests(AdmissionsTestCase):
    """The one form on the platform an anonymous stranger can write through."""

    def url(self, school=None) -> str:
        return reverse(
            "admissions_public:public_enquiry", args=[(school or self.alpha).slug]
        )

    def test_the_page_is_reachable_signed_out(self):
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200)

    def test_the_page_is_branded_to_the_school_not_the_platform(self):
        """A parent arriving from Instagram sees their school, not our name."""
        response = self.client.get(self.url())
        body = response.content.decode()
        self.assertIn("Alpha Schools", body)
        self.assertNotIn("SCHOOLCORD", body)

    def test_an_unknown_slug_is_a_404(self):
        response = self.client.get("/no-such-school/enquiry/")
        self.assertEqual(response.status_code, 404)

    def test_submitting_writes_to_the_school_and_branch_chosen(self):
        response = self.client.post(
            self.url(),
            enquiry_post(branch=self.north.pk, school_class=self.north_p1.pk),
        )
        self.assertEqual(response.status_code, 302)

        applicant = Applicant.all_objects.get()
        self.assertEqual(applicant.school_id, self.alpha.pk)
        self.assertEqual(applicant.branch_id, self.north.pk)
        self.assertEqual(applicant.school_class_id, self.north_p1.pk)
        self.assertEqual(applicant.status, ApplicantStatus.ENQUIRY)
        self.assertEqual(applicant.source, ApplicantSource.ONLINE)

    def test_the_second_campus_is_honoured(self):
        """Branch is the parent's choice, and it is the one that gets written."""
        self.client.post(
            self.url(),
            enquiry_post(branch=self.south.pk, school_class=self.south_p1.pk),
        )
        self.assertEqual(Applicant.all_objects.get().branch_id, self.south.pk)

    def test_the_level_is_derived_from_the_class(self):
        self.client.post(
            self.url(),
            enquiry_post(branch=self.north.pk, school_class=self.north_kg2.pk),
        )
        self.assertEqual(Applicant.all_objects.get().level, Level.NURSERY)

    def test_a_class_from_another_school_is_refused(self):
        """The security boundary: the URL decides the school, not the post body."""
        response = self.client.post(
            self.url(),
            enquiry_post(branch=self.north.pk, school_class=self.beta_p1.pk),
        )
        self.assertEqual(response.status_code, 200)  # redisplayed with errors
        self.assertFalse(Applicant.all_objects.exists())

    def test_a_branch_from_another_school_is_refused(self):
        response = self.client.post(
            self.url(),
            enquiry_post(branch=self.beta_main.pk, school_class=self.north_p1.pk),
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Applicant.all_objects.exists())

    def test_a_class_at_the_wrong_campus_of_the_right_school_is_refused(self):
        response = self.client.post(
            self.url(),
            enquiry_post(branch=self.north.pk, school_class=self.south_p1.pk),
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Applicant.all_objects.exists())

    def test_each_school_gets_its_own_reference_series(self):
        self.client.post(
            self.url(),
            enquiry_post(branch=self.north.pk, school_class=self.north_p1.pk),
        )
        self.client.post(
            self.url(self.beta),
            enquiry_post(branch=self.beta_main.pk, school_class=self.beta_p1.pk),
        )
        alpha_ref = Applicant.all_objects.get(school=self.alpha).reference
        beta_ref = Applicant.all_objects.get(school=self.beta).reference
        self.assertTrue(alpha_ref.endswith("0001"))
        # Numbered per school, so the second school also starts at one.
        self.assertTrue(beta_ref.endswith("0001"))

    def test_references_increment_within_a_school(self):
        for _ in range(3):
            self.client.post(
                self.url(),
                enquiry_post(branch=self.north.pk, school_class=self.north_p1.pk),
            )
        references = sorted(
            Applicant.all_objects.values_list("reference", flat=True)
        )
        self.assertEqual(len(set(references)), 3)
        self.assertTrue(references[-1].endswith("0003"))

    def test_the_expiry_window_is_set_from_the_school_policy(self):
        AdmissionsConfig.all_objects.create(school=self.alpha, enquiry_validity_days=5)
        self.client.post(
            self.url(),
            enquiry_post(branch=self.north.pk, school_class=self.north_p1.pk),
        )
        applicant = Applicant.all_objects.get()
        self.assertIsNotNone(applicant.expires_at)
        self.assertWindowIs(applicant, days=5)

    def test_the_parent_is_acknowledged_by_email(self):
        self.client.post(
            self.url(),
            enquiry_post(branch=self.north.pk, school_class=self.north_p1.pk),
        )
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertIn("Alpha Schools", message.subject)
        # Signed by the school; the platform is not named in the body.
        self.assertNotIn("SCHOOLCORD", message.body)

    def test_a_suspended_school_does_not_collect_enquiries(self):
        self.alpha.status = "suspended"
        self.alpha.save()
        self.assertEqual(self.client.get(self.url()).status_code, 404)


class StaffEnquiryTests(AdmissionsTestCase):
    """The walk-in form: same model, branch taken from the member of staff."""

    def test_a_principal_does_not_choose_a_branch(self):
        self.client.force_login(self.north_principal)
        response = self.client.post(
            reverse("admissions:enquiry_create"),
            enquiry_post(school_class=self.north_p1.pk),
        )
        self.assertEqual(response.status_code, 302)

        applicant = Applicant.all_objects.get()
        self.assertEqual(applicant.branch_id, self.north.pk)
        self.assertEqual(applicant.source, ApplicantSource.WALK_IN)

    def test_a_walk_in_and_an_online_enquiry_share_the_model(self):
        self.client.force_login(self.north_principal)
        self.client.post(
            reverse("admissions:enquiry_create"),
            enquiry_post(school_class=self.north_p1.pk),
        )
        self.client.logout()
        self.client.post(
            reverse("admissions_public:public_enquiry", args=[self.alpha.slug]),
            enquiry_post(branch=self.north.pk, school_class=self.north_p1.pk,
                         parent_email="other@example.com"),
        )
        self.assertEqual(Applicant.all_objects.count(), 2)
        self.assertEqual(
            set(Applicant.all_objects.values_list("source", flat=True)),
            {ApplicantSource.ONLINE, ApplicantSource.WALK_IN},
        )

    def test_a_bursar_cannot_capture_an_enquiry(self):
        self.client.force_login(self.north_bursar)
        response = self.client.post(
            reverse("admissions:enquiry_create"),
            enquiry_post(school_class=self.north_p1.pk),
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Applicant.all_objects.exists())


# ===========================================================================
# Enquiry -> application, without re-asking
# ===========================================================================


class ProgressionTests(AdmissionsTestCase):
    def setUp(self):
        self.applicant = self.make_applicant()
        self.client.force_login(self.north_principal)

    def test_the_application_form_does_not_ask_for_the_enquiry_again(self):
        """The whole point of one record with stages.

        If any of these ever appears on the application form, the same fact has
        two places to be typed and therefore two places to disagree.
        """
        response = self.client.get(
            reverse("admissions:application", args=[self.applicant.pk])
        )
        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        for field in (
            "first_name", "last_name", "sex", "date_of_birth",
            "parent_name", "parent_phone", "parent_email", "previous_school",
        ):
            self.assertNotIn(field, form.fields)

    def test_the_enquiry_data_is_shown_as_carried_over(self):
        response = self.client.get(
            reverse("admissions:application", args=[self.applicant.pk])
        )
        body = response.content.decode()
        self.assertIn("Chinaza", body)
        self.assertIn("Mrs. Ngozi Okonkwo", body)

    def test_progressing_keeps_every_field_captured_at_enquiry(self):
        self.client.post(
            reverse("admissions:application", args=[self.applicant.pk]),
            {
                "school_class": self.north_p1.pk,
                "address": "14 Adeniyi Jones Avenue, Ikeja",
                "nationality": "Nigerian",
                "state_of_origin": "",
                "parent_occupation": "",
                "notes": "",
            },
        )
        self.applicant.refresh_from_db()
        self.assertEqual(self.applicant.status, ApplicantStatus.APPLICATION)
        self.assertIsNotNone(self.applicant.applied_at)
        # Untouched by the advance.
        self.assertEqual(self.applicant.first_name, "Chinaza")
        self.assertEqual(self.applicant.parent_phone, "08034129876")
        self.assertEqual(self.applicant.date_of_birth, date(2019, 4, 12))
        # And the application's own field landed.
        self.assertEqual(self.applicant.address, "14 Adeniyi Jones Avenue, Ikeja")

    def test_it_is_one_row_throughout(self):
        pk, reference = self.applicant.pk, self.applicant.reference
        self.client.post(
            reverse("admissions:application", args=[self.applicant.pk]),
            {"school_class": self.north_p1.pk, "address": "", "nationality": "",
             "state_of_origin": "", "parent_occupation": "", "notes": ""},
        )
        self.assertEqual(Applicant.all_objects.count(), 1)
        applicant = Applicant.all_objects.get()
        self.assertEqual(applicant.pk, pk)
        self.assertEqual(applicant.reference, reference)

    def test_moving_a_class_moves_the_level_with_it(self):
        """Requirements follow the level, so it cannot lag behind the class."""
        self.client.post(
            reverse("admissions:application", args=[self.applicant.pk]),
            {"school_class": self.north_kg2.pk, "address": "", "nationality": "",
             "state_of_origin": "", "parent_occupation": "", "notes": ""},
        )
        self.applicant.refresh_from_db()
        self.assertEqual(self.applicant.level, Level.NURSERY)


# ===========================================================================
# Level-conditional requirements
# ===========================================================================


class RequirementTests(AdmissionsTestCase):
    def test_nursery_skips_the_entrance_exam(self):
        profile = profile_for(Level.NURSERY, school=self.alpha, branch=self.north)
        self.assertFalse(profile.requires_assessment)

    def test_primary_and_senior_sit_the_entrance_exam(self):
        for level in (Level.PRIMARY, Level.JUNIOR_SECONDARY,
                      Level.SENIOR_SECONDARY):
            with self.subTest(level=level):
                profile = profile_for(level, school=self.alpha, branch=self.north)
                self.assertTrue(profile.requires_assessment)

    def test_a_passport_photo_is_required_at_every_level(self):
        for level in Level:
            with self.subTest(level=level):
                profile = profile_for(level.value, school=self.alpha)
                self.assertTrue(profile.requires(RequirementKind.PASSPORT_PHOTO))

    def test_nursery_does_not_ask_for_previous_results(self):
        """A four-year-old has no previous school to produce results from."""
        profile = profile_for(Level.NURSERY, school=self.alpha)
        self.assertFalse(profile.requires(RequirementKind.PREVIOUS_RESULTS))

    def test_secondary_asks_for_a_transfer_letter_and_primary_does_not(self):
        junior = profile_for(Level.JUNIOR_SECONDARY, school=self.alpha)
        primary = profile_for(Level.PRIMARY, school=self.alpha)
        self.assertTrue(junior.requires(RequirementKind.TRANSFER_LETTER))
        self.assertFalse(primary.requires(RequirementKind.TRANSFER_LETTER))

    def test_the_assessment_screen_404s_for_a_level_that_does_not_sit_one(self):
        """Skipping the exam is a URL that does not exist, not a hidden button."""
        applicant = self.make_applicant(
            school_class=self.north_kg2, level=Level.NURSERY
        )
        self.client.force_login(self.north_principal)
        response = self.client.get(
            reverse("admissions:assessment", args=[applicant.pk])
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(Assessment.all_objects.exists())

    def test_the_assessment_screen_opens_for_a_level_that_does(self):
        applicant = self.make_applicant(status=ApplicantStatus.APPLICATION)
        self.client.force_login(self.north_principal)
        response = self.client.get(
            reverse("admissions:assessment", args=[applicant.pk])
        )
        self.assertEqual(response.status_code, 200)

    def test_a_schools_own_set_overrides_the_default(self):
        """Configurable, not hardcoded: Primary stops sitting the exam here."""
        requirement_set = RequirementSet.all_objects.create(
            school=self.alpha, branch=None, level=Level.PRIMARY
        )
        Requirement.all_objects.create(
            requirement_set=requirement_set,
            kind=RequirementKind.PASSPORT_PHOTO,
            is_required=True,
        )
        Requirement.all_objects.create(
            requirement_set=requirement_set,
            kind=RequirementKind.ENTRANCE_EXAM,
            is_required=False,
        )
        profile = profile_for(Level.PRIMARY, school=self.alpha, branch=self.north)
        self.assertTrue(profile.is_configured)
        self.assertFalse(profile.requires_assessment)
        # The other school is untouched by Alpha's policy.
        self.assertTrue(
            profile_for(Level.PRIMARY, school=self.beta).requires_assessment
        )

    def test_a_branch_set_beats_the_school_wide_one(self):
        school_wide = RequirementSet.all_objects.create(
            school=self.alpha, branch=None, level=Level.PRIMARY
        )
        Requirement.all_objects.create(
            requirement_set=school_wide,
            kind=RequirementKind.ENTRANCE_EXAM,
            is_required=True,
        )
        campus = RequirementSet.all_objects.create(
            school=self.alpha, branch=self.north, level=Level.PRIMARY
        )
        Requirement.all_objects.create(
            requirement_set=campus,
            kind=RequirementKind.ENTRANCE_EXAM,
            is_required=False,
        )
        self.assertFalse(
            profile_for(Level.PRIMARY, school=self.alpha, branch=self.north)
            .requires_assessment
        )
        self.assertTrue(
            profile_for(Level.PRIMARY, school=self.alpha, branch=self.south)
            .requires_assessment
        )

    def test_an_empty_set_falls_back_rather_than_admitting_on_no_papers(self):
        RequirementSet.all_objects.create(
            school=self.alpha, branch=None, level=Level.PRIMARY
        )
        profile = profile_for(Level.PRIMARY, school=self.alpha)
        self.assertFalse(profile.is_configured)
        self.assertTrue(profile.requires(RequirementKind.PASSPORT_PHOTO))

    def test_missing_requirements_name_what_is_outstanding(self):
        applicant = self.make_applicant()
        missing = {line.kind for line in applicant.missing_requirements}
        self.assertIn(RequirementKind.PASSPORT_PHOTO, missing)
        self.assertIn(RequirementKind.ENTRANCE_EXAM, missing)
        self.assertFalse(applicant.is_complete)

    def test_a_sat_assessment_satisfies_the_exam_requirement(self):
        applicant = self.make_applicant()
        Assessment.all_objects.create(
            applicant=applicant,
            score=Decimal("72"),
            outcome=AssessmentOutcome.PASSED,
        )
        applicant.refresh_from_db()
        missing = {line.kind for line in applicant.missing_requirements}
        self.assertNotIn(RequirementKind.ENTRANCE_EXAM, missing)

    def test_the_application_form_asks_only_for_this_levels_documents(self):
        """A Nursery application does not grow a transfer-letter field."""
        nursery = self.make_applicant(
            school_class=self.north_kg2, level=Level.NURSERY
        )
        senior = self.make_applicant(
            school_class=self.north_sss1, level=Level.SENIOR_SECONDARY,
            first_name="Emeka", last_name="Obi",
        )
        self.client.force_login(self.north_principal)

        nursery_fields = self.client.get(
            reverse("admissions:application", args=[nursery.pk])
        ).context["form"].fields
        senior_fields = self.client.get(
            reverse("admissions:application", args=[senior.pk])
        ).context["form"].fields

        self.assertIn("immunisation_record", nursery_fields)
        # Not listed at all for Nursery -- not even as optional. A four-year-old
        # has no previous school to transfer from.
        self.assertNotIn("transfer_letter", nursery_fields)
        self.assertNotIn("previous_results", nursery_fields)
        self.assertIn("transfer_letter", senior_fields)
        self.assertIn("previous_results", senior_fields)
        # Asked of everybody.
        self.assertIn("passport_photo", nursery_fields)
        self.assertIn("passport_photo", senior_fields)


# ===========================================================================
# Assessment and decision
# ===========================================================================


class DecisionTests(AdmissionsTestCase):
    def setUp(self):
        self.applicant = self.make_applicant(status=ApplicantStatus.APPLICATION)

    def test_recording_an_outcome_advances_the_applicant(self):
        self.client.force_login(self.north_principal)
        self.client.post(
            reverse("admissions:assessment", args=[self.applicant.pk]),
            {
                "scheduled_for": "2026-01-10",
                "sat_on": "2026-01-10",
                "score": "72",
                "max_score": "100",
                "outcome": AssessmentOutcome.PASSED,
                "notes": "",
            },
        )
        self.applicant.refresh_from_db()
        self.assertEqual(self.applicant.status, ApplicantStatus.ASSESSED)

    def test_an_offer_is_recorded_against_the_person_who_made_it(self):
        self.client.force_login(self.north_principal)
        self.client.post(
            reverse("admissions:decision", args=[self.applicant.pk]),
            {"decision": ApplicantStatus.OFFERED, "decision_note": "Welcome.",
             "notify_parent": "on"},
        )
        self.applicant.refresh_from_db()
        self.assertEqual(self.applicant.status, ApplicantStatus.OFFERED)
        self.assertEqual(self.applicant.decided_by_id, self.north_principal.pk)
        self.assertIsNotNone(self.applicant.decided_at)

    def test_the_parent_is_emailed_the_decision(self):
        self.client.force_login(self.north_principal)
        self.client.post(
            reverse("admissions:decision", args=[self.applicant.pk]),
            {"decision": ApplicantStatus.OFFERED, "decision_note": "",
             "notify_parent": "on"},
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Alpha Schools", mail.outbox[0].subject)
        self.applicant.refresh_from_db()
        self.assertIsNotNone(self.applicant.decision_sent_at)

    def test_a_rejection_gets_its_own_letter(self):
        self.client.force_login(self.north_principal)
        self.client.post(
            reverse("admissions:decision", args=[self.applicant.pk]),
            {"decision": ApplicantStatus.REJECTED, "decision_note": "",
             "notify_parent": "on"},
        )
        self.applicant.refresh_from_db()
        self.assertEqual(self.applicant.status, ApplicantStatus.REJECTED)
        self.assertEqual(len(mail.outbox), 1)
        self.assertNotIn("delighted", mail.outbox[0].body)

    def test_the_parent_can_be_left_unemailed(self):
        self.client.force_login(self.north_principal)
        self.client.post(
            reverse("admissions:decision", args=[self.applicant.pk]),
            {"decision": ApplicantStatus.OFFERED, "decision_note": ""},
        )
        self.assertEqual(len(mail.outbox), 0)

    def test_an_applicant_cannot_be_decided_twice(self):
        """A stale second tab must not overwrite a decision."""
        self.applicant.status = ApplicantStatus.OFFERED
        self.applicant.save()
        self.client.force_login(self.north_principal)
        self.client.post(
            reverse("admissions:decision", args=[self.applicant.pk]),
            {"decision": ApplicantStatus.REJECTED, "decision_note": ""},
        )
        self.applicant.refresh_from_db()
        self.assertEqual(self.applicant.status, ApplicantStatus.OFFERED)


# ===========================================================================
# Enrolment -- the conversion
# ===========================================================================


class EnrolmentTests(AdmissionsTestCase):
    def setUp(self):
        self.applicant = self.make_applicant(
            status=ApplicantStatus.OFFERED, address="14 Adeniyi Jones Avenue"
        )
        AdmissionsConfig.all_objects.create(
            school=self.alpha, require_payment_before_enrolment=False
        )

    def test_enrolling_creates_a_student_in_the_right_class_and_branch(self):
        student = enrolment.enrol(
            self.applicant, admission_number="ALP/2026/001"
        )
        self.assertIsInstance(student, Student)
        self.assertEqual(student.school_class_id, self.north_p1.pk)
        self.assertEqual(student.branch_id, self.north.pk)
        self.assertEqual(student.school_id, self.alpha.pk)
        self.assertEqual(student.admission_number, "ALP/2026/001")

    def test_the_details_are_copied_rather_than_retyped(self):
        student = enrolment.enrol(self.applicant, admission_number="ALP/2026/002")
        self.assertEqual(student.first_name, "Chinaza")
        self.assertEqual(student.last_name, "Okonkwo")
        self.assertEqual(student.sex, "female")
        self.assertEqual(student.date_of_birth, date(2019, 4, 12))
        self.assertEqual(student.parent_name, "Mrs. Ngozi Okonkwo")
        self.assertEqual(student.parent_phone, "08034129876")
        self.assertEqual(student.address, "14 Adeniyi Jones Avenue")

    def test_the_applicant_is_kept_and_linked(self):
        """History survives the conversion -- that is why it is not a flag."""
        student = enrolment.enrol(self.applicant, admission_number="ALP/2026/003")
        self.applicant.refresh_from_db()
        self.assertEqual(self.applicant.status, ApplicantStatus.ENROLLED)
        self.assertEqual(self.applicant.student_id, student.pk)
        self.assertIsNotNone(self.applicant.enrolled_at)
        self.assertEqual(student.applicant, self.applicant)

    def test_the_applicant_is_a_separate_row_from_the_student(self):
        enrolment.enrol(self.applicant, admission_number="ALP/2026/004")
        self.assertEqual(Applicant.all_objects.count(), 1)
        self.assertEqual(Student.all_objects.count(), 1)

    def test_a_placement_change_at_enrolment_is_honoured(self):
        north_p2 = Class.all_objects.create(
            branch=self.north, name="Primary 2", level=Level.PRIMARY, year_in_level=2
        )
        student = enrolment.enrol(
            self.applicant, admission_number="ALP/2026/005", school_class=north_p2
        )
        self.assertEqual(student.school_class_id, north_p2.pk)

    def test_an_undecided_applicant_cannot_be_enrolled(self):
        self.applicant.status = ApplicantStatus.APPLICATION
        self.applicant.save()
        with self.assertRaises(enrolment.EnrolmentError):
            enrolment.enrol(self.applicant, admission_number="ALP/2026/006")
        self.assertFalse(Student.all_objects.exists())

    def test_a_rejected_applicant_cannot_be_enrolled(self):
        self.applicant.status = ApplicantStatus.REJECTED
        self.applicant.save()
        with self.assertRaises(enrolment.EnrolmentError):
            enrolment.enrol(self.applicant, admission_number="ALP/2026/007")

    def test_nobody_is_enrolled_twice(self):
        enrolment.enrol(self.applicant, admission_number="ALP/2026/008")
        with self.assertRaises(enrolment.EnrolmentError):
            enrolment.enrol(self.applicant, admission_number="ALP/2026/009")
        self.assertEqual(Student.all_objects.count(), 1)

    def test_a_class_at_another_campus_is_refused(self):
        with self.assertRaises(enrolment.EnrolmentError):
            enrolment.enrol(
                self.applicant,
                admission_number="ALP/2026/010",
                school_class=self.south_p1,
            )

    def test_a_duplicate_admission_number_is_refused_and_nothing_is_written(self):
        Student.all_objects.create(
            school_class=self.north_p1, admission_number="ALP/2026/011",
            first_name="Someone", last_name="Else", sex="male",
            parent_name="A Parent", parent_phone="08034129876",
        )
        with self.assertRaises(ValidationError):
            enrolment.enrol(self.applicant, admission_number="ALP/2026/011")
        self.applicant.refresh_from_db()
        self.assertEqual(self.applicant.status, ApplicantStatus.OFFERED)
        self.assertIsNone(self.applicant.student_id)

    def test_the_suggested_number_follows_the_branch_series(self):
        prefix = enrolment.school_prefix(self.alpha)
        self.assertEqual(prefix, "AS")  # "Alpha Schools"
        Student.all_objects.create(
            school_class=self.north_p1,
            admission_number=f"{prefix}/{timezone.localdate().year}/007",
            first_name="Someone", last_name="Else", sex="male",
            parent_name="A Parent", parent_phone="08034129876",
        )
        suggested = enrolment.suggest_admission_number(self.north)
        self.assertTrue(suggested.endswith("/008"), suggested)
        self.assertTrue(suggested.startswith(f"{prefix}/"), suggested)

    def test_the_payment_gate_holds_when_the_school_wants_it(self):
        config = AdmissionsConfig.all_objects.get(school=self.alpha)
        config.require_payment_before_enrolment = True
        config.save()

        schedule = AdmissionFeeSchedule.all_objects.create(
            school_class=self.north_p1, academic_year="2025/2026"
        )
        AdmissionFeeItem.all_objects.create(
            schedule=schedule, name="Admission Fee", amount=Decimal("20000"),
            kind=AdmissionFeeKind.ADMISSION,
        )

        with self.assertRaises(enrolment.EnrolmentError):
            enrolment.enrol(self.applicant, admission_number="ALP/2026/012")

        AdmissionPayment.all_objects.create(
            applicant=self.applicant, amount=Decimal("20000"),
            status=AdmissionPaymentStatus.CONFIRMED,
        )
        student = enrolment.enrol(self.applicant, admission_number="ALP/2026/012")
        self.assertIsNotNone(student.pk)

    def test_a_pending_payment_does_not_open_the_gate(self):
        config = AdmissionsConfig.all_objects.get(school=self.alpha)
        config.require_payment_before_enrolment = True
        config.save()

        schedule = AdmissionFeeSchedule.all_objects.create(
            school_class=self.north_p1, academic_year="2025/2026"
        )
        AdmissionFeeItem.all_objects.create(
            schedule=schedule, name="Admission Fee", amount=Decimal("20000"),
            kind=AdmissionFeeKind.ADMISSION,
        )
        AdmissionPayment.all_objects.create(
            applicant=self.applicant, amount=Decimal("20000"),
            status=AdmissionPaymentStatus.PENDING,
        )
        with self.assertRaises(enrolment.EnrolmentError):
            enrolment.enrol(self.applicant, admission_number="ALP/2026/013")

    def test_the_screen_enrols_end_to_end(self):
        self.client.force_login(self.north_principal)
        response = self.client.post(
            reverse("admissions:enrol", args=[self.applicant.pk]),
            {
                "admission_number": "ALP/2026/014",
                "school_class": self.north_p1.pk,
                "date_admitted": "2026-01-12",
            },
        )
        self.assertEqual(response.status_code, 302)
        student = Student.all_objects.get()
        self.assertEqual(student.admission_number, "ALP/2026/014")
        self.assertEqual(student.date_admitted, date(2026, 1, 12))


# ===========================================================================
# Admission fees stay out of termly fees
# ===========================================================================


class AdmissionFeeTests(AdmissionsTestCase):
    """The separation the client asked for, asserted from both sides."""

    def setUp(self):
        self.term = Term.all_objects.create(
            branch=self.north, name="First Term 2025/2026",
            academic_year="2025/2026", sequence=TermSequence.FIRST, is_current=True,
        )
        self.termly = FeeStructure.all_objects.create(
            school_class=self.north_p1, term=self.term
        )
        for name, amount in [("School Fees", 26000), ("Textbooks", 50000),
                             ("Development", 2000)]:
            FeeComponent.all_objects.create(
                fee_structure=self.termly, name=name, amount=Decimal(amount)
            )

        self.schedule = AdmissionFeeSchedule.all_objects.create(
            school_class=self.north_p1, academic_year="2025/2026"
        )
        for name, amount, kind in [
            ("Admission Fee", 20000, AdmissionFeeKind.ADMISSION),
            ("Tuition", 26000, AdmissionFeeKind.INTAKE),
            ("Uniform", 24000, AdmissionFeeKind.INTAKE),
            ("Compulsory Books", 50000, AdmissionFeeKind.BOOKS),
        ]:
            AdmissionFeeItem.all_objects.create(
                schedule=self.schedule, name=name, amount=Decimal(amount), kind=kind
            )

    def test_they_are_different_tables(self):
        self.assertNotEqual(FeeStructure, AdmissionFeeSchedule)
        self.assertNotEqual(FeeComponent, AdmissionFeeItem)

    def test_the_termly_total_is_unchanged_by_admission_fees(self):
        self.assertEqual(self.termly.total, Decimal("78000"))
        self.assertEqual(self.termly.components.count(), 3)

    def test_the_admission_total_is_unchanged_by_termly_fees(self):
        # Admission + intake. Books are quoted separately.
        self.assertEqual(self.schedule.compulsory_total, Decimal("70000"))
        self.assertEqual(self.schedule.books_total, Decimal("50000"))
        self.assertEqual(self.schedule.total, Decimal("120000"))

    def test_the_admission_fee_alone_is_what_enrolment_is_gated_on(self):
        self.assertEqual(self.schedule.admission_total, Decimal("20000"))

    def test_seeding_admission_fees_does_not_touch_termly_fees(self):
        before = {
            (c.name, c.amount) for c in FeeComponent.all_objects.all()
        }
        call_command(
            "seed_admission_fees",
            "--school", "Alpha Schools", "--branch", "North",
            stdout=StringIO(),
        )
        after = {(c.name, c.amount) for c in FeeComponent.all_objects.all()}
        self.assertEqual(before, after)
        self.assertEqual(FeeStructure.all_objects.count(), 1)

    def test_admission_payments_are_not_termly_payments(self):
        """A term's collection figure must never quietly include intake money."""
        from apps.payments.models import Payment

        applicant = self.make_applicant()
        AdmissionPayment.all_objects.create(
            applicant=applicant, amount=Decimal("20000"),
            status=AdmissionPaymentStatus.CONFIRMED,
        )
        self.assertEqual(Payment.all_objects.count(), 0)
        self.assertEqual(applicant.admission_paid, Decimal("20000"))

    def test_only_confirmed_admission_money_counts(self):
        applicant = self.make_applicant()
        AdmissionPayment.all_objects.create(
            applicant=applicant, amount=Decimal("5000"),
            status=AdmissionPaymentStatus.PENDING,
        )
        AdmissionPayment.all_objects.create(
            applicant=applicant, amount=Decimal("15000"),
            status=AdmissionPaymentStatus.CONFIRMED,
        )
        self.assertEqual(applicant.admission_paid, Decimal("15000"))
        self.assertEqual(applicant.admission_outstanding, Decimal("55000"))

    def test_an_overpayment_is_not_a_negative_debt(self):
        applicant = self.make_applicant()
        AdmissionPayment.all_objects.create(
            applicant=applicant, amount=Decimal("100000"),
            status=AdmissionPaymentStatus.CONFIRMED,
        )
        self.assertEqual(applicant.admission_outstanding, Decimal("0"))


class AdmissionPricingSheetTests(TestCase):
    """The sheet itself: shape now, figures when the client supplies them."""

    def test_every_band_is_covered(self):
        named = {name for spec in ADMISSION_FEES for name in spec.class_names}
        for expected in ("Pre-KG", "KG 3", "Primary 1", "Primary 6", "JSS 1",
                         "JSS 3", "SSS 1", "SSS 3"):
            self.assertIn(expected, named)

    def test_every_sheet_carries_the_lines_the_client_listed(self):
        for spec in ADMISSION_FEES:
            names = {item.name for item in spec.lines}
            with self.subTest(classes=spec.class_names):
                for expected in ("Admission Fee", "Exam / Dossier", "Track suit"):
                    self.assertIn(expected, names)
                # Junior and senior quote two sets; the younger bands one.
                self.assertTrue({"Uniform", "Uniform (2)"} & names, names)
                # Senior charges Practical where the rest charge Sportswear --
                # a different item, not a renamed one.
                self.assertTrue({"Sportswear", "Practical"} & names, names)

    def test_no_sheet_carries_an_item_that_recurs_every_term(self):
        """Tuition, books and development are billed by the term structure."""
        for spec in ADMISSION_FEES:
            names = {item.name for item in spec.lines}
            with self.subTest(classes=spec.class_names):
                self.assertFalse(
                    {"Tuition", "Development Levy", "Compulsory Books"} & names
                )
                self.assertFalse([i for i in spec.lines if i.kind == "books"])

    def test_every_sheet_totals_what_the_school_wrote(self):
        """Less the recurring lines -- except the nursery band, whose 1,000 gap
        has its own test below."""
        for spec in ADMISSION_FEES:
            if spec.total_note:
                continue
            with self.subTest(classes=spec.class_names):
                self.assertEqual(spec.computed_total, spec.client_total)

    def test_the_senior_arms_share_one_sheet(self):
        senior = [spec for spec in ADMISSION_FEES if "SSS 1" in spec.class_names]
        self.assertEqual(len(senior), 1)
        self.assertEqual(senior[0].streams, ())

    def test_the_nursery_total_discrepancy_is_recorded_not_balanced(self):
        """The 1,000 gap the client flagged survives the move of tuition.

        No balancing line is invented; the school's written total (less what
        moved to the term structure) is kept as a cross-check so the seeder
        warns until the client confirms the figure.
        """
        nursery = next(s for s in ADMISSION_FEES if "Pre-KG" in s.class_names)
        self.assertEqual(nursery.client_total, Decimal("55000"))
        self.assertEqual(nursery.computed_total, Decimal("56000"))
        self.assertIn("56,000", nursery.total_note)
        names = [item.name for item in nursery.lines]
        self.assertEqual(len(names), len(set(names)))

    def test_every_figure_has_now_been_supplied(self):
        """The sheets are priced; nothing is waiting on the client."""
        self.assertEqual(pending_report(), [])
        for spec in ADMISSION_FEES:
            with self.subTest(classes=spec.class_names):
                self.assertTrue(spec.is_complete)

    def test_an_unpriced_line_would_still_be_reported_rather_than_guessed(self):
        """The refusal mechanism, on a sheet built for the purpose.

        It outlives the gaps it was written for: the next sheet a school sends
        may arrive incomplete too, and a 0 in front of a parent is worse than a
        named gap.
        """
        incomplete = AdmissionSpec(
            ("Primary 1",),
            (line("Admission Fee", None, "admission"), line("Tuition", 26_000)),
        )
        self.assertFalse(incomplete.is_complete)
        self.assertEqual(
            [item.name for item in incomplete.pending_lines], ["Admission Fee"]
        )


class AdmissionFeeSeedTests(AdmissionsTestCase):
    """The figures have landed, so the seeder now writes rather than waits."""

    def seed(self) -> str:
        out = StringIO()
        call_command(
            "seed_admission_fees",
            "--school", "Alpha Schools", "--branch", "North",
            stdout=out,
        )
        return out.getvalue()

    def test_it_writes_a_schedule_per_class_from_the_sheet(self):
        self.seed()
        # One per class at the branch: KG 2, Primary 1, SSS 1 Science.
        schedules = AdmissionFeeSchedule.all_objects.filter(branch=self.north)
        self.assertEqual(schedules.count(), 3)
        kg2 = schedules.get(school_class=self.north_kg2)
        self.assertEqual(kg2.admission_total, Decimal("3000"))
        self.assertEqual(kg2.compulsory_total, Decimal("56000"))
        self.assertEqual(kg2.books_total, Decimal("0"))

    def test_the_senior_sheet_charges_practical_not_sportswear(self):
        self.seed()
        sss1 = AdmissionFeeSchedule.all_objects.get(school_class=self.north_sss1)
        self.assertEqual(sss1.compulsory_total, Decimal("64000"))
        self.assertEqual(sss1.books_total, Decimal("0"))
        names = [item.name for item in sss1.items.all()]
        self.assertIn("Practical", names)
        self.assertNotIn("Sportswear", names)

    def test_it_warns_about_the_nursery_gap_without_balancing_it(self):
        output = self.seed()
        self.assertIn("56,000", output)
        self.assertIn("55,000", output)
        kg2 = AdmissionFeeSchedule.all_objects.get(school_class=self.north_kg2)
        # Seeded as supplied: five one-off lines, no invented sixth.
        self.assertEqual(kg2.items.count(), 5)

    def test_it_never_seeds_a_zero(self):
        self.seed()
        self.assertFalse(
            AdmissionFeeItem.all_objects.filter(amount=Decimal("0")).exists()
        )

    def test_seeding_twice_rewrites_rather_than_duplicates(self):
        self.seed()
        first = AdmissionFeeItem.all_objects.count()
        self.seed()
        self.assertEqual(AdmissionFeeItem.all_objects.count(), first)

    def test_a_sheet_still_missing_a_figure_is_named_not_guessed(self):
        """The refusal path, with a deliberately unpriced sheet standing in."""
        unpriced = AdmissionSpec(
            ("Primary 1",),
            (line("Admission Fee", None, "admission"), line("Tuition", 26_000)),
        )
        with mock.patch(
            "apps.admissions.management.commands.seed_admission_fees.specs_for",
            lambda klass: [unpriced] if klass.name == "Primary 1" else [],
        ):
            output = self.seed()
        self.assertIn("skipped", output)
        self.assertIn("Admission Fee", output)
        self.assertFalse(AdmissionFeeSchedule.all_objects.exists())


# ===========================================================================
# The validity window
# ===========================================================================


class ExpiryTests(AdmissionsTestCase):
    def test_an_enquiry_gets_a_window_by_default(self):
        applicant = self.make_applicant()
        self.assertIsNotNone(applicant.expires_at)
        self.assertWindowIs(
            applicant, days=AdmissionsConfig.DEFAULT_VALIDITY_DAYS
        )

    def test_a_lapsed_enquiry_reads_as_lapsed_before_the_sweep_runs(self):
        applicant = self.make_applicant(
            expires_at=timezone.now() - timedelta(days=1)
        )
        self.assertTrue(applicant.has_lapsed)
        self.assertEqual(applicant.status, ApplicantStatus.ENQUIRY)

    def test_the_command_expires_it_and_keeps_the_row(self):
        applicant = self.make_applicant(
            expires_at=timezone.now() - timedelta(days=1)
        )
        call_command("expire_enquiries", stdout=StringIO())
        applicant.refresh_from_db()
        self.assertEqual(applicant.status, ApplicantStatus.EXPIRED)
        self.assertIsNotNone(applicant.expired_at)
        # Kept, not deleted -- the intake has to stay answerable.
        self.assertEqual(Applicant.all_objects.count(), 1)

    def test_a_dry_run_writes_nothing(self):
        applicant = self.make_applicant(
            expires_at=timezone.now() - timedelta(days=1)
        )
        call_command("expire_enquiries", "--dry-run", stdout=StringIO())
        applicant.refresh_from_db()
        self.assertEqual(applicant.status, ApplicantStatus.ENQUIRY)

    def test_an_application_is_never_expired(self):
        """Once the family has applied, the window has done its job."""
        applicant = self.make_applicant(
            status=ApplicantStatus.APPLICATION,
            expires_at=timezone.now() - timedelta(days=30),
        )
        call_command("expire_enquiries", stdout=StringIO())
        applicant.refresh_from_db()
        self.assertEqual(applicant.status, ApplicantStatus.APPLICATION)
        self.assertFalse(applicant.has_lapsed)

    def test_a_live_enquiry_is_left_alone(self):
        applicant = self.make_applicant()
        call_command("expire_enquiries", stdout=StringIO())
        applicant.refresh_from_db()
        self.assertEqual(applicant.status, ApplicantStatus.ENQUIRY)

    def test_the_reminder_goes_out_inside_the_window(self):
        AdmissionsConfig.all_objects.create(
            school=self.alpha, enquiry_validity_days=21, reminder_after_days=7
        )
        applicant = self.make_applicant()
        # Backdate the enquiry past the nudge date.
        Applicant.all_objects.filter(pk=applicant.pk).update(
            created_at=timezone.now() - timedelta(days=8)
        )

        call_command("send_enquiry_reminders", stdout=StringIO())

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(applicant.parent_email, mail.outbox[0].to)
        applicant.refresh_from_db()
        self.assertIsNotNone(applicant.reminder_sent_at)

    def test_the_reminder_is_sent_only_once(self):
        AdmissionsConfig.all_objects.create(
            school=self.alpha, enquiry_validity_days=21, reminder_after_days=7
        )
        applicant = self.make_applicant()
        Applicant.all_objects.filter(pk=applicant.pk).update(
            created_at=timezone.now() - timedelta(days=8)
        )
        call_command("send_enquiry_reminders", stdout=StringIO())
        call_command("send_enquiry_reminders", stdout=StringIO())
        self.assertEqual(len(mail.outbox), 1)

    def test_a_fresh_enquiry_is_not_nudged(self):
        AdmissionsConfig.all_objects.create(
            school=self.alpha, enquiry_validity_days=21, reminder_after_days=7
        )
        self.make_applicant()
        call_command("send_enquiry_reminders", stdout=StringIO())
        self.assertEqual(len(mail.outbox), 0)

    def test_an_expired_enquiry_is_not_nudged(self):
        applicant = self.make_applicant(
            expires_at=timezone.now() - timedelta(days=1)
        )
        Applicant.all_objects.filter(pk=applicant.pk).update(
            created_at=timezone.now() - timedelta(days=30)
        )
        call_command("send_enquiry_reminders", stdout=StringIO())
        self.assertEqual(len(mail.outbox), 0)

    def test_an_enquiry_with_no_email_is_skipped_not_failed(self):
        applicant = self.make_applicant(parent_email="")
        Applicant.all_objects.filter(pk=applicant.pk).update(
            created_at=timezone.now() - timedelta(days=30)
        )
        call_command("send_enquiry_reminders", stdout=StringIO())
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(notifications.send_enquiry_reminder(applicant))


# ===========================================================================
# Tenant isolation and permissions
# ===========================================================================


class IsolationTests(AdmissionsTestCase):
    def setUp(self):
        self.alpha_applicant = self.make_applicant()
        self.beta_applicant = self.make_applicant(
            branch=self.beta_main, school_class=self.beta_p1,
            first_name="Tobiloba", last_name="Adeyemi", sex="male",
        )
        self.south_applicant = self.make_applicant(
            branch=self.south, school_class=self.south_p1,
            first_name="Emeka", last_name="Obi", sex="male",
        )

    def test_a_school_sees_only_its_own_applicants(self):
        with self.as_user(self.alpha_owner):
            pks = set(Applicant.objects.values_list("pk", flat=True))
        self.assertEqual(pks, {self.alpha_applicant.pk, self.south_applicant.pk})
        self.assertNotIn(self.beta_applicant.pk, pks)

    def test_two_schools_may_both_use_the_same_reference(self):
        """References are numbered per school, which is why isolation is by pk."""
        self.assertEqual(
            self.alpha_applicant.reference, self.beta_applicant.reference
        )
        self.assertNotEqual(
            self.alpha_applicant.school_id, self.beta_applicant.school_id
        )

    def test_a_principal_sees_only_their_own_campus(self):
        with self.as_user(self.north_principal):
            pks = set(Applicant.objects.values_list("pk", flat=True))
        self.assertEqual(pks, {self.alpha_applicant.pk})

    def test_another_schools_applicant_404s_rather_than_leaking(self):
        self.client.force_login(self.north_principal)
        response = self.client.get(
            reverse("admissions:applicant_detail", args=[self.beta_applicant.pk])
        )
        self.assertEqual(response.status_code, 404)

    def test_another_campuses_applicant_404s_for_a_principal(self):
        self.client.force_login(self.north_principal)
        response = self.client.get(
            reverse("admissions:applicant_detail", args=[self.south_applicant.pk])
        )
        self.assertEqual(response.status_code, 404)

    def test_the_pipeline_shows_no_other_tenants_names(self):
        self.client.force_login(self.north_principal)
        body = self.client.get(reverse("admissions:pipeline")).content.decode()
        self.assertIn("Chinaza", body)
        self.assertNotIn("Tobiloba", body)

    def test_another_school_cannot_be_decided_on(self):
        self.client.force_login(self.north_principal)
        response = self.client.post(
            reverse("admissions:decision", args=[self.beta_applicant.pk]),
            {"decision": ApplicantStatus.OFFERED, "decision_note": ""},
        )
        self.assertEqual(response.status_code, 404)
        self.beta_applicant.refresh_from_db()
        self.assertEqual(self.beta_applicant.status, ApplicantStatus.ENQUIRY)

    def test_another_school_cannot_be_paid_against(self):
        self.client.force_login(self.north_bursar)
        response = self.client.post(
            reverse("admissions:payment_create", args=[self.beta_applicant.pk]),
            {"amount": "1000", "method": "cash", "status": "confirmed",
             "received_on": "2026-01-12"},
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(AdmissionPayment.all_objects.exists())


class PermissionTests(AdmissionsTestCase):
    def setUp(self):
        self.applicant = self.make_applicant(status=ApplicantStatus.APPLICATION)

    def test_leadership_may_decide(self):
        for user in (self.alpha_owner, self.north_principal):
            with self.subTest(user=user.username):
                self.assertTrue(can_decide(user))
                self.assertTrue(
                    has_capability(user, Capability.MANAGE_ADMISSIONS)
                )

    def test_a_bursar_may_never_decide(self):
        self.assertFalse(can_decide(self.north_bursar))
        self.client.force_login(self.north_bursar)
        response = self.client.post(
            reverse("admissions:decision", args=[self.applicant.pk]),
            {"decision": ApplicantStatus.OFFERED, "decision_note": ""},
        )
        self.assertEqual(response.status_code, 403)
        self.applicant.refresh_from_db()
        self.assertEqual(self.applicant.status, ApplicantStatus.APPLICATION)

    def test_a_bursar_does_not_see_the_pipeline_by_default(self):
        self.assertFalse(can_view_applicants(self.north_bursar))
        self.client.force_login(self.north_bursar)
        self.assertEqual(
            self.client.get(reverse("admissions:pipeline")).status_code, 403
        )

    def test_a_bursar_sees_admission_fees_by_default(self):
        """The default the client asked for: money yes, pipeline no."""
        self.assertTrue(
            has_capability(self.north_bursar, Capability.VIEW_ADMISSION_PAYMENTS)
        )
        self.client.force_login(self.north_bursar)
        self.assertEqual(
            self.client.get(reverse("admissions:fee_schedules")).status_code, 200
        )

    def test_a_bursar_may_record_an_admission_payment(self):
        self.client.force_login(self.north_bursar)
        response = self.client.post(
            reverse("admissions:payment_create", args=[self.applicant.pk]),
            {"amount": "20000", "method": "transfer", "status": "confirmed",
             "received_on": "2026-01-12", "reference": "", "note": ""},
        )
        self.assertEqual(response.status_code, 302)
        payment = AdmissionPayment.all_objects.get()
        self.assertEqual(payment.amount, Decimal("20000"))
        self.assertEqual(payment.recorded_by_id, self.north_bursar.pk)

    def test_the_school_can_let_its_bursar_see_the_pipeline(self):
        AdmissionsConfig.all_objects.create(
            school=self.alpha, bursar_can_view_applicants=True
        )
        self.assertTrue(can_view_applicants(self.north_bursar))
        self.client.force_login(self.north_bursar)
        self.assertEqual(
            self.client.get(reverse("admissions:pipeline")).status_code, 200
        )

    def test_the_toggle_never_widens_deciding(self):
        """Configurable viewing; fixed deciding. The line the client drew."""
        AdmissionsConfig.all_objects.create(
            school=self.alpha, bursar_can_view_applicants=True
        )
        self.assertFalse(can_decide(self.north_bursar))
        self.client.force_login(self.north_bursar)
        self.assertEqual(
            self.client.get(
                reverse("admissions:decision", args=[self.applicant.pk])
            ).status_code,
            403,
        )

    def test_the_toggle_is_per_school(self):
        AdmissionsConfig.all_objects.create(
            school=self.alpha, bursar_can_view_applicants=True
        )
        beta_bursar = User.objects.create_user(
            "beta.bursar", password="pw", role=Role.BURSAR,
            school=self.beta, branch=self.beta_main,
        )
        self.assertTrue(can_view_applicants(self.north_bursar))
        self.assertFalse(can_view_applicants(beta_bursar))

    def test_a_bursar_cannot_advance_an_application(self):
        AdmissionsConfig.all_objects.create(
            school=self.alpha, bursar_can_view_applicants=True
        )
        self.client.force_login(self.north_bursar)
        self.assertEqual(
            self.client.get(
                reverse("admissions:application", args=[self.applicant.pk])
            ).status_code,
            403,
        )

    def test_signed_out_visitors_are_sent_to_the_login_page(self):
        response = self.client.get(reverse("admissions:pipeline"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])

    def test_the_sidebar_offers_a_bursar_no_link_it_will_refuse(self):
        """Navigation and the views have to agree, or a link 403s."""
        from apps.core.navigation import nav_for
        from apps.admissions.access import admissions_capabilities

        sections = nav_for(
            Role.BURSAR,
            "/admissions/",
            admissions_capabilities(self.north_bursar),
            # This school has the add-on; what is being tested is which of its
            # entries a bursar gets. A school without it is tested in
            # apps/core/tests_modules.py.
            modules={"admissions"},
        )
        labels = {item.label for section in sections for item in section.items}
        self.assertNotIn("Applications", labels)
        self.assertIn("Admission fees", labels)

    def test_a_school_without_the_add_on_offers_a_bursar_nothing_at_all(self):
        from apps.core.navigation import nav_for
        from apps.admissions.access import admissions_capabilities

        sections = nav_for(
            Role.BURSAR,
            "/",
            admissions_capabilities(self.north_bursar),
            modules=frozenset(),
        )
        labels = {item.label for section in sections for item in section.items}
        for entry in ("Applications", "Admission fees", "Requirements",
                      "New enquiry", "Admissions settings"):
            with self.subTest(entry=entry):
                self.assertNotIn(entry, labels)


class ScreenTests(AdmissionsTestCase):
    """Every screen renders, and the settings screen actually saves.

    Cheap, but not decorative: the templates carry the requirement checklist and
    the fee tables, and a typo in one of them is a 500 nobody would meet until a
    school opened the page.
    """

    def setUp(self):
        self.applicant = self.make_applicant()
        self.client.force_login(self.north_principal)

    def test_every_staff_screen_renders(self):
        urls = [
            reverse("admissions:pipeline"),
            reverse("admissions:enquiry_create"),
            reverse("admissions:fee_schedules"),
            reverse("admissions:settings"),
            reverse("admissions:applicant_detail", args=[self.applicant.pk]),
            reverse("admissions:application", args=[self.applicant.pk]),
            reverse("admissions:assessment", args=[self.applicant.pk]),
            reverse("admissions:decision", args=[self.applicant.pk]),
            reverse("admissions:payment_create", args=[self.applicant.pk]),
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_the_enrol_screen_renders_for_an_offered_applicant(self):
        self.applicant.status = ApplicantStatus.OFFERED
        self.applicant.save()
        response = self.client.get(
            reverse("admissions:enrol", args=[self.applicant.pk])
        )
        self.assertEqual(response.status_code, 200)

    def test_the_settings_screen_creates_the_policy_on_first_save(self):
        """A school with no row edits an unsaved default rather than a 404."""
        self.assertFalse(AdmissionsConfig.all_objects.exists())
        response = self.client.post(
            reverse("admissions:settings"),
            {
                "intake_mode": "walk_in",
                "enquiry_validity_days": 14,
                "reminder_after_days": 5,
                "bursar_can_view_applicants": "on",
                "bursar_can_enrol_walk_ins": "on",
                "require_payment_before_enrolment": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        config = AdmissionsConfig.all_objects.get()
        self.assertEqual(config.school_id, self.alpha.pk)
        self.assertEqual(config.enquiry_validity_days, 14)
        self.assertTrue(config.bursar_can_view_applicants)
        self.assertEqual(config.intake_mode, "walk_in")
        self.assertTrue(config.bursar_can_enrol_walk_ins)

    def test_a_reminder_after_the_window_is_refused(self):
        response = self.client.post(
            reverse("admissions:settings"),
            {"intake_mode": "both", "enquiry_validity_days": 5,
             "reminder_after_days": 10},
        )
        self.assertEqual(response.status_code, 200)
        # Refused for the reminder, not for anything else missing.
        self.assertEqual(
            set(response.context["form"].errors), {"reminder_after_days"}
        )
        self.assertFalse(AdmissionsConfig.all_objects.exists())

    def test_the_settings_screen_shows_the_policy_in_force_per_level(self):
        body = self.client.get(reverse("admissions:settings")).content.decode()
        self.assertIn("Nursery", body)
        self.assertIn("No entrance exam", body)
        self.assertIn("Sits the entrance exam", body)

    def test_withdrawing_closes_the_applicant_and_keeps_the_row(self):
        response = self.client.post(
            reverse("admissions:withdraw", args=[self.applicant.pk]),
            {"closing_note": "Moved to Abuja."},
        )
        self.assertEqual(response.status_code, 302)
        self.applicant.refresh_from_db()
        self.assertEqual(self.applicant.status, ApplicantStatus.WITHDRAWN)
        self.assertEqual(self.applicant.closing_note, "Moved to Abuja.")
        self.assertIsNotNone(self.applicant.withdrawn_at)
        self.assertEqual(Applicant.all_objects.count(), 1)

    def test_an_empty_board_offers_the_public_link_to_hand_out(self):
        Applicant.all_objects.all().delete()
        body = self.client.get(reverse("admissions:pipeline")).content.decode()
        self.assertIn(f"/{self.alpha.slug}/enquiry/", body)
