"""Bringing a school's existing records in from one workbook."""

from __future__ import annotations

import shutil
import tempfile
from decimal import Decimal
from io import BytesIO, StringIO

from django.conf import settings
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from openpyxl import Workbook, load_workbook

from apps.audit.models import AuditEvent
from apps.families.models import Family, FamilyMembership
from apps.families.services import link
from apps.fees.models import AdjustmentKind, StudentFeeAdjustment, StudentTermEnrolment, Term
from apps.payments.models import (
    Payment,
    PaymentSource,
    PaymentStatus,
    PaymentTransaction,
    TransactionKind,
)
from apps.payments.tests_allocation import build_other_school, build_school
from apps.schools.models import SchoolModule
from apps.students import fees
from apps.students.models import Sex, Student, StudentStatus

from . import engine
from .models import BatchStatus, ImportBatch, ImportRow, MatchDecision, RowStatus
from .sheets import ALL_SHEETS, BY_NAME

D = Decimal


def workbook(**pages) -> SimpleUploadedFile:
    """``Students=[{"admission_number": ...}]``: columns named by key, as a person would."""
    book = Workbook()
    book.remove(book.active)
    names = {
        "families": "Families", "students": "Students", "links": "Family Links",
        "fees": "Fee Records", "payments": "Payment Records", "allocations": "Payment Allocations",
    }
    for key, rows in pages.items():
        sheet = BY_NAME[names[key]]
        page = book.create_sheet(sheet.name)
        columns = list(sheet.columns)
        page.append([column.label for column in columns])
        for row in rows:
            page.append([row.get(column.key, "") for column in columns])
    buffer = BytesIO()
    book.save(buffer)
    return SimpleUploadedFile("records.xlsx", buffer.getvalue())


def student(number="N1", first="Tola", **extra):
    row = {
        "admission_number": number, "first_name": first, "last_name": "Bello", "sex": "Female",
        "school_class": "JSS 1", "parent_name": "Mr Bello", "parent_phone": "08031111111",
        "campus": "North",
    }
    row.update(extra)
    return row


def payment(legacy="OLD-1", **extra):
    row = {
        "legacy_id": legacy, "date_paid": "2026-09-20", "amount": "30000", "method": "Cash",
        "reference": "", "campus": "North",
    }
    row.update(extra)
    return row


class ImportTestCase(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.media = tempfile.mkdtemp()
        cls.override = override_settings(
            MEDIA_ROOT=cls.media,
            STORAGES={
                **settings.STORAGES,
                "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
            },
        )
        cls.override.enable()

    @classmethod
    def tearDownClass(cls):
        cls.override.disable()
        shutil.rmtree(cls.media, ignore_errors=True)
        super().tearDownClass()

    @classmethod
    def setUpTestData(cls):
        for key, value in build_school().items():
            setattr(cls, key, value)
        SchoolModule.set_state(cls.school, "onboarding", True)
        SchoolModule.set_state(cls.school, "families", True)

    def setUp(self):
        self.client.force_login(self.owner)

    def upload(self, **pages) -> ImportBatch:
        response = self.client.post(reverse("onboarding:index"), {"upload": workbook(**pages)})
        self.assertEqual(response.status_code, 302, getattr(response, "context", None))
        return ImportBatch.all_objects.latest("id")

    def rows(self, batch, sheet=None):
        rows = batch.rows.order_by("id")
        return rows.filter(sheet=sheet) if sheet else rows

    def row(self, batch, sheet=None) -> ImportRow:
        return self.rows(batch, sheet).first()

    def commit(self, batch):
        return self.client.post(reverse("onboarding:commit", args=[batch.pk]))

    def decide(self, row, decision):
        return self.client.post(
            reverse("onboarding:decide", args=[row.pk]), {"decision": decision}
        )

    def imported(self, number="N1"):
        return Student.all_objects.get(admission_number=number, school=self.school)

    def owed(self, child):
        return fees.position_for(Student.all_objects.get(pk=child.pk)).total_owed


class AccessTests(ImportTestCase):
    def test_only_a_school_owner_can_import(self):
        for user in (self.principal, self.bursar):
            self.client.force_login(user)
            self.assertEqual(self.client.get(reverse("onboarding:index")).status_code, 403)

    def test_it_is_off_until_switched_on(self):
        SchoolModule.set_state(self.school, "onboarding", False)
        response = self.client.get(reverse("onboarding:index"))
        self.assertContains(response, "not switched on", status_code=403)

    def test_another_school_cannot_see_a_batch(self):
        batch = self.upload(students=[student()])
        other = build_other_school().school
        SchoolModule.set_state(other, "onboarding", True)
        from apps.accounts.models import User
        from apps.core.roles import Role

        owner = User.objects.create_user(
            "other-owner", password="pw", role=Role.SCHOOL_OWNER, school=other
        )
        self.client.force_login(owner)
        for name in ("detail", "commit", "undo", "discard"):
            method = self.client.get if name == "detail" else self.client.post
            response = method(reverse(f"onboarding:{name}", args=[batch.pk]))
            self.assertEqual(response.status_code, 404, name)
        response = self.decide(self.row(batch), "skip")
        self.assertEqual(response.status_code, 404)

    def test_decisions_commit_and_undo_need_a_post(self):
        batch = self.upload(students=[student()])
        for name in ("commit", "undo", "discard"):
            response = self.client.get(reverse(f"onboarding:{name}", args=[batch.pk]))
            self.assertEqual(response.status_code, 405)


class TemplateTests(ImportTestCase):
    def test_the_template_has_all_six_sheets(self):
        response = self.client.get(reverse("onboarding:template"))
        book = load_workbook(BytesIO(response.content))
        self.assertEqual(book.sheetnames[:6], [sheet.name for sheet in ALL_SHEETS])

    def test_an_untouched_template_is_refused_kindly(self):
        response = self.client.get(reverse("onboarding:template"))
        upload = SimpleUploadedFile("t.xlsx", response.content)
        response = self.client.post(reverse("onboarding:index"), {"upload": upload})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "nothing to import")
        self.assertFalse(ImportBatch.all_objects.exists())


