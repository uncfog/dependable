"""The record form's "Where they stand" panel, and the gated receipt link.

What must hold:

* picking a student on the record form shows *that* student's balance --
  expected minus confirmed, with pending money left out -- both from the live
  lookup the picker calls and when the form re-renders after a submit;
* the lookup is tenant-scoped: another branch's admission number names nobody;
* every screen that shows a payment with a slip attached links to the gated
  receipt page, and that page actually opens the file -- for the owner, the
  principal and the bursar, and for nobody outside the branch.
"""

from __future__ import annotations

import shutil
import tempfile
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from .forms import picker_label
from .models import Payment, PaymentLabel, PaymentMethod, PaymentStatus
from .tests import FIFTY_K, PaymentsTestCase

PNG = b"\x89PNG\r\n\x1a\n fake"


def slip(name="slip.png"):
    return SimpleUploadedFile(name, PNG, content_type="image/png")


class BalanceLookupTests(PaymentsTestCase):
    @property
    def url(self):
        return reverse("payments:student_position")

    def lookup(self, value, user=None):
        self.client.force_login(user or self.bursar)
        return self.client.get(self.url, {"student": value})

    def test_the_picked_student_shows_expected_less_confirmed(self):
        self.pay(self.ada, "20000")
        self.pay(self.ada, "5000", status=PaymentStatus.PENDING)

        response = self.lookup(picker_label(self.ada))

        self.assertEqual(response.status_code, 200)
        position = response.context["position"]
        self.assertEqual(position.expected, FIFTY_K)
        self.assertEqual(position.paid, Decimal("20000"))
        # Pending money does not count until someone confirms it.
        self.assertEqual(position.outstanding, Decimal("30000"))
        self.assertContains(response, "₦30,000")
        self.assertContains(response, "Owes now")
        self.assertNotContains(response, "Pick a student")

    def test_a_bare_admission_number_finds_the_student_too(self):
        response = self.lookup(self.ada.admission_number.lower())
        self.assertEqual(response.context["position"].outstanding, FIFTY_K)

    def test_nothing_picked_keeps_the_prompt(self):
        response = self.lookup("")
        self.assertIsNone(response.context["position"])
        self.assertContains(response, "Pick a student and their balance appears here.")

    def test_an_unpriced_class_says_so(self):
        response = self.lookup(self.dami.admission_number)
        self.assertContains(response, "has no fees set for this term")

    def test_another_branchs_student_names_nobody(self):
        response = self.lookup(self.south_kid.admission_number)
        self.assertIsNone(response.context["position"])
        self.assertNotContains(response, "₦30,000")

    def test_the_principal_and_owner_get_it_too(self):
        for user in (self.principal, self.owner):
            with self.subTest(user=user.username):
                response = self.lookup(self.ada.admission_number, user=user)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["position"].outstanding, FIFTY_K)

    def test_signed_out_is_sent_to_log_in(self):
        response = self.client.get(self.url, {"student": self.ada.admission_number})
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_the_record_form_wires_the_panel_to_the_lookup(self):
        self.client.force_login(self.bursar)
        response = self.client.get(reverse("payments:record"))
        self.assertContains(response, 'id="position-panel"')
        self.assertContains(response, f'data-position-url="{self.url}"')

    def test_a_re_rendered_form_shows_the_typed_students_balance(self):
        # The pilot bug: once the form was bound, the picker's value was its
        # text, which the panel could not resolve, so it fell back to the prompt.
        self.pay(self.ada, "20000")
        self.client.force_login(self.bursar)
        response = self.client.post(reverse("payments:record"), {
            "student": picker_label(self.ada),
            "amount": "",  # invalid, so the form comes back
            "date_paid": timezone.localdate().isoformat(),
            "label": PaymentLabel.SCHOOL_FEES,
            "method": PaymentMethod.CASH,
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["position"].outstanding, Decimal("30000"))
        self.assertNotContains(response, "Pick a student and their balance appears here.")

    def test_arriving_from_a_students_page_shows_their_balance(self):
        self.client.force_login(self.bursar)
        response = self.client.get(
            reverse("payments:record"), {"student": self.ada.pk}
        )
        self.assertEqual(response.context["position"].outstanding, FIFTY_K)


class ReceiptLinkTests(PaymentsTestCase):
    def setUp(self):
        self.media = tempfile.mkdtemp()
        self.override = override_settings(MEDIA_ROOT=self.media)
        self.override.enable()

    def tearDown(self):
        self.override.disable()
        shutil.rmtree(self.media, ignore_errors=True)

    def open_receipt(self, payment):
        response = self.client.get(reverse("payments:receipt_file", args=[payment.pk]))
        self.assertEqual(response.status_code, 200)
        # Reading it to the end closes it. Closing it again by hand fires the
        # end-of-request signal, which closes the database connection under the test.
        body = b"".join(response.streaming_content)
        return response, body

    def test_a_freshly_uploaded_receipt_opens_from_the_confirmation(self):
        self.client.force_login(self.bursar)
        response = self.client.post(reverse("payments:record"), {
            "student": self.ada.admission_number,
            "amount": "20000",
            "date_paid": timezone.localdate().isoformat(),
            "label": PaymentLabel.SCHOOL_FEES,
            "method": PaymentMethod.TRANSFER,
            "confirm_now": "on",
            "receipt_checked": "on",
            "receipt": slip(),
        }, follow=True)

        payment = Payment.all_objects.get()
        gated = reverse("payments:receipt_file", args=[payment.pk])
        # Landed on the receipt page, which links to the slip.
        self.assertEqual(
            response.redirect_chain[-1][0], reverse("payments:receipt", args=[payment.pk])
        )
        self.assertContains(response, gated)
        self.assertContains(response, "View receipt")

        opened, body = self.open_receipt(payment)
        self.assertEqual(opened["Content-Type"], "image/png")
        self.assertEqual(body, PNG)
        self.assertIn("no-store", opened["Cache-Control"])

    def test_detail_list_and_history_link_to_the_gated_page(self):
        payment = self.pay(self.ada, "20000", receipt=slip())
        gated = reverse("payments:receipt_file", args=[payment.pk])
        pages = [
            reverse("payments:payment_detail", args=[payment.pk]),
            reverse("payments:index"),
            reverse("payments:student_history", args=[self.ada.pk]),
            reverse("payments:receipt", args=[payment.pk]),
        ]
        for user in (self.owner, self.principal, self.bursar):
            self.client.force_login(user)
            for page in pages:
                with self.subTest(user=user.username, page=page):
                    response = self.client.get(page)
                    self.assertContains(response, gated)
                    self.assertContains(response, "View receipt")
                    # Never the raw storage URL.
                    self.assertNotContains(response, payment.receipt.url)
            with self.subTest(user=user.username, page="file"):
                _, body = self.open_receipt(payment)
                self.assertEqual(body, PNG)

    def test_the_pending_queue_and_edit_form_use_the_gated_page(self):
        payment = self.pay(
            self.ada, "20000", status=PaymentStatus.PENDING, receipt=slip()
        )
        gated = reverse("payments:receipt_file", args=[payment.pk])
        self.client.force_login(self.bursar)
        for page in (
            reverse("payments:pending"),
            reverse("payments:payment_update", args=[payment.pk]),
        ):
            with self.subTest(page=page):
                response = self.client.get(page)
                self.assertContains(response, gated)
                self.assertNotContains(response, payment.receipt.url)

    def test_no_link_without_an_attachment(self):
        payment = self.pay(self.ada, "20000")
        self.client.force_login(self.bursar)
        response = self.client.get(reverse("payments:payment_detail", args=[payment.pk]))
        self.assertNotContains(response, "View receipt")
        self.assertEqual(
            self.client.get(
                reverse("payments:receipt_file", args=[payment.pk])
            ).status_code,
            404,
        )

    def test_another_branch_cannot_open_the_slip(self):
        payment = self.pay(self.ada, "20000", receipt=slip())
        self.client.force_login(self.south_bursar)
        response = self.client.get(reverse("payments:receipt_file", args=[payment.pk]))
        self.assertEqual(response.status_code, 404)

    def test_signed_out_cannot_open_the_slip(self):
        payment = self.pay(self.ada, "20000", receipt=slip())
        response = self.client.get(reverse("payments:receipt_file", args=[payment.pk]))
        self.assertEqual(response.status_code, 302)
