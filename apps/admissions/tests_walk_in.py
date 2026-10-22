"""Enrol a walk-in: one screen, one transaction, three kinds of record.

The client's process: the parent walks in, sees the requirements, pays, and the
child is enrolled in one sitting. These tests hold the screen to that -- and to
the rule that it is all or nothing, and only for the roles allowed to admit.
"""

from __future__ import annotations

from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.core.roles import Role
from apps.payments.models import Payment
from apps.students import fees
from apps.students.models import Student

from .models import (
    AdmissionPayment,
    AdmissionsConfig,
    Applicant,
    ApplicantSource,
    ApplicantStatus,
    Assessment,
    IntakeMode,
)
from .requirements import RequirementKind
from .tests_split import SplitTestCase

User = get_user_model()
D = Decimal

URL = reverse("admissions:walk_in")


class WalkInTestCase(SplitTestCase):
    def post_data(self, **overrides):
        data = {
            "school_class": self.north_kg2.pk,
            "admission_number": "FA/2026/003",
            "date_admitted": "2026-09-16",
            "first_name": "God's Choice",
            "last_name": "Edidiong",
            "sex": "female",
            "date_of_birth": "2021-03-02",
            "parent_name": "Mrs. Edidiong",
            "parent_phone": "0803 555 0101",
            "parent_phone_alt": "08055550102",
            "amount": "117000",
            "method": "cash",
            "reference": "",
            "assessment_max_score": "100",
        }
        data.update(overrides)
        return data

    def configure(self, **fields):
        config = AdmissionsConfig.for_school(self.alpha)
        for name, value in fields.items():
            setattr(config, name, value)
        config.school = self.alpha
        config.save()