class UploadTests(ImportTestCase):
    def test_nothing_is_written_until_someone_confirms(self):
        before = Student.all_objects.count()
        batch = self.upload(students=[student()], families=[{"family_id": "F9", "name": "The Bellos"}])
        self.assertEqual(batch.status, BatchStatus.DRAFT)
        self.assertEqual(Student.all_objects.count(), before)
        self.assertFalse(Family.all_objects.filter(family_code="F9").exists())

    def test_a_file_that_is_not_a_workbook_is_refused(self):
        upload = SimpleUploadedFile("x.xlsx", b"not a workbook")
        response = self.client.post(reverse("onboarding:index"), {"upload": upload})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "could not be opened")

    def test_a_workbook_with_none_of_the_sheets_is_refused(self):
        book = Workbook()
        book.active.append(["Anything"])
        book.active.append(["At all"])
        buffer = BytesIO()
        book.save(buffer)
        response = self.client.post(
            reverse("onboarding:index"), {"upload": SimpleUploadedFile("x.xlsx", buffer.getvalue())}
        )
        self.assertContains(response, "None of the sheets")

    def test_uploading_the_same_file_again_warns(self):
        upload = workbook(students=[student()]).read()
        for _ in range(2):
            self.client.post(
                reverse("onboarding:index"), {"upload": SimpleUploadedFile("a.xlsx", upload)}
            )
        batch = ImportBatch.all_objects.latest("id")
        response = self.client.get(reverse("onboarding:detail", args=[batch.pk]))
        self.assertContains(response, "same file")

    def test_the_file_is_kept_privately_with_its_hash(self):
        batch = self.upload(students=[student()])
        self.assertEqual(len(batch.file_hash), 64)
        self.assertTrue(batch.file.name.startswith(f"imports/{self.school.pk}/"))


