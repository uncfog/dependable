"""Tests for parent messaging.

The four things that must never regress, because each of them is a way to text
the wrong person about money:

* an audience filter resolves to exactly the parents it names;
* "parents who owe" selects only students with an outstanding balance;
* the console provider actually marks a message delivered, so the pilot's
  delivery log is real rather than decorative;
* a branch cannot message another branch's parents -- by filter, by form, or by
  guessing an id.
"""

from __future__ import annotations

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.academics.models import Class, Level
from apps.core.permissions import Capability, has_capability
from apps.core.roles import Role
from apps.core.tenancy import scope_to
from apps.fees.models import FeeComponent, FeeStructure, Term, TermSequence
from apps.schools.models import Branch, School
from apps.students import fees
from apps.students.models import Sex, Student, StudentStatus

from . import audiences, dispatch
from .forms import ComposeForm
from .models import (
    AudienceType,
    Message,
    MessageRecipient,
    MessageStatus,
    SchoolMessagingConfig,
    SenderIdStatus,
)
from .providers import (
    AfricasTalkingProvider,
    Channel,
    ConsoleProvider,
    DeliveryStatus,
    MessagingProvider,
    ProviderKey,
    ProviderNotConfigured,
    SenderIdentity,
    SendResult,
    TermiiProvider,
    get_provider,
)

User = get_user_model()


