"""What must never regress about Reports.

Four things, in rough order of how much damage they would do if they broke:

1. **Scoping.** One school's report must never contain another's campus, class or
   child -- including when the other school's id is typed straight into the query
   string.
2. **The figures.** Expected, collected and outstanding have to be the same
   numbers the rest of the platform shows, because they come from the same
   derivations. A report that quietly disagreed with the bursar's dashboard would
   be worse than no report.
3. **The export.** The PDF has to be a PDF, arrive as a download, and say the same
   things the page says -- including the one thing that silently went wrong the
   first time it was written: the naira sign has no glyph in a built-in PDF font,
   and ReportLab substitutes ZapfDingbats rather than complaining.
4. **The move.** The breakdowns that came off the dashboards have to actually be
   gone from them and actually be on the report.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.academics.models import Class, Level
from apps.core.navigation import nav_for
from apps.core.permissions import capabilities_for
from apps.core.roles import Role
from apps.fees.models import FeeComponent, FeeStructure, Term, TermSequence
from apps.payments.models import (
    Payment,
    PaymentLabel,
    PaymentMethod,
    PaymentStatus,
)
from apps.schools.models import Branch, School
from apps.students.models import Sex, Student, StudentStatus

from . import pdf, reporting
from .structure import printed_amount

User = get_user_model()
ZERO = Decimal("0")


class ReportsData(TestCase):
    """Two schools, so every scoping assertion has something to fail against.

    Fulfilled Academy: two campuses. Main has a current term, a priced class with
    four children -- two paid in full, one part paid, one untouched -- plus an
    unpriced class with a child in it. Second Campus has children and no current
    term, which is the other kind of hole a report has to report.

    Somewhere Else: one campus, one child, one large payment. Nothing about it
    should ever appear in a Fulfilled report.
    """

    @classmethod
    def setUpTestData(cls):
        cls.school = School.all_objects.create(name="Fulfilled Academy")
        cls.main = Branch.all_objects.create(
            school=cls.school, name="Main Campus", city="Aba", state="Abia"
        )
        cls.second = Branch.all_objects.create(
            school=cls.school, name="Second Campus", city="Owerri", state="Imo"
        )

        cls.other_school = School.all_objects.create(name="Somewhere Else")
        cls.other_branch = Branch.all_objects.create(
            school=cls.other_school, name="Their Campus"
        )

        cls.owner = User.objects.create_user(
            "owner", email="owner@example.com", password="pw",
            role=Role.SCHOOL_OWNER, school=cls.school,
        )
        cls.principal = User.objects.create_user(
            "principal", email="principal@example.com", password="pw",
            role=Role.PRINCIPAL, school=cls.school, branch=cls.main,
        )
        cls.bursar = User.objects.create_user(
            "bursar", email="bursar@example.com", password="pw",
            role=Role.BURSAR, school=cls.school, branch=cls.main,
        )
        cls.platform = User.objects.create_superuser(
            "platform", email="platform@example.com", password="pw",
            role=Role.PLATFORM_OWNER,
        )
        cls.stranger = User.objects.create_user(
            "stranger", email="stranger@example.com", password="pw",
            role=Role.SCHOOL_OWNER, school=cls.other_school,
        )

        cls.term = Term.all_objects.create(
            school=cls.school, branch=cls.main, name="First Term 2026/2027",
            academic_year="2026/2027", sequence=TermSequence.FIRST, is_current=True,
        )
        cls.priced = cls.make_class(cls.main, "Primary 1")
        cls.unpriced = cls.make_class(cls.main, "Primary 2")
        cls.fee = Decimal("50000")
        structure = FeeStructure.all_objects.create(
            school=cls.school, branch=cls.main, school_class=cls.priced,
            term=cls.term,
        )
        FeeComponent.all_objects.create(
            school=cls.school, branch=cls.main, fee_structure=structure,
            name="Tuition", amount=cls.fee,
        )

        # Distinct surnames throughout: `formal_name` is the key several
        # assertions below look rows up by, and four children called the same
        # thing would let a wrong row pass for a right one.
        cls.fully_paid = [
            cls.make_student(cls.main, cls.priced, f"FP{n}", last_name=f"Settled{n}")
            for n in range(2)
        ]
        cls.part_paid = cls.make_student(
            cls.main, cls.priced, "PP1", last_name="Halfway"
        )
        cls.untouched = cls.make_student(
            cls.main, cls.priced, "UT1", last_name="Untouched"
        )
        cls.unpriced_child = cls.make_student(
            cls.main, cls.unpriced, "UP1", last_name="Unpriced"
        )

        for student in cls.fully_paid:
            cls.pay(student, cls.term, cls.fee, PaymentStatus.CONFIRMED)
        cls.pay(cls.part_paid, cls.term, Decimal("20000"), PaymentStatus.CONFIRMED)
        # Handed in, not yet checked: counts toward nothing but the pending line.
        cls.pending_amount = Decimal("5000")
        cls.pay(cls.untouched, cls.term, cls.pending_amount, PaymentStatus.PENDING,
                method=PaymentMethod.TRANSFER, label=PaymentLabel.UNIFORM)

        # A campus with children and no current term.
        cls.second_class = cls.make_class(cls.second, "Primary 1")
        cls.second_child = cls.make_student(cls.second, cls.second_class, "SC1")

        # Another school entirely.
        cls.other_term = Term.all_objects.create(
            school=cls.other_school, branch=cls.other_branch,
            name="Their Term 2026/2027", academic_year="2026/2027",
            sequence=TermSequence.FIRST, is_current=True,
        )
        cls.other_class = cls.make_class(cls.other_branch, "Their Primary 1",
                                         school=cls.other_school)
        other_structure = FeeStructure.all_objects.create(
            school=cls.other_school, branch=cls.other_branch,
            school_class=cls.other_class, term=cls.other_term,
        )
        FeeComponent.all_objects.create(
            school=cls.other_school, branch=cls.other_branch,
            fee_structure=other_structure, name="Tuition",
            amount=Decimal("999999"),
        )
        cls.other_child = cls.make_student(
            cls.other_branch, cls.other_class, "OT1", school=cls.other_school,
            last_name="Elsewhere",
        )
        cls.pay(cls.other_child, cls.other_term, Decimal("777777"),
                PaymentStatus.CONFIRMED, branch=cls.other_branch,
                school=cls.other_school)

    # -- builders ----------------------------------------------------------

    @classmethod
    def make_class(cls, branch, name, school=None):
        return Class.all_objects.create(
            school=school or cls.school, branch=branch, name=name,
            level=Level.PRIMARY, year_in_level=1,
        )

    @classmethod
    def make_student(cls, branch, klass, admission, school=None, last_name="Child"):
        return Student.all_objects.create(
            school=school or cls.school, branch=branch, school_class=klass,
            admission_number=admission, first_name="Test", last_name=last_name,
            sex=Sex.choices[0][0], status=StudentStatus.ACTIVE,
            parent_name=f"Parent of {admission}", parent_phone="08030000000",
        )

    @classmethod
    def pay(cls, student, term, amount, status, *, branch=None, school=None,
            method=PaymentMethod.CASH, label=PaymentLabel.SCHOOL_FEES):
        return Payment.all_objects.create(
            school=school or cls.school, branch=branch or cls.main,
            student=student, term=term, amount=amount, status=status,
            method=method, label=label,
        )

    # -- helpers -----------------------------------------------------------

    def report_for(self, user, **filters):
        """The report ``user`` would get, built through the real scope path."""
        from apps.core.tenancy import TenantContext, activate, deactivate

        token = activate(TenantContext.from_user(user))
        try:
            scope = reporting.resolve_scope(**filters)
            return scope, reporting.build(scope, prepared_for=user.username)
        finally:
            deactivate(token)

    def table(self, report, key):
        return next(t for t in report.tables if t.key == key)

    def stat(self, report, label):
        return next(s for s in report.stats if s.label == label)


# ===========================================================================
# Who may open it
# ===========================================================================


class AccessTests(ReportsData):
    def test_every_role_can_open_the_report_and_its_pdf(self):
        for who in [self.owner, self.principal, self.bursar, self.platform]:
            with self.subTest(role=who.username):
                self.client.force_login(who)
                self.assertEqual(self.client.get(reverse("reports:index")).status_code, 200)
                self.assertEqual(self.client.get(reverse("reports:pdf")).status_code, 200)

    def test_signed_out_visitors_are_sent_to_sign_in(self):
        for url_name in ["reports:index", "reports:pdf"]:
            with self.subTest(screen=url_name):
                response = self.client.get(reverse(url_name))
                self.assertEqual(response.status_code, 302)
                self.assertIn("/accounts/login/", response["Location"])

    def test_the_sidebar_offers_reports_to_every_role_and_it_resolves(self):
        for role in [Role.PLATFORM_OWNER, Role.SCHOOL_OWNER, Role.PRINCIPAL,
                     Role.BURSAR]:
            with self.subTest(role=role):
                entries = {
                    item.label: item
                    for section in nav_for(
                        role,
                        capabilities={c.value for c in capabilities_for(role)},
                    )
                    for item in section.items
                }
                self.assertIn("Reports", entries)
                self.assertTrue(entries["Reports"].available)
                self.assertEqual(entries["Reports"].href, reverse("reports:index"))


# ===========================================================================
# Scoping
# ===========================================================================


class ScopeTests(ReportsData):
    def test_a_principal_sees_only_their_own_campus(self):
        scope, _ = self.report_for(self.principal)
        self.assertEqual([b.name for b in scope.branches], ["Main Campus"])
        self.assertFalse(scope.covers_many_branches)

    def test_an_owner_sees_every_campus_of_their_school_and_no_others(self):
        scope, _ = self.report_for(self.owner)
        self.assertEqual(
            sorted(b.name for b in scope.branches), ["Main Campus", "Second Campus"]
        )
        self.assertNotIn("Their Campus", [b.name for b in scope.branches])

    def test_the_platform_owner_sees_every_school(self):
        scope, report = self.report_for(self.platform)
        self.assertTrue(scope.covers_many_schools)
        labels = [row[0].text for row in self.table(report, "schools").rows]
        self.assertIn("Fulfilled Academy", labels)
        self.assertIn("Somewhere Else", labels)

    def test_another_schools_branch_id_in_the_url_does_not_widen_the_scope(self):
        """The one that matters. A hand-typed id is not an error and is not a
        404 -- it is simply not a choice this caller has, so they get their own
        report."""
        scope, report = self.report_for(
            self.owner, branch_id=self.other_branch.pk
        )
        self.assertNotIn("Their Campus", [b.name for b in scope.branches])
        self.assertEqual(
            sorted(b.name for b in scope.branches), ["Main Campus", "Second Campus"]
        )
        self.assertNotIn("999,999", report.stats[0].value.text)

    def test_another_schools_school_id_in_the_url_does_not_widen_the_scope(self):
        scope, _ = self.report_for(self.owner, school_id=self.other_school.pk)
        self.assertIsNone(scope.chosen_school)
        self.assertEqual(
            sorted(b.name for b in scope.branches), ["Main Campus", "Second Campus"]
        )

    def test_rubbish_in_the_query_string_is_ignored_rather_than_raising(self):
        for value in ["", "abc", "0", "-1", "9999999", None]:
            with self.subTest(value=value):
                scope, _ = self.report_for(self.owner, branch_id=value)
                self.assertEqual(len(scope.branches), 2)

    def test_an_owner_can_narrow_to_one_of_their_own_campuses(self):
        scope, report = self.report_for(self.owner, branch_id=self.main.pk)
        self.assertEqual([b.name for b in scope.branches], ["Main Campus"])
        self.assertEqual(scope.label, "Fulfilled Academy · Main Campus")
        # Narrowed to one campus, the per-campus table has nothing to compare.
        self.assertNotIn("branches", [t.key for t in report.tables])

    def test_no_campus_at_all_produces_a_report_that_says_so(self):
        nobody = User.objects.create_user(
            "nobody", email="nobody@example.com", password="pw",
            role=Role.PRINCIPAL, school=self.school,
        )
        scope, report = self.report_for(nobody)
        self.assertTrue(scope.is_empty)
        self.assertIn("no campus assigned", report.notes[0].text)
        # And it still renders, both ways.
        self.client.force_login(nobody)
        self.assertEqual(self.client.get(reverse("reports:index")).status_code, 200)
        self.assertEqual(self.client.get(reverse("reports:pdf")).status_code, 200)

    def test_another_schools_student_never_appears_on_the_page(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("reports:index"))
        self.assertNotContains(response, "Elsewhere")
        self.assertNotContains(response, "Their Campus")
        self.assertNotContains(response, "777,777")


# ===========================================================================
# The figures
# ===========================================================================


class FigureTests(ReportsData):
    def test_expected_is_the_class_fee_times_the_children_in_it(self):
        _, report = self.report_for(self.principal)
        # Four children in the priced class; the fifth is in an unpriced one and
        # contributes nothing rather than zero.
        self.assertEqual(
            self.stat(report, "Expected this term").value.text,
            f"₦{self.fee * 4:,.0f}",
        )

    def test_collected_counts_confirmed_money_only(self):
        _, report = self.report_for(self.principal)
        expected_collected = self.fee * 2 + Decimal("20000")
        self.assertEqual(
            self.stat(report, "Collected").value.text,
            f"₦{expected_collected:,.0f}",
        )
        self.assertEqual(
            self.stat(report, "Pending confirmation").value.text,
            f"₦{self.pending_amount:,.0f}",
        )

    def test_outstanding_is_expected_less_confirmed(self):
        _, report = self.report_for(self.principal)
        owed = self.fee * 4 - (self.fee * 2 + Decimal("20000"))
        self.assertEqual(
            self.stat(report, "Outstanding").value.text, f"₦{owed:,.0f}"
        )

    def test_the_four_payment_states_are_counted_from_the_roster(self):
        _, report = self.report_for(self.principal)
        states = next(c for c in report.charts if c.key == "states")
        counts = {s.label: s.value.text for s in states.segments}
        self.assertEqual(counts["Paid"], "2")
        self.assertEqual(counts["Part paid"], "1")
        self.assertEqual(counts["Unpaid"], "1")
        self.assertEqual(counts["Overdue"], "0")
        # Four, not five: the child in the unpriced class is in no state at all.
        self.assertEqual(self.stat(report, "Priced students").value.text, "4")

    def test_a_pending_receipt_moves_no_balance(self):
        """The receipt handed in by the untouched child's parent must leave them
        unpaid, not part paid."""
        _, report = self.report_for(self.principal)
        rows = self.table(report, "outstanding").rows
        owed_by = {row[0].text: row[6].text for row in rows}
        # The pending receipt is theirs, and it has moved nothing: they owe the
        # whole fee, not the fee less what was handed in.
        self.assertEqual(owed_by["Untouched, Test"], f"₦{self.fee:,.0f}")
        self.assertEqual(owed_by["Halfway, Test"], f"₦30,000")
        self.assertNotIn("Settled0, Test", owed_by)

    def test_confirming_that_receipt_moves_every_figure_at_once(self):
        before = self.stat(self.report_for(self.principal)[1], "Collected").value.text
        payment = Payment.all_objects.get(status=PaymentStatus.PENDING)
        payment.status = PaymentStatus.CONFIRMED
        payment.save()
        after = self.report_for(self.principal)[1]
        self.assertNotEqual(before, self.stat(after, "Collected").value.text)
        self.assertEqual(
            self.stat(after, "Pending confirmation").value.text, "₦0"
        )
        states = next(c for c in after.charts if c.key == "states")
        counts = {s.label: s.value.text for s in states.segments}
        self.assertEqual(counts["Part paid"], "2")
        self.assertEqual(counts["Unpaid"], "0")

    def test_voiding_a_payment_moves_every_figure_at_once(self):
        before = self.stat(self.report_for(self.principal)[1], "Collected").value.text
        payment = Payment.all_objects.filter(
            status=PaymentStatus.CONFIRMED, amount=self.fee
        ).first()
        payment.void(by=self.owner, reason="Bounced")
        after = self.stat(self.report_for(self.principal)[1], "Collected").value.text
        self.assertNotEqual(before, after)

    def test_an_overdue_term_turns_unpaid_balances_overdue(self):
        self.term.due_date = timezone.localdate() - timedelta(days=1)
        self.term.save()
        _, report = self.report_for(self.principal)
        states = next(c for c in report.charts if c.key == "states")
        counts = {s.label: s.value.text for s in states.segments}
        self.assertEqual(counts["Overdue"], "2")
        self.assertEqual(counts["Unpaid"], "0")
        self.assertEqual(counts["Part paid"], "0")

    def test_the_campus_rows_add_up_to_the_headline(self):
        _, report = self.report_for(self.owner)
        table = self.table(report, "branches")
        self.assertEqual(len(table.rows), 2)
        # The total row is the view's, and it has to equal the headline stat.
        self.assertEqual(
            table.filled_total[4].cell.text,
            self.stat(report, "Collected").value.text,
        )

    def test_a_campus_with_no_current_term_still_gets_a_row_and_a_note(self):
        _, report = self.report_for(self.owner)
        rows = {row[0].text: row for row in self.table(report, "branches").rows}
        self.assertIn("Second Campus", rows)
        self.assertEqual(rows["Second Campus"][1].text, "No current term")
        self.assertTrue(
            any("No current term is set at Second Campus" in n.text
                for n in report.notes)
        )

    def test_an_unpriced_class_with_children_in_it_is_named_in_the_notes(self):
        _, report = self.report_for(self.principal)
        self.assertTrue(
            any("Primary 2 (1 student)" in n.text for n in report.notes),
            [n.text for n in report.notes],
        )

    def test_the_unpriced_class_is_absent_from_the_per_class_table(self):
        _, report = self.report_for(self.principal)
        labels = [row[0].text for row in self.table(report, "classes").rows]
        self.assertIn("Primary 1", labels)
        self.assertNotIn("Primary 2", labels)

    def test_money_is_grouped_by_method_and_by_label(self):
        _, report = self.report_for(self.principal)
        methods = {row[0].text: row[2].text for row in self.table(report, "methods").rows}
        # Only confirmed money: the pending transfer is not here.
        self.assertEqual(list(methods), ["Cash"])
        labels = {row[0].text for row in self.table(report, "labels").rows}
        self.assertEqual(labels, {"School Fees"})

    def test_the_outstanding_list_is_the_same_derivation_the_bursar_chases(self):
        from apps.payments import balances
        from apps.core.tenancy import TenantContext, activate, deactivate

        token = activate(TenantContext.from_user(self.principal))
        try:
            chased = [row.student.formal_name for row in balances.outstanding()]
        finally:
            deactivate(token)
        _, report = self.report_for(self.principal)
        reported = [row[0].text for row in self.table(report, "outstanding").rows]
        self.assertEqual(reported, chased)

    def test_nothing_priced_yet_is_a_report_rather_than_a_crash(self):
        FeeComponent.all_objects.all().delete()
        FeeStructure.all_objects.all().delete()
        _, report = self.report_for(self.principal)
        self.assertEqual(self.stat(report, "Expected this term").value.text, "₦0")
        self.assertEqual(self.stat(report, "Collection rate").value.text, "—")
        # Both classes are now unpriced, so neither is a row: an unknown
        # expected total is not a zero one.
        self.assertEqual(self.table(report, "classes").rows, ())
        self.assertEqual(self.table(report, "class_states").rows, ())
        self.client.force_login(self.principal)
        self.assertEqual(self.client.get(reverse("reports:index")).status_code, 200)
        self.assertEqual(self.client.get(reverse("reports:pdf")).status_code, 200)


# ===========================================================================
# The export
# ===========================================================================


class PdfTests(ReportsData):
    def setUp(self):
        self.client.force_login(self.owner)

    def test_it_arrives_as_a_pdf_download(self):
        response = self.client.get(reverse("reports:pdf"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response["Content-Disposition"].startswith("attachment;"))
        self.assertTrue(response.content.startswith(b"%PDF"))

    def test_the_filename_names_the_school_and_the_day(self):
        response = self.client.get(reverse("reports:pdf"))
        disposition = response["Content-Disposition"]
        self.assertIn("fulfilled-academy", disposition)
        self.assertIn("collections", disposition)
        self.assertIn(timezone.localdate().strftime("%Y-%m-%d"), disposition)
        self.assertTrue(disposition.rstrip('"').endswith(".pdf"))

    def test_the_naira_sign_never_reaches_the_pdf(self):
        """The bug this guards against was silent.

        ReportLab does not refuse a character its font cannot encode -- it
        substitutes ZapfDingbats -- so every amount in the first draft of this
        export rendered as a dingbat, with nothing in any log to say so. The
        report writes the ISO code instead, which means no substitute font should
        ever appear in the output.
        """
        content = self.client.get(reverse("reports:pdf")).content
        self.assertNotIn(b"ZapfDingbats", content)
        self.assertNotIn(b"Symbol", content)
        self.assertIn(b"Helvetica", content)

    def test_the_download_covers_the_same_scope_the_page_shows(self):
        response = self.client.get(
            reverse("reports:pdf"), {"branch": self.main.pk}
        )
        self.assertIn("main-campus", response["Content-Disposition"])

    def test_every_amount_carries_both_spellings(self):
        """The page and the file must differ in the currency symbol and in
        nothing else -- that is the whole contract between the two renderers."""
        _, report = self.report_for(self.owner)
        cells = [stat.value for stat in report.stats]
        cells += [
            cell
            for table in report.tables
            for row in table.rows
            for cell in row
        ]
        money = [c for c in cells if c.text.startswith("₦")]
        self.assertTrue(money, "no money on the report to check")
        for cell in money:
            with self.subTest(cell=cell.text):
                self.assertTrue(cell.printed.startswith("NGN "))
                self.assertEqual(
                    cell.text.removeprefix("₦"),
                    cell.printed.removeprefix("NGN "),
                )

    def test_prose_with_money_in_it_is_printable_too(self):
        _, report = self.report_for(self.owner)
        printed = [note.printed for note in report.notes]
        printed += [t.note.printed for t in report.tables if t.note is not None]
        for sentence in printed:
            with self.subTest(sentence=sentence[:40]):
                self.assertNotIn("₦", sentence)

    def test_it_renders_from_a_report_without_touching_the_database(self):
        """The renderer is handed a finished structure, so drawing it must not
        need a single query. If this ever fails, something in pdf.py has started
        resolving models."""
        _, report = self.report_for(self.owner)
        with self.assertNumQueries(0):
            data = pdf.render(report)
        self.assertTrue(data.startswith(b"%PDF"))

    def test_printed_amount_matches_the_screen_formatter(self):
        self.assertEqual(printed_amount(Decimal("102000")), "NGN 102,000")
        self.assertEqual(printed_amount(Decimal("1250.50")), "NGN 1,250.50")
        self.assertEqual(printed_amount(None), "—")
        self.assertEqual(printed_amount("not a number"), "—")


# ===========================================================================
# What moved off the dashboards
# ===========================================================================


class MovedOffTheDashboardsTests(ReportsData):
    """The dashboards keep their summary figures and one chart each.

    These assert on the tables that moved, by a string only the table carried,
    and on the pointer that replaced each of them.
    """

    def test_the_bursar_dashboard_keeps_its_figures_and_its_one_chart(self):
        self.client.force_login(self.bursar)
        response = self.client.get(reverse("core:bursar_dashboard"))
        self.assertContains(response, "Collected this term")
        self.assertContains(response, "Waiting for you")
        # Gone: the per-class table, and the second chart beside the meter.
        self.assertNotContains(response, "What each class is worth")
        self.assertNotContains(response, "Where the roster stands")
        self.assertContains(response, reverse("reports:index"))

    def test_the_school_dashboard_keeps_its_figures_and_its_one_chart(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("core:school_dashboard"))
        self.assertContains(response, "Fees across every campus")
        self.assertNotContains(response, "Your campuses")
        self.assertContains(response, reverse("reports:index"))

    def test_the_branch_dashboard_keeps_its_figures_and_its_one_chart(self):
        self.client.force_login(self.principal)
        response = self.client.get(reverse("core:branch_dashboard"))
        self.assertContains(response, "Collected this term")
        self.assertNotContains(response, "Classes on this campus")
        self.assertContains(response, reverse("reports:index"))

    def test_the_campus_table_is_on_the_report_instead(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("reports:index"))
        self.assertContains(response, "By campus")
        self.assertContains(response, "Second Campus")

    def test_the_class_table_is_on_the_report_instead(self):
        self.client.force_login(self.principal)
        response = self.client.get(reverse("reports:index"))
        self.assertContains(response, "By class")
        self.assertContains(response, "Payment status by class")
        self.assertContains(response, "Primary 1")

    def test_the_dashboards_still_warn_about_holes_in_their_figures(self):
        """A missing term and an unpriced class are alerts, not breakdowns, so
        they stayed behind when the tables left."""
        self.client.force_login(self.owner)
        self.assertContains(
            self.client.get(reverse("core:school_dashboard")),
            "No current term at Second Campus",
        )
        self.client.force_login(self.principal)
        self.assertContains(
            self.client.get(reverse("core:branch_dashboard")), "Primary 2"
        )


# ===========================================================================
# The picker
# ===========================================================================


class ScopeFormTests(ReportsData):
    def test_a_principal_is_offered_no_choice_at_all(self):
        self.client.force_login(self.principal)
        response = self.client.get(reverse("reports:index"))
        self.assertFalse(response.context["scope_form"].is_useful)

    def test_an_owner_with_two_campuses_is_offered_the_campus_select_only(self):
        self.client.force_login(self.owner)
        form = self.client.get(reverse("reports:index")).context["scope_form"]
        self.assertIn("branch", form.fields)
        self.assertNotIn("school", form.fields)
        self.assertEqual(
            [label for _, label in form.fields["branch"].choices],
            ["Every campus", "Main Campus", "Second Campus"],
        )

    def test_the_platform_owner_is_offered_both(self):
        self.client.force_login(self.platform)
        form = self.client.get(reverse("reports:index")).context["scope_form"]
        self.assertIn("school", form.fields)
        self.assertIn("branch", form.fields)

    def test_choosing_a_school_narrows_the_campus_select_to_it(self):
        self.client.force_login(self.platform)
        form = self.client.get(
            reverse("reports:index"), {"school": self.school.pk}
        ).context["scope_form"]
        offered = [label for _, label in form.fields["branch"].choices]
        self.assertNotIn("Their Campus", offered)
        self.assertIn("Main Campus", offered)

    def test_an_owner_is_never_offered_another_schools_campus(self):
        self.client.force_login(self.owner)
        form = self.client.get(reverse("reports:index")).context["scope_form"]
        self.assertNotIn(
            "Their Campus", [label for _, label in form.fields["branch"].choices]
        )