class StudentSheetTests(ImportTestCase):
    def test_a_good_row_creates_the_child(self):
        batch = self.upload(students=[student(date_of_birth="2015-02-03")])
        self.assertEqual(self.row(batch).status, RowStatus.READY)
        self.commit(batch)
        child = self.imported()
        self.assertEqual(child.branch, self.north)
        self.assertEqual(child.school_class, self.n_class)
        batch.refresh_from_db()
        self.assertEqual(batch.status, BatchStatus.COMMITTED)
        self.assertEqual(self.row(batch).status, RowStatus.DONE)

    def test_bad_rows_do_not_block_good_ones(self):
        batch = self.upload(students=[student(), student("N2", first="", school_class="Nope")])
        statuses = list(self.rows(batch).values_list("status", flat=True))
        self.assertEqual(statuses, [RowStatus.READY, RowStatus.PROBLEM])
        self.assertTrue(self.rows(batch)[1].messages)
        self.commit(batch)
        self.assertTrue(Student.all_objects.filter(admission_number="N1").exists())
        self.assertFalse(Student.all_objects.filter(admission_number="N2").exists())

    def test_a_school_with_several_campuses_has_to_say_which(self):
        batch = self.upload(students=[student(campus="")])
        self.assertEqual(self.row(batch).status, RowStatus.PROBLEM)
        self.assertIn("campus", " ".join(self.row(batch).messages).lower())

    def test_campus_can_be_left_out_at_a_one_campus_school(self):
        Student.all_objects.filter(branch=self.south).delete()
        self.south.delete()
        batch = self.upload(students=[student(campus="")])
        self.assertEqual(self.row(batch).status, RowStatus.READY)

    def test_an_unknown_campus_is_a_problem(self):
        batch = self.upload(students=[student(campus="Nowhere")])
        self.assertEqual(self.row(batch).status, RowStatus.PROBLEM)

    def test_the_same_number_twice_in_a_file_is_a_problem(self):
        batch = self.upload(students=[student(), student(first="Other")])
        self.assertEqual(self.rows(batch)[1].status, RowStatus.PROBLEM)

    def test_the_same_number_with_the_same_details_is_the_same_child(self):
        batch = self.upload(students=[student("A1", first="Ada", last_name="Obi")])
        self.assertEqual(self.row(batch).status, RowStatus.MATCHED)
        count = Student.all_objects.count()
        self.commit(batch)
        self.assertEqual(Student.all_objects.count(), count)
        self.assertEqual(self.row(batch).status, RowStatus.MATCHED)

    def test_a_child_already_here_can_be_given_a_family_and_nothing_else_changes(self):
        batch = self.upload(students=[student(
            "A9", first="Stranger", last_name="Obi", family_id="F9", parent_name="Changed",
        )])
        self.commit(batch)
        self.stranger.refresh_from_db()
        self.assertEqual(self.stranger.parent_name, "P")
        self.assertEqual(
            FamilyMembership.all_objects.get(student=self.stranger).family.family_code, "F9"
        )

    def test_the_same_number_with_different_details_goes_to_review_and_never_overwrites(self):
        batch = self.upload(students=[student("A1", first="Someone", last_name="Else")])
        row = self.row(batch)
        self.assertEqual(row.status, RowStatus.REVIEW)
        self.decide(row, "use_existing")
        row.refresh_from_db()
        self.assertEqual(row.status, RowStatus.MATCHED)
        self.commit(batch)
        self.assertEqual(self.imported("A1").first_name, "Ada")

    def test_same_name_and_birthday_with_another_number_is_reviewed_never_merged(self):
        Student.all_objects.filter(pk=self.ada.pk).update(date_of_birth="2015-02-03")
        row_data = student("N7", first="Ada", last_name="Obi", date_of_birth="2015-02-03")
        batch = self.upload(students=[row_data])
        row = self.row(batch)
        self.assertEqual(row.status, RowStatus.REVIEW)

        self.decide(row, "create")
        self.commit(batch)

        self.assertTrue(Student.all_objects.filter(admission_number="N7").exists())
        self.assertTrue(Student.all_objects.filter(pk=self.ada.pk).exists())

    def test_a_shared_surname_and_guardian_name_alone_means_nothing(self):
        batch = self.upload(students=[
            student("N3", last_name="Obi", parent_name="P"),
        ])
        self.assertEqual(self.row(batch).status, RowStatus.READY)
        self.assertFalse(self.row(batch).messages)
        self.commit(batch)
        self.assertFalse(FamilyMembership.all_objects.filter(
            student__admission_number="N3"
        ).exists())

    def test_a_decision_is_remembered_for_the_next_upload(self):
        Student.all_objects.filter(pk=self.ada.pk).update(date_of_birth="2015-02-03")
        row_data = student("N7", first="Ada", last_name="Obi", date_of_birth="2015-02-03")
        self.decide(self.row(self.upload(students=[row_data])), "skip")
        self.assertEqual(MatchDecision.all_objects.count(), 1)
        again = self.upload(students=[row_data])
        self.assertEqual(self.row(again).status, RowStatus.SKIP)


class FamilySheetTests(ImportTestCase):
    def test_a_repeated_family_id_means_one_family(self):
        batch = self.upload(students=[
            student("N1", family_id="F9"), student("N2", first="Kemi", family_id="f9"),
        ])
        self.commit(batch)
        family = Family.all_objects.get(family_code__iexact="F9")
        self.assertEqual(
            FamilyMembership.all_objects.filter(family=family, ended_at__isnull=True).count(), 2
        )

    def test_the_families_sheet_supplies_the_details(self):
        batch = self.upload(
            families=[{"family_id": "F9", "name": "The Bellos", "phone": "08032222222"}],
            students=[student(family_id="F9")],
        )
        self.commit(batch)
        family = Family.all_objects.get(family_code="F9")
        self.assertEqual(family.name, "The Bellos")
        self.assertEqual(family.contact_phone, "08032222222")

    def test_a_family_id_on_the_system_is_used(self):
        batch = self.upload(students=[student(family_id="F-1")])
        self.commit(batch)
        self.assertEqual(
            FamilyMembership.all_objects.get(student__admission_number="N1").family, self.family
        )

    def test_a_family_id_with_other_contact_details_goes_to_review(self):
        Family.all_objects.filter(pk=self.family.pk).update(contact_phone="08039999999")
        batch = self.upload(families=[{"family_id": "F-1", "name": "Obis", "phone": "08030000000"}])
        row = self.row(batch)
        self.assertEqual(row.status, RowStatus.REVIEW)
        self.decide(row, "use_existing")
        self.commit(batch)
        self.family.refresh_from_db()
        self.assertEqual(self.family.contact_phone, "08039999999")

    def test_the_same_family_id_twice_on_the_sheet_is_a_problem(self):
        batch = self.upload(families=[
            {"family_id": "F9", "name": "A"}, {"family_id": "f9", "name": "B"},
        ])
        self.assertEqual(self.rows(batch)[1].status, RowStatus.PROBLEM)

    def test_links_join_children_already_on_the_system(self):
        batch = self.upload(
            families=[{"family_id": "F9", "name": "The Bellos"}],
            links=[{"family_id": "F9", "campus": "North", "admission_number": "A9"}],
        )
        self.commit(batch)
        self.assertEqual(
            FamilyMembership.all_objects.get(student=self.stranger, ended_at__isnull=True).family.family_code,
            "F9",
        )

    def test_a_link_for_a_child_in_another_family_is_a_problem(self):
        batch = self.upload(
            families=[{"family_id": "F9", "name": "The Bellos"}],
            links=[{"family_id": "F9", "campus": "North", "admission_number": "A1"}],
        )
        row = self.row(batch, "links")
        self.assertEqual(row.status, RowStatus.PROBLEM)
        self.assertIn("already", " ".join(row.messages))

    def test_a_link_for_a_child_nobody_knows_is_a_problem(self):
        batch = self.upload(links=[{"family_id": "F-1", "campus": "North", "admission_number": "ZZ"}])
        self.assertEqual(self.row(batch).status, RowStatus.PROBLEM)