class MessagingTestCase(TestCase):
    """Two branches of one school, each with a class, a term and parents.

    North is priced and populated; South exists so every scoping assertion has
    a real other branch to fail against rather than an empty one.
    """

    @staticmethod
    def enable_messaging(*schools):
        """Give these schools the messaging module.

        A helper rather than a line repeated in every fixture, because a suite
        that forgot it would fail with a 403 from the middleware -- which looks
        like a permissions bug and is not one.
        """
        from apps.schools.models import SchoolModule

        for school in schools:
            SchoolModule.set_state(school, "messaging", True)

    @classmethod
    def setUpTestData(cls):
        cls.alpha = School.all_objects.create(name="Alpha Schools")
        cls.north = Branch.all_objects.create(school=cls.alpha, name="North")
        cls.south = Branch.all_objects.create(school=cls.alpha, name="South")
        # Messaging is a paid add-on and off until a school is set up to send --
        # see apps/core/modules.py. This school is being tested for its
        # messaging, so it has bought messaging.
        cls.enable_messaging(cls.alpha)

        cls.owner = User.objects.create_user(
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
        cls.south_bursar = User.objects.create_user(
            "south.bursar", password="pw", role=Role.BURSAR,
            school=cls.alpha, branch=cls.south,
        )

        cls.jss1 = Class.all_objects.create(
            branch=cls.north, name="JSS 1", stream="A",
            level=Level.JUNIOR_SECONDARY, year_in_level=1,
        )
        cls.p1 = Class.all_objects.create(
            branch=cls.north, name="Primary 1", level=Level.PRIMARY, year_in_level=1
        )
        cls.south_p1 = Class.all_objects.create(
            branch=cls.south, name="Primary 1", level=Level.PRIMARY, year_in_level=1
        )

        cls.north_term = Term.all_objects.create(
            branch=cls.north, name="First Term 2025/2026", academic_year="2025/2026",
            sequence=TermSequence.FIRST, is_current=True,
        )
        cls.south_term = Term.all_objects.create(
            branch=cls.south, name="First Term 2025/2026", academic_year="2025/2026",
            sequence=TermSequence.FIRST, is_current=True,
        )

        # JSS 1A costs 50,000 a term. Primary 1 is deliberately left unpriced.
        # Every branch here can send: identity is exercised in
        # tests_identity.py, and a batch that cannot go out would make every
        # audience and delivery assertion below untestable.
        #
        # That goes for WhatsApp too. Two tests below send over it, and
        # `dispatch.send` resolves the school's identity before writing
        # anything -- so without an approved WhatsApp half they never reached
        # the batch recording and the per-row failure they exist to assert,
        # and errored on the identity guard instead. The guard is right and
        # stays exactly as it is; it simply belongs to tests_identity.py.
        SchoolMessagingConfig.all_objects.create(
            school=cls.alpha,
            branch=None,
            sender_id="Alpha",
            provider=ProviderKey.BULKSMSNIGERIA,
            status=SenderIdStatus.APPROVED,
            whatsapp_provider=ProviderKey.TERMII_WHATSAPP,
            whatsapp_device_id="alpha-whatsapp-device",
            whatsapp_status=SenderIdStatus.APPROVED,
        )

        cls.jss1_fees = FeeStructure.all_objects.create(
            school_class=cls.jss1, term=cls.north_term
        )
        FeeComponent.all_objects.create(
            fee_structure=cls.jss1_fees, name="School Fees", amount=Decimal("50000")
        )

        cls.ada = cls.make_student("N/001", "Ada", "Obi", cls.jss1, "08031234567")
        cls.bola = cls.make_student("N/002", "Bola", "Ade", cls.jss1, "08039876543")
        cls.chidi = cls.make_student("N/003", "Chidi", "Eze", cls.jss1, "08051112222")
        # Primary 1: same branch, different class, and unpriced this term.
        cls.dami = cls.make_student("N/004", "Dami", "Ola", cls.p1, "08063334444")
        # Withdrawn: never messaged, whatever the filter says.
        cls.gone = cls.make_student(
            "N/005", "Gone", "Away", cls.jss1, "08075556666",
            status=StudentStatus.WITHDRAWN,
        )
        # No number on file: picked by the filter, but unreachable.
        cls.silent = cls.make_student("N/006", "Silent", "Ike", cls.jss1, "")

        cls.south_kid = cls.make_student(
            "S/001", "Sade", "Uche", cls.south_p1, "08087778888"
        )

    @classmethod
    def make_student(cls, number, first, last, klass, phone, **extra):
        return Student.all_objects.create(
            branch=klass.branch,
            school_class=klass,
            admission_number=number,
            first_name=first,
            last_name=last,
            sex=Sex.FEMALE,
            parent_name=f"Parent of {first}",
            parent_phone=phone,
            **extra,
        )

    def as_user(self, user):
        return scope_to(
            school_id=user.school_id, branch_id=user.branch_id, role=user.role
        )


# ===========================================================================
# Audience resolution
# ===========================================================================


class AudienceTests(MessagingTestCase):
    def test_all_reaches_every_active_parent_with_a_number(self):
        with self.as_user(self.north_bursar):
            audience = audiences.resolve(AudienceType.ALL, branch=self.north)

        self.assertEqual(
            {s.pk for s in audience.students},
            {self.ada.pk, self.bola.pk, self.chidi.pk, self.dami.pk},
        )
        self.assertEqual(audience.description, "All parents")

    def test_withdrawn_students_are_never_messaged(self):
        with self.as_user(self.north_bursar):
            audience = audiences.resolve(AudienceType.ALL, branch=self.north)
        self.assertNotIn(self.gone.pk, {s.pk for s in audience.students})

    def test_a_parent_with_no_number_is_reported_not_dropped(self):
        with self.as_user(self.north_bursar):
            audience = audiences.resolve(AudienceType.ALL, branch=self.north)

        self.assertNotIn(self.silent.pk, {s.pk for s in audience.students})
        self.assertEqual([s.pk for s in audience.unreachable], [self.silent.pk])
        self.assertEqual(audience.unreachable_count, 1)

    def test_class_audience_is_only_that_class(self):
        with self.as_user(self.north_bursar):
            audience = audiences.resolve(
                AudienceType.CLASS, branch=self.north, school_class=self.jss1
            )

        self.assertEqual(
            {s.pk for s in audience.students},
            {self.ada.pk, self.bola.pk, self.chidi.pk},
        )
        self.assertEqual(audience.description, "JSS 1A parents")

    def test_selected_students_audience(self):
        with self.as_user(self.north_bursar):
            audience = audiences.resolve(
                AudienceType.STUDENTS,
                branch=self.north,
                students=[self.ada, self.dami],
            )

        self.assertEqual(
            {s.pk for s in audience.students}, {self.ada.pk, self.dami.pk}
        )
        self.assertIn("Ada Obi", audience.description)

    def test_a_filter_with_nothing_chosen_reaches_nobody(self):
        """The failure mode of a broken filter must never be "text everyone"."""
        with self.as_user(self.north_bursar):
            no_class = audiences.resolve(AudienceType.CLASS, branch=self.north)
            no_students = audiences.resolve(AudienceType.STUDENTS, branch=self.north)

        self.assertEqual(no_class.count, 0)
        self.assertEqual(no_students.count, 0)


class OwingAudienceTests(MessagingTestCase):
    """"Parents who owe" is the core use case, so it gets the most scrutiny."""

    def paid(self, **amounts) -> fees.FeeSchedule:
        """A schedule stating who has paid how much, as payments will supply."""
        with self.as_user(self.north_bursar):
            students = list(audiences.contactable(self.north))
            return fees.load(
                students,
                payments={
                    getattr(self, name).pk: Decimal(amount)
                    for name, amount in amounts.items()
                },
            )

    def test_nobody_has_paid_so_every_priced_student_owes(self):
        with self.as_user(self.north_bursar):
            audience = audiences.resolve(
                AudienceType.OWING, branch=self.north, schedule=self.paid()
            )

        self.assertEqual(
            {s.pk for s in audience.students},
            {self.ada.pk, self.bola.pk, self.chidi.pk},
        )

    def test_a_student_paid_in_full_is_left_out(self):
        schedule = self.paid(ada="50000")
        with self.as_user(self.north_bursar):
            audience = audiences.resolve(
                AudienceType.OWING, branch=self.north, schedule=schedule
            )

        self.assertNotIn(self.ada.pk, {s.pk for s in audience.students})
        self.assertEqual(
            {s.pk for s in audience.students}, {self.bola.pk, self.chidi.pk}
        )

    def test_a_part_paid_student_still_owes(self):
        schedule = self.paid(ada="50000", bola="20000")
        with self.as_user(self.north_bursar):
            audience = audiences.resolve(
                AudienceType.OWING, branch=self.north, schedule=schedule
            )

        self.assertEqual(
            {s.pk for s in audience.students}, {self.bola.pk, self.chidi.pk}
        )

    def test_an_overpayment_is_not_a_debt(self):
        schedule = self.paid(ada="60000")
        with self.as_user(self.north_bursar):
            audience = audiences.resolve(
                AudienceType.OWING, branch=self.north, schedule=schedule
            )
        self.assertNotIn(self.ada.pk, {s.pk for s in audience.students})

    def test_an_unpriced_class_is_not_chased(self):
        """Nobody has said what Primary 1 costs, so there is no debt to chase."""
        with self.as_user(self.north_bursar):
            audience = audiences.resolve(
                AudienceType.OWING, branch=self.north, schedule=self.paid()
            )
        self.assertNotIn(self.dami.pk, {s.pk for s in audience.students})

    def test_owing_never_reaches_another_branch(self):
        with self.as_user(self.north_bursar):
            audience = audiences.resolve(AudienceType.OWING, branch=self.north)
        self.assertNotIn(self.south_kid.pk, {s.pk for s in audience.students})


# ===========================================================================
# Providers
# ===========================================================================


class ConsoleProviderTests(TestCase):
    def test_marks_delivered_with_a_reference(self):
        result = ConsoleProvider().send("08031234567", "Fees are due.", Channel.SMS)

        self.assertEqual(result.status, DeliveryStatus.DELIVERED)
        self.assertTrue(result.ok)
        self.assertTrue(result.reference.startswith("console-"))
        self.assertEqual(result.error, "")

    def test_carries_both_channels(self):
        provider = ConsoleProvider()
        self.assertTrue(provider.supports(Channel.SMS))
        self.assertTrue(provider.supports(Channel.WHATSAPP))

    def test_is_always_configured(self):
        self.assertTrue(ConsoleProvider().is_configured)


class ProviderSelectionTests(TestCase):
    def test_console_is_the_default(self):
        self.assertIsInstance(get_provider(), ConsoleProvider)

    @override_settings(MESSAGING_PROVIDER="termii")
    def test_settings_choose_the_provider(self):
        self.assertIsInstance(get_provider(), TermiiProvider)

    @override_settings(MESSAGING_PROVIDER="nonsense")
    def test_an_unknown_provider_is_an_error_not_a_silent_fallback(self):
        from django.core.exceptions import ImproperlyConfigured

        with self.assertRaises(ImproperlyConfigured):
            get_provider()

    def test_every_provider_implements_the_interface(self):
        from .providers import PROVIDERS

        for key, provider_class in PROVIDERS.items():
            with self.subTest(provider=key):
                self.assertTrue(issubclass(provider_class, MessagingProvider))
                self.assertEqual(provider_class.key, key)


class StubProviderTests(TestCase):
    """The one unfinished provider must refuse loudly, never pretend to send.

    Africa's Talking takes its sender from the school's identity exactly as the
    live providers do, so the day it is finished the per-tenant Sender ID
    already flows through it. Termii used to be tested here; it is live now and
    has its own class below.
    """

    identity = SenderIdentity(sender_id="Fulfilled", provider_key="africastalking")

    @override_settings(AFRICASTALKING_USERNAME="", AFRICASTALKING_API_KEY="")
    def test_africastalking_without_credentials_is_not_configured(self):
        self.assertFalse(AfricasTalkingProvider(self.identity).is_configured)

    @override_settings(
        AFRICASTALKING_USERNAME="alpha", AFRICASTALKING_API_KEY="key"
    )
    def test_africastalking_payload_uses_the_plus_form(self):
        payload = AfricasTalkingProvider(self.identity).payload(
            "08031234567", "Hi", Channel.SMS
        )
        self.assertEqual(payload["to"], "+2348031234567")
        self.assertEqual(payload["from"], "Fulfilled")

    def test_africastalking_does_not_claim_whatsapp(self):
        self.assertFalse(
            AfricasTalkingProvider(self.identity).supports(Channel.WHATSAPP)
        )

    @override_settings(AFRICASTALKING_USERNAME="alpha", AFRICASTALKING_API_KEY="k")
    def test_africastalking_reports_failure_rather_than_a_false_success(self):
        result = AfricasTalkingProvider(self.identity).send(
            "08031234567", "Hi", Channel.SMS
        )
        self.assertEqual(result.status, DeliveryStatus.FAILED)


class FailingProvider(MessagingProvider):
    """Fails one nominated number and delivers the rest."""

    key = "failing"
    label = "Failing test provider"
    channels = (Channel.SMS, Channel.WHATSAPP)

    def __init__(self, bad_number: str):
        self.bad_number = bad_number

    def send(self, recipient, message, channel, *, purpose="transactional"):
        if recipient == self.bad_number:
            return SendResult.failure("Number not reachable.")
        return SendResult(status=DeliveryStatus.DELIVERED, reference="ref-1")


class ExplodingProvider(MessagingProvider):
    key = "exploding"
    label = "Exploding test provider"

    def send(self, recipient, message, channel, *, purpose="transactional"):
        raise RuntimeError("the network fell over")


# ===========================================================================
# Dispatch
# ===========================================================================


class DispatchTests(MessagingTestCase):
    def send_to_class(self, provider=None, channel=Channel.SMS):
        with self.as_user(self.north_bursar):
            audience = audiences.resolve(
                AudienceType.CLASS, branch=self.north, school_class=self.jss1
            )
            return dispatch.send(
                body="Fees are due.",
                channel=channel,
                audience=audience,
                branch=self.north,
                sender=self.north_bursar,
                provider=provider or ConsoleProvider(),
            )

    def test_console_send_delivers_every_recipient(self):
        message = self.send_to_class()

        self.assertEqual(message.status, MessageStatus.SENT)
        counts = message.delivery_counts()
        self.assertEqual(counts[DeliveryStatus.DELIVERED], 3)
        self.assertEqual(counts[DeliveryStatus.FAILED], 0)
        self.assertEqual(counts[DeliveryStatus.PENDING], 0)

    def test_a_recipient_row_per_parent_with_the_number_snapshotted(self):
        message = self.send_to_class()
        rows = MessageRecipient.all_objects.filter(message=message)

        self.assertEqual(rows.count(), 3)
        ada_row = rows.get(student=self.ada)
        self.assertEqual(ada_row.phone, "08031234567")
        self.assertEqual(ada_row.parent_name, "Parent of Ada")
        self.assertIsNotNone(ada_row.sent_at)
        self.assertTrue(ada_row.provider_reference)

    def test_the_batch_records_what_it_was_and_who_sent_it(self):
        message = self.send_to_class(channel=Channel.WHATSAPP)

        self.assertEqual(message.audience, "JSS 1A parents")
        self.assertEqual(message.audience_type, AudienceType.CLASS)
        self.assertEqual(message.channel, Channel.WHATSAPP)
        self.assertEqual(message.sender, self.north_bursar)
        self.assertEqual(message.provider, "console")
        self.assertEqual(message.branch, self.north)
        self.assertEqual(message.school, self.alpha)

    def test_one_failure_does_not_stop_the_rest(self):
        message = self.send_to_class(FailingProvider(self.bola.parent_phone))

        counts = message.delivery_counts()
        self.assertEqual(counts[DeliveryStatus.DELIVERED], 2)
        self.assertEqual(counts[DeliveryStatus.FAILED], 1)
        self.assertEqual(message.status, MessageStatus.PARTIAL)

        failed = MessageRecipient.all_objects.get(
            message=message, student=self.bola
        )
        self.assertEqual(failed.error, "Number not reachable.")
        self.assertIsNone(failed.sent_at)

    def test_a_provider_that_raises_becomes_a_failed_row_not_a_500(self):
        message = self.send_to_class(ExplodingProvider())

        self.assertEqual(message.status, MessageStatus.FAILED)
        row = MessageRecipient.all_objects.filter(message=message).first()
        self.assertEqual(row.status, DeliveryStatus.FAILED)
        self.assertIn("the network fell over", row.error)

    def test_a_provider_that_cannot_carry_the_channel_fails_the_rows(self):
        message = self.send_to_class(
            AfricasTalkingProvider(), channel=Channel.WHATSAPP
        )

        self.assertEqual(message.status, MessageStatus.FAILED)
        row = MessageRecipient.all_objects.filter(message=message).first()
        self.assertIn("cannot send over WhatsApp", row.error)

    def test_recording_writes_a_queued_batch_that_has_sent_nothing(self):
        with self.as_user(self.north_bursar):
            audience = audiences.resolve(
                AudienceType.CLASS, branch=self.north, school_class=self.jss1
            )
            message = dispatch.record(
                body="Later.", channel=Channel.SMS, audience=audience,
                branch=self.north, sender=self.north_bursar,
            )

        self.assertEqual(message.status, MessageStatus.QUEUED)
        self.assertEqual(
            message.delivery_counts()[DeliveryStatus.PENDING], 3
        )

    def test_delivering_twice_does_not_message_a_parent_again(self):
        message = self.send_to_class()
        before = list(
            MessageRecipient.all_objects.filter(message=message)
            .values_list("pk", "sent_at")
        )

        dispatch.deliver(message, ConsoleProvider())

        after = list(
            MessageRecipient.all_objects.filter(message=message)
            .values_list("pk", "sent_at")
        )
        self.assertEqual(before, after)


class MessageStatusRollupTests(MessagingTestCase):
    def test_status_follows_the_recipient_rows(self):
        message = Message.all_objects.create(
            branch=self.north, school=self.alpha, body="Hi",
            channel=Channel.SMS, audience="All parents",
        )
        for student, status in (
            (self.ada, DeliveryStatus.DELIVERED),
            (self.bola, DeliveryStatus.DELIVERED),
        ):
            MessageRecipient.all_objects.create(
                branch=self.north, school=self.alpha, message=message,
                student=student, phone=student.parent_phone, status=status,
            )

        self.assertEqual(message.refresh_status(), MessageStatus.SENT)

        MessageRecipient.all_objects.filter(
            message=message, student=self.bola
        ).update(status=DeliveryStatus.FAILED)
        self.assertEqual(message.refresh_status(), MessageStatus.PARTIAL)

        MessageRecipient.all_objects.filter(message=message).update(
            status=DeliveryStatus.FAILED
        )
        self.assertEqual(message.refresh_status(), MessageStatus.FAILED)

        message.refresh_from_db()
        self.assertEqual(message.status, MessageStatus.FAILED)


# ===========================================================================
# Tenant scoping
# ===========================================================================


class ScopingTests(MessagingTestCase):
    def test_a_branch_cannot_resolve_another_branchs_parents(self):
        with self.as_user(self.north_bursar):
            audience = audiences.resolve(AudienceType.ALL, branch=self.north)
        self.assertNotIn(self.south_kid.pk, {s.pk for s in audience.students})

        with self.as_user(self.south_bursar):
            audience = audiences.resolve(AudienceType.ALL, branch=self.south)
        self.assertEqual([s.pk for s in audience.students], [self.south_kid.pk])

    def test_naming_another_branchs_branch_yields_nobody(self):
        """A north bursar handed South's branch object still reaches nobody.

        The scoped manager, not the branch argument, is what does the work.
        """
        with self.as_user(self.north_bursar):
            audience = audiences.resolve(AudienceType.ALL, branch=self.south)
        self.assertEqual(audience.count, 0)

    def test_dispatch_refuses_to_write_a_cross_branch_recipient(self):
        """Even handed a stray student, the batch stays inside its branch."""
        smuggled = audiences.Audience(
            type=AudienceType.STUDENTS,
            description="Smuggled",
            students=[self.ada, self.south_kid],
        )
        with self.as_user(self.north_bursar):
            message = dispatch.record(
                body="Hi", channel=Channel.SMS, audience=smuggled,
                branch=self.north, sender=self.north_bursar,
            )

        rows = MessageRecipient.all_objects.filter(message=message)
        self.assertEqual([r.student_id for r in rows], [self.ada.pk])

    def test_a_branch_cannot_read_another_branchs_message_log(self):
        message = Message.all_objects.create(
            branch=self.south, school=self.alpha, body="South only",
            channel=Channel.SMS, audience="All parents",
        )

        self.client.force_login(self.north_bursar)
        self.assertEqual(
            self.client.get(
                reverse("messaging:message_detail", args=[message.pk])
            ).status_code,
            404,
        )
        response = self.client.get(reverse("messaging:index"))
        self.assertNotContains(response, "South only")

    def test_the_form_rejects_a_class_from_another_branch(self):
        with self.as_user(self.owner):
            form = ComposeForm(
                data={
                    "body": "Hi",
                    "channel": Channel.SMS,
                    "audience_type": AudienceType.CLASS,
                    "school_class": self.south_p1.pk,
                    "branch": self.north.pk,
                }
            )
            self.assertFalse(form.is_valid())
            self.assertIn("school_class", form.errors)

    def test_the_form_rejects_a_student_from_another_branch(self):
        with self.as_user(self.owner):
            form = ComposeForm(
                data={
                    "body": "Hi",
                    "channel": Channel.SMS,
                    "audience_type": AudienceType.STUDENTS,
                    "students": [self.south_kid.pk],
                    "branch": self.north.pk,
                }
            )
            self.assertFalse(form.is_valid())
            self.assertIn("students", form.errors)


# ===========================================================================
# Permissions and screens
# ===========================================================================


class CapabilityTests(TestCase):
    def test_every_role_may_send(self):
        """Chasing a fee is the bursar's job, so they send like a principal.

        Asserted on ``capabilities_for``, the role table itself, rather than on a
        user: a capability is a fact about a role and stays one, and whether a
        particular account can exercise it today also depends on whether its
        school has the messaging module -- which is the next test.
        """
        from apps.core.permissions import capabilities_for

        for role in (
            Role.SCHOOL_OWNER, Role.PRINCIPAL, Role.BURSAR, Role.PLATFORM_OWNER
        ):
            with self.subTest(role=role):
                granted = capabilities_for(role)
                self.assertIn(Capability.SEND_MESSAGES, granted)
                self.assertIn(Capability.VIEW_MESSAGES, granted)

    def test_a_school_without_the_module_holds_neither(self):
        """The withdrawal that closes the fee-reminder button on the bursar's
        dashboard, which is a messaging action drawn on a payments screen."""
        from apps.schools.models import School, SchoolModule

        school = School.all_objects.create(name="No Messaging Academy")
        bursar = User.objects.create_user(
            "quiet.bursar", password="pw", role=Role.BURSAR, school=school
        )
        self.assertFalse(has_capability(bursar, Capability.SEND_MESSAGES))
        self.assertFalse(has_capability(bursar, Capability.VIEW_MESSAGES))
        # Still holds everything that is not messaging.
        self.assertTrue(has_capability(bursar, Capability.VIEW_PAYMENTS))

        SchoolModule.set_state(school, "messaging", True)
        bursar = User.objects.get(pk=bursar.pk)   # a fresh instance: the answer
        self.assertTrue(has_capability(bursar, Capability.SEND_MESSAGES))

    def test_an_anonymous_visitor_may_not(self):
        from django.contrib.auth.models import AnonymousUser

        self.assertFalse(
            has_capability(AnonymousUser(), Capability.SEND_MESSAGES)
        )


class ViewTests(MessagingTestCase):
    def test_signed_out_visitors_are_sent_to_login(self):
        response = self.client.get(reverse("messaging:compose"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])

    def test_a_bursar_reaches_every_messaging_screen(self):
        self.client.force_login(self.north_bursar)
        for name in ("messaging:index", "messaging:compose", "messaging:fee_reminder"):
            with self.subTest(url=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)

    def test_the_compose_screen_counts_the_audience_before_sending(self):
        self.client.force_login(self.north_bursar)
        response = self.client.get(reverse("messaging:compose"))

        self.assertEqual(response.context["preview"].count, 4)
        self.assertEqual(response.context["preview"].unreachable_count, 1)

    def test_the_reminder_shortcut_presets_the_owing_audience(self):
        self.client.force_login(self.north_bursar)
        response = self.client.get(reverse("messaging:fee_reminder"))

        self.assertEqual(
            response.context["form"].initial["audience_type"], AudienceType.OWING
        )
        # Only the three priced JSS 1A students owe; Primary 1 is unpriced.
        self.assertEqual(response.context["preview"].count, 3)

    def test_the_count_endpoint_matches_what_would_be_sent(self):
        self.client.force_login(self.north_bursar)
        response = self.client.get(
            reverse("messaging:recipient_count"),
            {
                "audience_type": AudienceType.CLASS,
                "school_class": self.jss1.pk,
                "branch": self.north.pk,
            },
        )

        self.assertEqual(response.json()["count"], 3)
        self.assertEqual(response.json()["description"], "JSS 1A parents")

    def test_the_count_endpoint_will_not_count_another_branch(self):
        self.client.force_login(self.north_bursar)
        response = self.client.get(
            reverse("messaging:recipient_count"),
            {
                "audience_type": AudienceType.CLASS,
                "school_class": self.south_p1.pk,
                "branch": self.south.pk,
            },
        )
        self.assertEqual(response.json()["count"], 0)

    def test_sending_from_the_screen_writes_the_log_and_redirects_to_it(self):
        self.client.force_login(self.north_bursar)
        response = self.client.post(
            reverse("messaging:compose"),
            {
                "body": "  School fees are due this week.  ",
                "channel": Channel.SMS,
                "audience_type": AudienceType.CLASS,
                "school_class": self.jss1.pk,
                "branch": self.north.pk,
            },
        )

        message = Message.all_objects.get()
        self.assertRedirects(response, message.get_absolute_url())
        self.assertEqual(message.body, "School fees are due this week.")
        self.assertEqual(message.status, MessageStatus.SENT)
        self.assertEqual(message.recipients.count(), 3)

    def test_sending_to_an_empty_audience_is_refused(self):
        empty = Class.all_objects.create(
            branch=self.north, name="JSS 3", level=Level.JUNIOR_SECONDARY,
            year_in_level=3,
        )
        self.client.force_login(self.north_bursar)
        response = self.client.post(
            reverse("messaging:compose"),
            {
                "body": "Hello",
                "channel": Channel.SMS,
                "audience_type": AudienceType.CLASS,
                "school_class": empty.pk,
                "branch": self.north.pk,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Message.all_objects.exists())

    def test_the_detail_screen_shows_the_per_parent_breakdown(self):
        with self.as_user(self.north_bursar):
            audience = audiences.resolve(
                AudienceType.CLASS, branch=self.north, school_class=self.jss1
            )
            message = dispatch.send(
                body="Fees are due.", channel=Channel.SMS, audience=audience,
                branch=self.north, sender=self.north_bursar,
                provider=FailingProvider(self.bola.parent_phone),
            )

        self.client.force_login(self.north_bursar)
        response = self.client.get(message.get_absolute_url())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total"], 3)
        self.assertEqual(len(response.context["failures"]), 1)
        self.assertContains(response, "Number not reachable.")
        self.assertContains(response, "Parent of Ada")

    def test_the_log_summarises_delivery_without_a_query_per_row(self):
        self.send_two_messages()
        self.client.force_login(self.north_bursar)

        with self.assertNumQueries(10):
            # session, user, count for pagination, the annotated page, the two
            # the tenant middleware needs, one for the school's messaging
            # identity, one for the school's enabled modules -- which decides
            # what the sidebar offers and which URLs are reachable at all -- and
            # -- for a bursar only -- one for the school's admissions policy,
            # which decides whether the sidebar offers them the applicant
            # pipeline, and one for the unread count on the notification bell
            # in the header. The point of the assertion is that none of them grows
            # with the number of messages on the page.
            response = self.client.get(reverse("messaging:index"))

        rows = list(response.context["messages_sent"])
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row.total, 3)
            self.assertEqual(row.delivered, 3)

    def send_two_messages(self):
        with self.as_user(self.north_bursar):
            for body in ("First notice.", "Second notice."):
                audience = audiences.resolve(
                    AudienceType.CLASS, branch=self.north, school_class=self.jss1
                )
                dispatch.send(
                    body=body, channel=Channel.SMS, audience=audience,
                    branch=self.north, sender=self.north_bursar,
                    provider=ConsoleProvider(),
                )


class NavigationTests(TestCase):
    def test_messaging_is_reachable_from_the_sidebar_for_every_school_role(self):
        from apps.core.navigation import nav_for

        for role in (Role.SCHOOL_OWNER, Role.PRINCIPAL, Role.BURSAR):
            # The entry is module-gated as well as role-gated, so the module has
            # to be in hand for the question "does this role get it?" to mean
            # anything. The other direction is the next test.
            sections = nav_for(role, "/messaging/", modules={"messaging"})
            items = [i for s in sections for i in s.items if i.label == "Messaging"]
            with self.subTest(role=role):
                self.assertEqual(len(items), 1)
                self.assertTrue(items[0].available)
                self.assertEqual(items[0].href, reverse("messaging:index"))

    def test_the_platform_owner_is_not_offered_a_schools_messaging(self):
        """Writing to a school's parents is the school's work, not the
        platform's. Their half of messaging is the Sender ID roll -- see
        apps/messaging/tests_identity.py::NavigationTests."""
        from apps.core.navigation import nav_for

        labels = {
            i.label
            for s in nav_for(Role.PLATFORM_OWNER, "/", modules={"messaging"})
            for i in s.items
        }
        self.assertNotIn("Messaging", labels)

    def test_a_school_without_the_module_gets_no_messaging_entries(self):
        from apps.core.navigation import nav_for

        for role in (
            Role.SCHOOL_OWNER, Role.PRINCIPAL, Role.BURSAR, Role.PLATFORM_OWNER
        ):
            with self.subTest(role=role):
                labels = {
                    i.label
                    for s in nav_for(role, "/", modules=frozenset())
                    for i in s.items
                }
                self.assertNotIn("Messaging", labels)
                self.assertNotIn("Sender ID", labels)
