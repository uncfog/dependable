"""A send cut off partway must not text anybody twice when it is finished.

gunicorn kills a worker that runs past its timeout, and a long batch through a
real gateway can. What the killed worker had already sent has to be on record by
then, because the rows are the only thing a resumed send can go by.
"""

from __future__ import annotations

from django.test import TestCase

from apps.academics.models import Class, Level
from apps.schools.models import Branch, School
from apps.students.models import Sex, Student

from . import dispatch
from .models import Message, MessageRecipient, MessageStatus
from .providers import Channel, DeliveryStatus, SendResult


class Gateway:
    """Accepts messages until told to die the way a timed-out worker does."""

    key = "fake"
    label = "Fake"

    def __init__(self, die_after=None):
        self.die_after = die_after
        self.sent = []

    def supports(self, channel):
        return True

    def send(self, phone, body, channel, **options):
        if self.die_after is not None and len(self.sent) >= self.die_after:
            raise SystemExit(1)
        self.sent.append(phone)
        return SendResult(status=DeliveryStatus.SENT, reference=f"ref-{phone}")


class InterruptedSendTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        school = School.all_objects.create(name="Alpha")
        branch = Branch.all_objects.create(school=school, name="North")
        klass = Class.all_objects.create(
            branch=branch, name="JSS 1", level=Level.JUNIOR_SECONDARY, year_in_level=1
        )
        cls.message = Message.all_objects.create(
            school=school, branch=branch, body="Fees", channel=Channel.SMS,
            audience="All parents",
        )
        for number in range(5):
            student = Student.all_objects.create(
                branch=branch, school_class=klass, admission_number=f"A{number}",
                first_name="Kid", last_name=str(number), sex=Sex.MALE,
                parent_name="P", parent_phone=f"0803000000{number}",
            )
            MessageRecipient.all_objects.create(
                school=school, branch=branch, message=cls.message, student=student,
                phone=student.parent_phone, parent_name="P",
                status=DeliveryStatus.PENDING,
            )

    def interrupted(self, after=3) -> Gateway:
        gateway = Gateway(die_after=after)
        with self.assertRaises(SystemExit):
            dispatch.deliver(self.message, gateway)
        return gateway

    def test_what_went_out_before_the_kill_is_on_record(self):
        self.interrupted(after=3)

        counts = self.message.delivery_counts()
        self.assertEqual(counts[DeliveryStatus.SENT], 3)
        self.assertEqual(counts[DeliveryStatus.PENDING], 2)

    def test_the_reference_and_time_are_recorded_too(self):
        gateway = self.interrupted(after=1)

        row = MessageRecipient.all_objects.get(phone=gateway.sent[0])
        self.assertEqual(row.provider_reference, f"ref-{gateway.sent[0]}")
        self.assertIsNotNone(row.sent_at)

    def test_finishing_the_batch_texts_only_the_parents_who_were_missed(self):
        gateway = self.interrupted(after=3)
        gateway.die_after = None

        dispatch.deliver(self.message, gateway)

        self.assertEqual(len(gateway.sent), 5)
        self.assertEqual(len(set(gateway.sent)), 5)

    def test_the_rollup_reflects_a_batch_that_was_cut_off(self):
        self.interrupted(after=3)

        self.message.refresh_from_db()
        self.assertNotEqual(self.message.status, MessageStatus.QUEUED)