class FeeSheetTests(ImportTestCase):
    def fee(self, **extra):
        row = {
            "admission_number": "A9", "campus": "North", "term": "Third Term 2025/2026",
            "school_class": "JSS 1", "kind": "Opening balance", "amount": "12000",
            "description": "Owed from last year",
        }
        row.update(extra)
        return row

    def test_an_earlier_term_is_added_and_the_balance_carries(self):
        before = self.owed(self.stranger)
        batch = self.upload(fees=[self.fee()])
        self.assertEqual(self.row(batch).status, RowStatus.READY)
        self.commit(batch)

        term = Term.all_objects.get(branch=self.north, academic_year="2025/2026", sequence=3)
        self.assertFalse(term.is_current)
        self.assertEqual(self.owed(self.stranger), before + D("12000"))
        self.assertTrue(
            StudentTermEnrolment.all_objects.filter(student=self.stranger, term=term).exists()
        )
        self.assertEqual(
            StudentFeeAdjustment.all_objects.get(student=self.stranger).kind,
            AdjustmentKind.OPENING_BALANCE,
        )

    def test_the_current_term_stays_current(self):
        self.commit(self.upload(fees=[self.fee()]))
        self.n_term.refresh_from_db()
        self.assertTrue(self.n_term.is_current)

    def test_a_term_after_the_current_one_is_a_problem(self):
        batch = self.upload(fees=[self.fee(term="First Term 2030/2031")])
        self.assertEqual(self.row(batch).status, RowStatus.PROBLEM)

    def test_a_term_that_cannot_be_read_is_a_problem(self):
        batch = self.upload(fees=[self.fee(term="Last year")])
        self.assertEqual(self.row(batch).status, RowStatus.PROBLEM)

    def test_an_unknown_kind_or_a_bad_amount_is_a_problem(self):
        batch = self.upload(fees=[self.fee(kind="Nonsense"), self.fee(amount="-4")])
        self.assertEqual(
            list(self.rows(batch).values_list("status", flat=True)),
            [RowStatus.PROBLEM, RowStatus.PROBLEM],
        )

    def test_it_can_name_a_child_in_the_same_file(self):
        batch = self.upload(students=[student()], fees=[self.fee(admission_number="N1")])
        self.commit(batch)
        self.assertEqual(StudentFeeAdjustment.all_objects.get().student.admission_number, "N1")

    def test_a_child_whose_row_was_refused_takes_the_fee_with_them(self):
        batch = self.upload(
            students=[student(school_class="Nope")], fees=[self.fee(admission_number="N1")]
        )
        self.assertEqual(self.row(batch, "fees").status, RowStatus.PROBLEM)


