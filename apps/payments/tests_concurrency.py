"""Two copies of one payment arriving at the same moment, on a real database.

The duplicate check relies on a row lock, and SQLite has none, so this only runs on
Postgres. CI runs it there. Real threads with real connections are the only way to
show the lock is doing the work.
"""

from __future__ import annotations

import threading
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, TransactionTestCase, skipUnlessDBFeature
from django.urls import reverse
from django.utils import timezone

from apps.academics.models import Class, Level
from apps.core.roles import Role
from apps.fees.models import Term, TermSequence
from apps.schools.models import Branch, School
from apps.students.models import Sex, Student

from .models import Payment


@skipUnlessDBFeature("has_select_for_update")
class SimultaneousDuplicateTests(TransactionTestCase):
    def test_simultaneous_copies_record_one_payment(self):
        school = School.all_objects.create(name="Alpha")
        branch = Branch.all_objects.create(school=school, name="North")
        klass = Class.all_objects.create(
            branch=branch, name="JSS 1", level=Level.JUNIOR_SECONDARY, year_in_level=1
        )
        Term.all_objects.create(
            branch=branch, name="First", academic_year="2026/2027",
            sequence=TermSequence.FIRST, is_current=True,
        )
        student = Student.all_objects.create(
            branch=branch, school_class=klass, admission_number="A1", first_name="Ada",
            last_name="Obi", sex=Sex.FEMALE, parent_name="P", parent_phone="08031234567",
        )
        bursar = get_user_model().objects.create_user(
            "bursar", password="pw", role=Role.BURSAR, school=school, branch=branch
        )
        data = {
            "student": student.admission_number, "amount": "5000", "label": "school_fees",
            "date_paid": timezone.localdate().isoformat(), "method": "cash",
            "reference": "", "note": "", "confirm_now": "on",
        }
        copies = 6
        ready = threading.Barrier(copies)
        statuses = []

        def send():
            try:
                client = Client()
                client.force_login(bursar)
                ready.wait()
                statuses.append(client.post(reverse("payments:record"), data).status_code)
            finally:
                connection.close()

        threads = [threading.Thread(target=send) for _ in range(copies)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(statuses, [302] * copies)
        self.assertEqual(Payment.all_objects.count(), 1)
        self.assertEqual(Payment.all_objects.get().amount, Decimal("5000"))