class WalkInEnrolmentTests(WalkInTestCase):
    def setUp(self):
        self.client.force_login(self.north_principal)

    def test_one_save_creates_the_applicant_the_student_and_the_split_payment(self):
        response = self.client.post(URL, self.post_data())

        student = Student.all_objects.get(admission_number="FA/2026/003")
        applicant = Applicant.all_objects.get(student=student)
        self.assertEqual(applicant.status, ApplicantStatus.ENROLLED)
        self.assertEqual(applicant.source, ApplicantSource.WALK_IN)
        self.assertEqual(student.school_class, self.north_kg2)
        self.assertEqual(student.parent_phone, "08035550101")
        self.assertEqual(student.parent_phone_alt, "08055550102")

        term_payment = Payment.all_objects.get(student=student)
        intake = AdmissionPayment.all_objects.get(applicant=applicant)
        self.assertEqual(term_payment.amount, D("61000"))
        self.assertEqual(intake.amount, D("56000"))
        self.assertEqual(intake.term_payment, term_payment)
        self.assertEqual(fees.position_for(student).outstanding, D("0"))

        # Lands on the student's page, with the receipt one click away.
        self.assertRedirects(
            response,
            f"{student.get_absolute_url()}?receipt={term_payment.pk}",
        )
        page = self.client.get(response["Location"])
        self.assertContains(page, term_payment.receipt_number)
        self.assertContains(
            page, reverse("payments:receipt", args=[term_payment.pk])
        )

    def test_it_is_all_or_nothing(self):
        with mock.patch(
            "apps.admissions.split.record", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(URL, self.post_data())
        self.assertFalse(Applicant.all_objects.exists())
        self.assertFalse(Student.all_objects.exists())
        self.assertFalse(Payment.all_objects.exists())
        self.assertFalse(AdmissionPayment.all_objects.exists())

    def test_a_clashing_admission_number_saves_nothing(self):
        self.client.post(URL, self.post_data())
        response = self.client.post(
            URL, self.post_data(first_name="Second", parent_phone="08035550199")
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("admission_number", response.context["form"].errors)
        self.assertEqual(Student.all_objects.count(), 1)
        self.assertEqual(Applicant.all_objects.count(), 1)

    def test_what_was_handed_over_is_on_the_admission_record(self):
        photo = SimpleUploadedFile(
            "photo.png",
            b"\x89PNG\r\n\x1a\n" + b"\x00" * 64,
            content_type="image/png",
        )
        self.client.post(URL, self.post_data(
            requirements_received=[RequirementKind.BIRTH_CERTIFICATE],
            upload_passport_photo=photo,
        ))
        applicant = Applicant.all_objects.get()
        self.assertEqual(
            set(applicant.requirements_received),
            {RequirementKind.BIRTH_CERTIFICATE, RequirementKind.PASSPORT_PHOTO},
        )
        self.assertTrue(applicant.passport_photo)
        met = {line.kind for line, ok in applicant.requirement_checklist if ok}
        self.assertIn(RequirementKind.BIRTH_CERTIFICATE, met)
        self.assertIn(RequirementKind.PASSPORT_PHOTO, met)

    def test_an_assessment_score_is_recorded_when_given(self):
        self.client.post(URL, self.post_data(assessment_score="72"))
        assessment = Assessment.all_objects.get()
        self.assertEqual(assessment.score, D("72"))
        self.assertTrue(assessment.has_been_sat)

    def test_no_assessment_row_without_a_score(self):
        self.client.post(URL, self.post_data())
        self.assertFalse(Assessment.all_objects.exists())

    def test_less_than_the_admission_fee_is_refused_when_payment_is_required(self):
        response = self.client.post(URL, self.post_data(amount="2000"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("amount", response.context["form"].errors)
        self.assertFalse(Student.all_objects.exists())

    def test_without_the_payment_requirement_a_walk_in_can_owe_everything(self):
        self.configure(require_payment_before_enrolment=False)
        self.client.post(URL, self.post_data(amount=""))
        student = Student.all_objects.get()
        self.assertFalse(Payment.all_objects.exists())
        self.assertEqual(fees.position_for(student).outstanding, D("61000"))

    def test_the_bill_follows_the_class(self):
        response = self.client.get(
            reverse("admissions:walk_in_bill"), {"class": self.north_kg2.pk}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["bill"].total, D("117000"))
        self.assertContains(response, 'data-suggested-number="')

    def test_the_bill_will_not_show_another_schools_class(self):
        response = self.client.get(
            reverse("admissions:walk_in_bill"), {"class": self.beta_p1.pk}
        )
        self.assertEqual(response.status_code, 404)


class WalkInPermissionTests(WalkInTestCase):
    def test_owner_and_principal_may_enrol_walk_ins(self):
        for user in (self.alpha_owner, self.north_principal):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                self.assertEqual(self.client.get(URL).status_code, 200)

    def test_a_bursar_may_not_by_default(self):
        self.client.force_login(self.north_bursar)
        self.assertEqual(self.client.get(URL).status_code, 403)
        self.client.post(URL, self.post_data())
        self.assertFalse(Student.all_objects.exists())

    def test_a_bursar_may_once_the_school_allows_it(self):
        self.configure(bursar_can_enrol_walk_ins=True)
        self.client.force_login(self.north_bursar)
        self.assertEqual(self.client.get(URL).status_code, 200)
        self.client.post(URL, self.post_data())
        self.assertTrue(Student.all_objects.filter(branch=self.north).exists())

    def test_another_schools_class_cannot_be_chosen(self):
        self.client.force_login(self.beta_principal)
        response = self.client.post(URL, self.post_data())
        self.assertEqual(response.status_code, 200)
        self.assertIn("school_class", response.context["form"].errors)
        self.assertFalse(Student.all_objects.exists())

    def test_a_pipeline_only_school_has_no_walk_in_screen(self):
        self.configure(intake_mode=IntakeMode.PIPELINE)
        self.client.force_login(self.north_principal)
        self.assertEqual(self.client.get(URL).status_code, 404)
        dashboard = self.client.get(reverse("core:branch_dashboard"))
        self.assertNotContains(dashboard, URL)

    def test_a_walk_in_only_school_has_no_enquiry_screens(self):
        self.configure(intake_mode=IntakeMode.WALK_IN)
        self.client.force_login(self.north_principal)
        self.assertRedirects(
            self.client.get(reverse("admissions:enquiry_create")), URL
        )
        public = reverse("admissions_public:public_enquiry", args=[self.alpha.slug])
        self.client.logout()
        self.assertEqual(self.client.get(public).status_code, 404)

    def test_a_school_without_the_admissions_module_has_no_walk_in_screen(self):
        from apps.schools.models import SchoolModule

        SchoolModule.set_state(self.alpha, "admissions", False)
        self.client.force_login(self.north_principal)
        self.assertIn(self.client.get(URL).status_code, (403, 404))
        dashboard = self.client.get(reverse("core:branch_dashboard"))
        self.assertNotContains(dashboard, URL)


class WalkInButtonTests(WalkInTestCase):
    """Default intake mode is both, with "Enrol a walk-in" as the primary button."""

    def test_the_primary_button_on_the_dashboards_and_the_roster(self):
        for user, page in (
            (self.alpha_owner, "core:school_dashboard"),
            (self.north_principal, "core:branch_dashboard"),
            (self.north_principal, "students:student_list"),
        ):
            with self.subTest(user=user.username, page=page):
                self.client.force_login(user)
                response = self.client.get(reverse(page))
                self.assertContains(response, f'href="{URL}"')
                self.assertContains(response, "Enrol a walk-in")

    def test_not_offered_to_a_bursar_by_default(self):
        self.client.force_login(self.north_bursar)
        response = self.client.get(reverse("core:bursar_dashboard"))
        self.assertNotContains(response, f'href="{URL}"')

    def test_offered_to_a_bursar_the_school_allows(self):
        self.configure(bursar_can_enrol_walk_ins=True)
        self.client.force_login(self.north_bursar)
        response = self.client.get(reverse("core:bursar_dashboard"))
        self.assertContains(response, f'href="{URL}"')

    def test_the_pipeline_is_still_there_by_default(self):
        self.client.force_login(self.north_principal)
        response = self.client.get(reverse("admissions:pipeline"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("admissions:enquiry_create"))
        self.assertContains(response, f'href="{URL}"')