class PaymentSheetTests(ImportTestCase):
    def test_a_payment_naming_one_child_needs_no_allocation_sheet(self):
        batch = self.upload(payments=[payment(
            admission_number="A9", term="First", reference="T-5", payer_name="Mrs Obi",
            receipt_number="OLD/77",
        )])
        self.assertEqual(self.row(batch).status, RowStatus.READY, self.row(batch).messages)
        self.commit(batch)

        row = Payment.all_objects.get(student=self.stranger)
        self.assertEqual(row.amount, D("30000"))
        self.assertEqual(row.source, PaymentSource.IMPORT)
        self.assertEqual(row.status, PaymentStatus.CONFIRMED)
        self.assertEqual(str(row.date_paid), "2026-09-20")
        self.assertEqual(row.term, self.n_term)
        txn = row.transaction
        self.assertEqual(txn.kind, TransactionKind.INDIVIDUAL)
        self.assertEqual(txn.legacy_reference, "OLD-1")
        self.assertEqual(txn.legacy_receipt_number, "OLD/77")
        self.assertEqual(txn.payer_name, "Mrs Obi")

    def test_the_original_date_is_kept_as_when_it_was_confirmed(self):
        self.commit(self.upload(payments=[payment(admission_number="A9", term="First")]))
        row = Payment.all_objects.get(student=self.stranger)
        self.assertEqual(row.confirmed_at.date().isoformat(), "2026-09-20")

    def test_importing_sends_nobody_a_message(self):
        self.commit(self.upload(payments=[payment(admission_number="A9", term="First")]))
        self.assertEqual(mail.outbox, [])

    def test_a_family_payment_is_shared_and_what_is_left_stays_visible(self):
        batch = self.upload(
            payments=[payment(amount="100000", family_id="F-1")],
            allocations=[
                {"legacy_id": "OLD-1", "admission_number": "A1", "campus": "North",
                 "term": "First", "amount": "50000"},
                {"legacy_id": "OLD-1", "admission_number": "A2", "campus": "North",
                 "term": "First", "amount": "30000"},
            ],
        )
        self.assertEqual(self.row(batch, "payments").status, RowStatus.READY)
        self.commit(batch)

        txn = PaymentTransaction.all_objects.get(legacy_reference="OLD-1")
        self.assertEqual(txn.kind, TransactionKind.FAMILY)
        self.assertEqual(txn.family, self.family)
        self.assertEqual(txn.unallocated, D("20000"))
        self.assertEqual(self.owed(self.ada), D("0"))
        self.assertEqual(self.owed(self.bola), D("20000"))

    def test_shares_above_the_payment_refuse_the_whole_payment(self):
        batch = self.upload(
            payments=[payment(amount="10000", family_id="F-1")],
            allocations=[
                {"legacy_id": "OLD-1", "admission_number": "A1", "campus": "North",
                 "term": "First", "amount": "8000"},
                {"legacy_id": "OLD-1", "admission_number": "A2", "campus": "North",
                 "term": "First", "amount": "8000"},
            ],
        )
        self.assertEqual(self.row(batch, "payments").status, RowStatus.PROBLEM)
        self.commit(batch)
        self.assertFalse(PaymentTransaction.all_objects.exists())
        self.assertFalse(Payment.all_objects.exists())

    def test_a_share_for_a_child_outside_the_family_refuses_the_whole_payment(self):
        batch = self.upload(
            payments=[payment(family_id="F-1")],
            allocations=[{"legacy_id": "OLD-1", "admission_number": "A9", "campus": "North",
                          "term": "First", "amount": "8000"}],
        )
        self.assertEqual(self.row(batch, "payments").status, RowStatus.PROBLEM)

    def test_a_share_naming_an_unknown_payment_is_a_problem(self):
        batch = self.upload(allocations=[{
            "legacy_id": "GHOST", "admission_number": "A1", "campus": "North",
            "term": "First", "amount": "8000",
        }])
        self.assertEqual(self.row(batch).status, RowStatus.PROBLEM)

    def test_several_shares_with_no_family_are_a_problem(self):
        batch = self.upload(
            payments=[payment()],
            allocations=[
                {"legacy_id": "OLD-1", "admission_number": "A1", "campus": "North",
                 "term": "First", "amount": "8000"},
                {"legacy_id": "OLD-1", "admission_number": "A9", "campus": "North",
                 "term": "First", "amount": "8000"},
            ],
        )
        self.assertEqual(self.row(batch, "payments").status, RowStatus.PROBLEM)

    def test_a_payment_with_no_child_or_family_is_a_problem(self):
        batch = self.upload(payments=[payment()])
        self.assertEqual(self.row(batch).status, RowStatus.PROBLEM)

    def test_running_the_same_file_again_adds_nothing(self):
        pages = {"payments": [payment(admission_number="A9", term="First")]}
        self.commit(self.upload(**pages))
        again = self.upload(**pages)
        self.assertEqual(self.row(again).status, RowStatus.SKIP)
        self.commit(again)
        self.assertEqual(Payment.all_objects.count(), 1)

    def test_a_possible_duplicate_of_a_recorded_payment_is_reviewed(self):
        Payment.all_objects.create(
            branch=self.north, student=self.stranger, term=self.n_term, amount=D("30000"),
            date_paid="2026-09-20", reference="T-5",
        )
        batch = self.upload(payments=[payment(admission_number="A9", term="First", reference="T-5")])
        row = self.row(batch)
        self.assertEqual(row.status, RowStatus.REVIEW)
        self.decide(row, "skip")
        self.commit(batch)
        self.assertEqual(Payment.all_objects.count(), 1)

    def test_a_voided_payment_is_left_out_and_says_so(self):
        batch = self.upload(payments=[payment(admission_number="A9", term="First", status="Void")])
        row = self.row(batch)
        self.assertEqual(row.status, RowStatus.SKIP)
        self.assertTrue(row.messages)

    def test_a_pending_payment_does_not_count(self):
        self.commit(self.upload(payments=[
            payment(admission_number="A9", term="First", status="Pending"),
        ]))
        self.assertEqual(self.owed(self.stranger), D("50000"))

    def test_a_future_date_is_a_problem(self):
        batch = self.upload(payments=[payment(admission_number="A9", term="First", date_paid="2099-01-01")])
        self.assertEqual(self.row(batch).status, RowStatus.PROBLEM)

    def test_the_money_reconciles_afterwards(self):
        self.commit(self.upload(
            payments=[
                payment("OLD-1", admission_number="A9", term="First"),
                payment("OLD-2", amount="100000", family_id="F-1"),
            ],
            allocations=[{"legacy_id": "OLD-2", "admission_number": "A1", "campus": "North",
                          "term": "First", "amount": "50000"}],
        ))
        out = StringIO()
        call_command("check_payment_transactions", stdout=out)
        self.assertIn("agree", out.getvalue())


class PreviewTests(ImportTestCase):
    def test_it_shows_what_each_sheet_will_do_and_the_effect_on_the_totals(self):
        batch = self.upload(
            students=[student(), student("N2", school_class="Nope")],
            payments=[payment(admission_number="A9", term="First", amount="30000")],
        )
        response = self.client.get(reverse("onboarding:detail", args=[batch.pk]))
        summary = {line["sheet"]: line for line in response.context["summary"]}
        self.assertEqual(summary["Students"]["ready"], 1)
        self.assertEqual(summary["Students"]["problem"], 1)
        self.assertEqual(response.context["money"]["received"], D("30000"))
        self.assertContains(response, "₦30,000")

    def test_it_lists_the_rows_that_will_not_be_imported_and_why(self):
        batch = self.upload(students=[student(school_class="Nope")])
        response = self.client.get(reverse("onboarding:detail", args=[batch.pk]))
        self.assertContains(response, "Nope")


class CommitTests(ImportTestCase):
    def test_open_reviews_hold_it_back(self):
        batch = self.upload(students=[student("A1", first="Someone"), student("N2", first="Kemi")])
        response = self.commit(batch)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Student.all_objects.filter(admission_number="N2").exists())
        batch.refresh_from_db()
        self.assertEqual(batch.status, BatchStatus.DRAFT)

    def test_a_decision_changes_what_depends_on_it(self):
        Student.all_objects.filter(pk=self.ada.pk).update(date_of_birth="2015-02-03")
        batch = self.upload(
            students=[student("N7", first="Ada", last_name="Obi", date_of_birth="2015-02-03")],
            fees=[{"admission_number": "N7", "campus": "North", "term": "First",
                   "kind": "Extra charge", "amount": "100", "description": "x"}],
        )
        self.decide(self.row(batch, "students"), "skip")
        self.assertEqual(self.row(batch, "fees").status, RowStatus.PROBLEM)

    def test_it_is_checked_again_with_the_write_about_to_happen(self):
        batch = self.upload(students=[student()])
        Student.all_objects.create(
            branch=self.north, school_class=self.n_class, admission_number="N1",
            first_name="Enrolled", last_name="Meanwhile", sex=Sex.FEMALE, parent_name="P",
            parent_phone="08031234567",
        )
        self.commit(batch)
        self.assertEqual(self.imported().first_name, "Enrolled")
        self.assertEqual(self.row(batch).status, RowStatus.REVIEW)

    def test_a_large_file_finishes_over_several_requests(self):
        batch = self.upload(students=[student(f"N{i}", first=f"K{i}") for i in range(1, 6)])
        self.assertFalse(engine.commit_batch(batch, actor=self.owner, budget=-1))
        batch.refresh_from_db()
        self.assertEqual(batch.status, BatchStatus.COMMITTING)
        self.assertEqual(Student.all_objects.filter(admission_number__startswith="N").count(), 1)
        while not engine.commit_batch(batch, actor=self.owner, budget=-1):
            pass
        self.assertEqual(Student.all_objects.filter(admission_number__startswith="N").count(), 5)
        batch.refresh_from_db()
        self.assertEqual(batch.status, BatchStatus.COMMITTED)

    def test_pressing_twice_imports_once(self):
        batch = self.upload(students=[student()], fees=[{
            "admission_number": "N1", "campus": "North", "term": "First",
            "kind": "Extra charge", "amount": "100", "description": "x",
        }])
        self.commit(batch)
        self.commit(batch)
        self.assertEqual(StudentFeeAdjustment.all_objects.count(), 1)

    def test_a_row_that_fails_while_writing_leaves_the_others(self):
        batch = self.upload(students=[student(), student("N2", first="Kemi")])
        real = engine._write_student
        calls = []

        def flaky(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise ValueError("boom")
            return real(*args, **kwargs)

        engine._write_student = flaky
        try:
            self.assertTrue(engine.commit_batch(batch, actor=self.owner))
        finally:
            engine._write_student = real
        rows = list(self.rows(batch))
        self.assertEqual(rows[0].status, RowStatus.FAILED)
        self.assertEqual(rows[1].status, RowStatus.DONE)

    def test_it_is_in_the_audit_log(self):
        batch = self.upload(students=[student()])
        self.commit(batch)
        self.assertTrue(
            AuditEvent.all_objects.filter(action="importbatch.committed", target_id=str(batch.pk)).exists()
        )


class UndoTests(ImportTestCase):
    def undo(self, batch, reason="Wrong file"):
        return self.client.post(reverse("onboarding:undo", args=[batch.pk]), {"reason": reason})

    def test_payments_are_voided_and_kept(self):
        batch = self.upload(
            payments=[
                payment("OLD-1", admission_number="A9", term="First"),
                payment("OLD-2", amount="100000", family_id="F-1"),
            ],
            allocations=[{"legacy_id": "OLD-2", "admission_number": "A1", "campus": "North",
                          "term": "First", "amount": "50000"}],
        )
        self.commit(batch)
        self.undo(batch)

        self.assertEqual(Payment.all_objects.exclude(status=PaymentStatus.VOID).count(), 0)
        self.assertEqual(Payment.all_objects.count(), 2)
        self.assertEqual(self.owed(self.ada), D("50000"))
        self.assertEqual(self.owed(self.stranger), D("50000"))
        batch.refresh_from_db()
        self.assertEqual(batch.status, BatchStatus.UNDONE)
        self.assertEqual(batch.undo_reason, "Wrong file")

    def test_the_file_can_be_imported_again_afterwards(self):
        pages = {"payments": [payment(admission_number="A9", term="First")]}
        batch = self.upload(**pages)
        self.commit(batch)
        self.undo(batch)
        again = self.upload(**pages)
        self.assertEqual(self.row(again).status, RowStatus.READY)
        self.commit(again)
        self.assertEqual(self.owed(self.stranger), D("20000"))

    def test_a_new_child_with_nothing_else_on_them_is_removed(self):
        batch = self.upload(students=[student(family_id="F9")])
        self.commit(batch)
        self.undo(batch)
        self.assertFalse(Student.all_objects.filter(admission_number="N1").exists())
        self.assertFalse(Family.all_objects.filter(family_code="F9").exists())

    def test_a_child_with_money_since_then_stays_on_the_roll(self):
        batch = self.upload(students=[student()])
        self.commit(batch)
        child = self.imported()
        Payment.all_objects.create(
            branch=self.north, student=child, term=self.n_term, amount=D("100")
        )
        self.undo(batch)
        child.refresh_from_db()
        self.assertEqual(child.status, StudentStatus.ACTIVE)
        self.assertEqual(Payment.all_objects.get().status, PaymentStatus.CONFIRMED)

    def test_a_child_with_only_voided_imported_money_is_withdrawn(self):
        batch = self.upload(
            students=[student()],
            payments=[payment(admission_number="N1", term="First")],
        )
        self.commit(batch)
        self.undo(batch)
        self.assertEqual(self.imported().status, StudentStatus.WITHDRAWN)

    def test_links_end_and_fee_records_are_voided(self):
        batch = self.upload(
            families=[{"family_id": "F9", "name": "B"}],
            links=[{"family_id": "F9", "campus": "North", "admission_number": "A9"}],
            fees=[{"admission_number": "A9", "campus": "North", "term": "Third Term 2025/2026",
                   "school_class": "JSS 1", "kind": "Opening balance", "amount": "12000",
                   "description": "x"}],
        )
        self.commit(batch)
        self.undo(batch)
        self.assertFalse(
            FamilyMembership.all_objects.filter(student=self.stranger, ended_at__isnull=True).exists()
        )
        self.assertEqual(self.owed(self.stranger), D("50000"))
        self.assertIsNotNone(StudentFeeAdjustment.all_objects.get().voided_at)

    def test_it_needs_a_reason(self):
        batch = self.upload(students=[student()])
        self.commit(batch)
        self.undo(batch, reason="")
        batch.refresh_from_db()
        self.assertEqual(batch.status, BatchStatus.COMMITTED)

    def test_only_a_finished_import_can_be_undone(self):
        batch = self.upload(students=[student()])
        self.undo(batch)
        batch.refresh_from_db()
        self.assertEqual(batch.status, BatchStatus.DRAFT)

    def test_pressing_twice_undoes_once(self):
        batch = self.upload(payments=[payment(admission_number="A9", term="First")])
        self.commit(batch)
        self.undo(batch)
        self.undo(batch)
        self.assertEqual(Payment.all_objects.count(), 1)

    def test_it_is_in_the_audit_log(self):
        batch = self.upload(students=[student()])
        self.commit(batch)
        self.undo(batch)
        self.assertTrue(AuditEvent.all_objects.filter(action="importbatch.undone").exists())


class DiscardAndRetentionTests(ImportTestCase):
    def test_a_draft_can_be_thrown_away(self):
        batch = self.upload(students=[student()])
        self.client.post(reverse("onboarding:discard", args=[batch.pk]))
        self.assertFalse(ImportBatch.all_objects.filter(pk=batch.pk).exists())

    def test_a_finished_import_cannot_be_thrown_away(self):
        batch = self.upload(students=[student()])
        self.commit(batch)
        self.client.post(reverse("onboarding:discard", args=[batch.pk]))
        self.assertTrue(ImportBatch.all_objects.filter(pk=batch.pk).exists())

    def test_old_files_and_their_personal_data_are_removed_on_schedule(self):
        batch = self.upload(students=[student()])
        self.commit(batch)
        ImportBatch.all_objects.filter(pk=batch.pk).update(committed_at="2020-01-01T00:00:00Z")
        call_command("purge_import_files", "--days", "30", stdout=StringIO())
        batch.refresh_from_db()
        self.assertFalse(batch.file)
        self.assertEqual(self.row(batch).raw, {})
        self.assertEqual(batch.status, BatchStatus.COMMITTED)

    def test_recent_files_are_kept(self):
        batch = self.upload(students=[student()])
        call_command("purge_import_files", "--days", "30", stdout=StringIO())
        batch.refresh_from_db()
        self.assertTrue(batch.file)


class FamilySuggestionTests(ImportTestCase):
    def url(self):
        return reverse("families:suggestions")

    def setUp(self):
        super().setUp()
        Student.all_objects.filter(pk=self.stranger.pk).update(parent_phone="08035555555")
        self.twin = Student.all_objects.create(
            branch=self.north, school_class=self.n_class, admission_number="A10",
            first_name="Twin", last_name="Other", sex=Sex.FEMALE, parent_name="Q",
            parent_phone="08035555555",
        )

    def test_a_shared_phone_is_suggested(self):
        response = self.client.get(self.url())
        groups = response.context["groups"]
        self.assertEqual(len(groups), 1)
        self.assertEqual({s.pk for s in groups[0].students}, {self.stranger.pk, self.twin.pk})

    def test_a_shared_surname_or_guardian_name_is_never_suggested(self):
        Student.all_objects.filter(pk=self.twin.pk).update(
            parent_phone="08036666666", last_name="Obi", parent_name="P"
        )
        Student.all_objects.filter(pk=self.stranger.pk).update(last_name="Obi", parent_name="P")
        self.assertEqual(self.client.get(self.url()).context["groups"], [])

    def test_a_phone_on_many_children_is_flagged(self):
        for number in range(6):
            Student.all_objects.create(
                branch=self.north, school_class=self.n_class, admission_number=f"M{number}",
                first_name="M", last_name=f"L{number}", sex=Sex.FEMALE, parent_name="Q",
                parent_phone="08035555555",
            )
        group = self.client.get(self.url()).context["groups"][0]
        self.assertTrue(group.is_crowded)
        self.assertContains(self.client.get(self.url()), "driver")

    def test_confirming_makes_the_family_from_the_chosen_children_only(self):
        group = self.client.get(self.url()).context["groups"][0]
        self.client.post(self.url(), {
            "action": "confirm", "signature": group.signature, "name": "The Others",
            "students": [self.twin.pk, self.stranger.pk],
        })
        family = Family.all_objects.get(name="The Others")
        self.assertEqual(family.memberships.filter(ended_at__isnull=True).count(), 2)

    def test_it_cannot_be_used_to_add_a_child_from_outside_the_group(self):
        group = self.client.get(self.url()).context["groups"][0]
        self.client.post(self.url(), {
            "action": "confirm", "signature": group.signature, "name": "The Others",
            "students": [self.twin.pk, self.sade.pk],
        })
        self.assertFalse(FamilyMembership.all_objects.filter(
            family__name="The Others", student=self.sade
        ).exists())

    def test_rejecting_is_remembered(self):
        group = self.client.get(self.url()).context["groups"][0]
        self.client.post(self.url(), {"action": "reject", "signature": group.signature})
        self.assertEqual(self.client.get(self.url()).context["groups"], [])

    def test_a_new_child_on_that_phone_asks_again(self):
        group = self.client.get(self.url()).context["groups"][0]
        self.client.post(self.url(), {"action": "reject", "signature": group.signature})
        Student.all_objects.create(
            branch=self.north, school_class=self.n_class, admission_number="A11",
            first_name="New", last_name="Child", sex=Sex.FEMALE, parent_name="Q",
            parent_phone="08035555555",
        )
        self.assertEqual(len(self.client.get(self.url()).context["groups"]), 1)

    def test_a_bursar_cannot_use_it(self):
        self.client.force_login(self.bursar)
        self.assertEqual(self.client.get(self.url()).status_code, 403)
