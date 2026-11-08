"""Handing a school to a new owner.

What must hold: only the owner can start it, and only after typing their password;
it can only go to an address that can become this school's owner; nothing changes
until the person accepts from the emailed link; the link works once and expires;
and the swap leaves exactly one owner, never two and never none.
"""

from __future__ import annotations

import re
from datetime import timedelta
from io import StringIO

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import OwnershipTransfer
from apps.accounts.ownership import (
    DAILY_LIMIT,
    NewAccount,
    TransferError,
    accept_transfer,
    cancel_transfer,
    decline_transfer,
    hash_token,
    start_transfer,
)
from apps.audit.models import AuditEvent
from apps.core.roles import Role
from apps.schools.models import Branch, School, SchoolStatus

User = get_user_model()
Status = OwnershipTransfer.Status
Afterwards = OwnershipTransfer.Afterwards

STRONG = "violet-harbour-lantern-92"
NEW_PASSWORD = "copper-meadow-violin-47"
ACCEPT_PATH = re.compile(r"/accounts/ownership/accept/([^/\s]+)/")


@override_settings(PUBLIC_BASE_URL="https://example.test")
class OwnershipFixtures(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = School.all_objects.create(name="Riverbank Academy")
        cls.branch = Branch.all_objects.create(school=cls.school, name="Main Campus")
        cls.other_school = School.all_objects.create(name="Hilltop College")

        def make(username, **fields):
            return User.objects.create_user(
                username, email=f"{username}@riverbank.test", password=STRONG, **fields
            )

        cls.platform = make("platform", role=Role.PLATFORM_OWNER, is_staff=True, is_superuser=True)
        cls.owner = make("owner", role=Role.SCHOOL_OWNER, school=cls.school,
                         first_name="Ada", last_name="Obi")
        cls.principal = make("principal", role=Role.PRINCIPAL, school=cls.school, branch=cls.branch)
        cls.bursar = make("bursar", role=Role.BURSAR, school=cls.school, branch=cls.branch)
        cls.elsewhere = make("elsewhere", role=Role.BURSAR, school=cls.other_school)

    def start(self, email="new.owner@example.test", afterwards=Afterwards.LEAVE, **kwargs):
        kwargs.setdefault("stay_branch", self.branch if afterwards == Afterwards.STAY_PRINCIPAL else None)
        return start_transfer(
            school=self.school, email=email, name="Chidi Eze", afterwards=afterwards,
            started_by=kwargs.pop("started_by", self.owner), **kwargs,
        )

    def reload(self, *people):
        for person in people:
            person.refresh_from_db()

    def owners(self):
        return list(User.objects.filter(school=self.school, role=Role.SCHOOL_OWNER, is_active=True))


class StartingTests(OwnershipFixtures):
    def test_it_opens_a_pending_transfer_and_emails_both_people(self):
        started = self.start()
        transfer = started.transfer
        self.assertEqual(transfer.status, Status.PENDING)
        self.assertTrue(started.emailed)
        # Only the hash is kept.
        self.assertNotEqual(transfer.token_hash, started.token)
        self.assertEqual(transfer.token_hash, hash_token(started.token))
        self.assertAlmostEqual(
            transfer.expires_at, timezone.now() + timedelta(hours=72), delta=timedelta(minutes=1)
        )

        to = {message.to[0]: message for message in mail.outbox}
        self.assertEqual(set(to), {"new.owner@example.test", "owner@riverbank.test"})
        invite = to["new.owner@example.test"]
        self.assertIn("https://example.test/accounts/ownership/accept/", invite.body)
        self.assertEqual(ACCEPT_PATH.search(invite.body).group(1), started.token)
        self.assertIn("Riverbank Academy", invite.subject)
        self.assertNotIn(started.token, to["owner@riverbank.test"].body)

    def test_nothing_changes_until_it_is_accepted(self):
        self.start()
        self.assertEqual(self.owners(), [self.owner])

    def test_it_is_written_to_the_audit_log(self):
        transfer = self.start().transfer
        event = AuditEvent.all_objects.get(action="ownership.transfer_started")
        self.assertEqual(event.school_id, self.school.pk)
        self.assertEqual(event.target_id, str(transfer.pk))

    def test_only_the_owner_can_start_one(self):
        with self.assertRaises(TransferError):
            self.start(started_by=self.principal)

    def test_it_refuses_the_owners_own_email(self):
        with self.assertRaisesMessage(TransferError, "your own email"):
            self.start(email="OWNER@riverbank.test")

    def test_it_refuses_an_email_that_signs_in_at_another_school_without_naming_it(self):
        with self.assertRaises(TransferError) as caught:
            self.start(email="elsewhere@riverbank.test")
        self.assertNotIn("Hilltop", str(caught.exception))

    def test_it_refuses_platform_staff(self):
        with self.assertRaises(TransferError):
            self.start(email="platform@riverbank.test")

    def test_it_refuses_a_switched_off_staff_account(self):
        self.bursar.is_active = False
        self.bursar.save()
        with self.assertRaisesMessage(TransferError, "switched off"):
            self.start(email="bursar@riverbank.test")

    def test_staying_on_needs_an_open_campus_of_this_school(self):
        other_campus = Branch.all_objects.create(school=self.other_school, name="Far Campus")
        with self.assertRaisesMessage(TransferError, "campus"):
            self.start(afterwards=Afterwards.STAY_PRINCIPAL, stay_branch=other_campus)

    def test_one_waiting_transfer_at_a_time(self):
        self.start()
        with self.assertRaisesMessage(TransferError, "already waiting"):
            self.start(email="someone.else@example.test")

    def test_a_lapsed_transfer_does_not_block_a_new_one(self):
        first = self.start().transfer
        OwnershipTransfer.all_objects.filter(pk=first.pk).update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )
        self.start(email="someone.else@example.test")
        first.refresh_from_db()
        self.assertEqual(first.status, Status.EXPIRED)

    def test_a_school_can_start_only_a_few_a_day(self):
        for n in range(DAILY_LIMIT):
            transfer = self.start(email=f"person{n}@example.test").transfer
            cancel_transfer(transfer, by=self.owner)
        with self.assertRaisesMessage(TransferError, "a day"):
            self.start(email="one.more@example.test")


class AcceptingTests(OwnershipFixtures):
    def accept_new(self, transfer):
        return accept_transfer(
            transfer, new_account=NewAccount("Chidi", "Eze", NEW_PASSWORD)
        )

    def test_a_new_person_becomes_the_owner_and_the_old_owner_leaves(self):
        transfer = self.start().transfer
        mail.outbox.clear()
        new_owner = self.accept_new(transfer)

        self.reload(self.owner)
        transfer.refresh_from_db()
        self.assertEqual(new_owner.role, Role.SCHOOL_OWNER)
        self.assertEqual(new_owner.school, self.school)
        self.assertIsNone(new_owner.branch)
        self.assertTrue(new_owner.check_password(NEW_PASSWORD))
        self.assertFalse(self.owner.is_active)
        self.assertEqual(self.owners(), [new_owner])
        self.assertEqual(transfer.status, Status.ACCEPTED)
        self.assertEqual(transfer.to_user, new_owner)
        self.assertEqual(
            {m.to[0] for m in mail.outbox}, {"owner@riverbank.test", "new.owner@example.test"}
        )
        self.assertTrue(AuditEvent.all_objects.filter(action="ownership.transferred").exists())

    def test_a_principal_takes_over_and_the_old_owner_stays_as_principal(self):
        transfer = self.start(email="principal@riverbank.test",
                              afterwards=Afterwards.STAY_PRINCIPAL).transfer
        accept_transfer(transfer, user=self.principal)

        self.reload(self.owner, self.principal)
        self.assertEqual(self.principal.role, Role.SCHOOL_OWNER)
        self.assertIsNone(self.principal.branch)
        self.assertEqual(self.owner.role, Role.PRINCIPAL)
        self.assertEqual(self.owner.branch, self.branch)
        self.assertTrue(self.owner.is_active)
        self.assertEqual(self.owners(), [self.principal])

    def test_staff_must_be_signed_in_as_themselves(self):
        transfer = self.start(email="principal@riverbank.test").transfer
        with self.assertRaisesMessage(TransferError, "Sign in as"):
            accept_transfer(transfer, user=self.bursar)
        with self.assertRaisesMessage(TransferError, "Sign in as"):
            accept_transfer(transfer, user=None)
        self.assertEqual(self.owners(), [self.owner])

    def test_the_link_works_once(self):
        transfer = self.start().transfer
        self.accept_new(transfer)
        with self.assertRaises(TransferError):
            accept_transfer(transfer, new_account=NewAccount("Someone", "Else", NEW_PASSWORD))

    def test_an_expired_link_changes_nothing(self):
        transfer = self.start().transfer
        OwnershipTransfer.all_objects.filter(pk=transfer.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        with self.assertRaisesMessage(TransferError, "expired"):
            self.accept_new(transfer)
        self.assertEqual(self.owners(), [self.owner])
        self.assertFalse(User.objects.filter(email="new.owner@example.test").exists())

    def test_a_cancelled_transfer_cannot_be_accepted(self):
        transfer = self.start().transfer
        cancel_transfer(transfer, by=self.owner)
        with self.assertRaises(TransferError):
            self.accept_new(transfer)
        self.assertEqual(self.owners(), [self.owner])

    def test_it_fails_if_the_person_who_started_it_is_no_longer_the_owner(self):
        transfer = self.start().transfer
        self.owner.is_active = False
        self.owner.save()
        with self.assertRaisesMessage(TransferError, "no longer owns"):
            self.accept_new(transfer)

    def test_a_closed_campus_stops_the_handover_rather_than_dropping_the_choice(self):
        transfer = self.start(afterwards=Afterwards.STAY_PRINCIPAL).transfer
        Branch.all_objects.filter(pk=self.branch.pk).update(is_active=False)
        with self.assertRaisesMessage(TransferError, "closed"):
            self.accept_new(transfer)
        self.assertEqual(self.owners(), [self.owner])
        self.assertFalse(User.objects.filter(email="new.owner@example.test").exists())

    def test_the_school_itself_is_unchanged(self):
        before = School.all_objects.values("plan", "status", "name").get(pk=self.school.pk)
        self.accept_new(self.start().transfer)
        after = School.all_objects.values("plan", "status", "name").get(pk=self.school.pk)
        self.assertEqual(before, after)

    def test_declining_tells_the_owner_and_changes_nothing(self):
        transfer = self.start().transfer
        mail.outbox.clear()
        decline_transfer(transfer)
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, Status.DECLINED)
        self.assertEqual([m.to[0] for m in mail.outbox], ["owner@riverbank.test"])
        self.assertEqual(self.owners(), [self.owner])


class ScreenTests(OwnershipFixtures):
    def test_the_settings_card_is_the_owners_alone(self):
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse("accounts:settings")), 'id="ownership"')
        self.client.force_login(self.principal)
        self.assertNotContains(self.client.get(reverse("accounts:settings")), 'id="ownership"')

    def test_staff_cannot_open_the_start_page(self):
        self.client.force_login(self.principal)
        self.assertEqual(self.client.get(reverse("accounts:ownership_start")).status_code, 403)
        self.assertEqual(self.client.post(reverse("accounts:ownership_start"), {}).status_code, 403)

    def post_start(self, **overrides):
        data = {
            "full_name": "Chidi Eze", "email": "new.owner@example.test",
            "afterwards": Afterwards.STAY_PRINCIPAL, "stay_branch": self.branch.pk,
            "password": STRONG,
        }
        data.update(overrides)
        return self.client.post(reverse("accounts:ownership_start"), data)

    def test_the_owner_must_type_their_password(self):
        self.client.force_login(self.owner)
        response = self.post_start(password="not-it")
        self.assertContains(response, "That is not your password.")
        self.assertFalse(OwnershipTransfer.all_objects.exists())

    def test_starting_from_the_screen(self):
        self.client.force_login(self.owner)
        response = self.post_start()
        self.assertRedirects(response, reverse("accounts:settings") + "#ownership",
                             fetch_redirect_response=False)
        transfer = OwnershipTransfer.all_objects.get()
        self.assertEqual(transfer.stay_branch, self.branch)
        page = self.client.get(reverse("accounts:settings"))
        self.assertContains(page, "Waiting for")
        self.assertContains(page, "Cancel the transfer")

    def test_cancelling_from_the_screen(self):
        token = self.start().token
        self.client.force_login(self.owner)
        self.client.post(reverse("accounts:ownership_cancel"))
        self.assertEqual(OwnershipTransfer.all_objects.get().status, Status.CANCELLED)
        self.client.logout()
        response = self.client.get(reverse("accounts:ownership_accept", args=[token]))
        self.assertContains(response, "This transfer is closed")

    def test_a_school_behind_on_its_bill_can_still_hand_over(self):
        School.all_objects.filter(pk=self.school.pk).update(status=SchoolStatus.PAST_DUE)
        self.client.force_login(self.owner)
        self.post_start()
        self.assertTrue(OwnershipTransfer.all_objects.exists())

    def test_an_unknown_link_is_a_404(self):
        response = self.client.get(reverse("accounts:ownership_accept", args=["nope"]))
        self.assertEqual(response.status_code, 404)
        self.assertContains(response, "This link does not work", status_code=404)

    def test_the_link_page_keeps_its_url_from_other_sites(self):
        token = self.start().token
        response = self.client.get(reverse("accounts:ownership_accept", args=[token]))
        # same-origin, not no-referrer: the latter sends `Origin: null` with the
        # page's own forms and the CSRF check refuses them.
        self.assertEqual(response["Referrer-Policy"], "same-origin")

    def test_its_forms_pass_the_csrf_check_as_a_browser_sends_them(self):
        from django.test import Client

        token = self.start().token
        url = reverse("accounts:ownership_accept", args=[token])
        browser = Client(enforce_csrf_checks=True)
        page = browser.get(url)
        csrf = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.content.decode()).group(1)
        response = browser.post(
            url, {"csrfmiddlewaretoken": csrf, "action": "decline"},
            HTTP_ORIGIN="http://testserver", HTTP_REFERER="http://testserver" + url,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(OwnershipTransfer.all_objects.get().status, Status.DECLINED)

    def test_a_new_person_accepts_and_is_signed_in_as_the_owner(self):
        token = self.start().token
        url = reverse("accounts:ownership_accept", args=[token])
        self.assertContains(self.client.get(url), "Accept and take over")
        response = self.client.post(url, {
            "action": "accept", "first_name": "Chidi", "last_name": "Eze",
            "password": NEW_PASSWORD, "password_confirm": NEW_PASSWORD, "understood": "on",
        })
        self.assertEqual(response.status_code, 302)
        new_owner = User.objects.get(email="new.owner@example.test")
        self.assertEqual(new_owner.role, Role.SCHOOL_OWNER)
        self.assertEqual(int(self.client.session["_auth_user_id"]), new_owner.pk)

    def test_accepting_needs_the_box_ticked(self):
        token = self.start().token
        url = reverse("accounts:ownership_accept", args=[token])
        self.client.post(url, {
            "action": "accept", "first_name": "Chidi", "last_name": "Eze",
            "password": NEW_PASSWORD, "password_confirm": NEW_PASSWORD,
        })
        self.assertEqual(self.owners(), [self.owner])

    def test_staff_are_asked_to_sign_in_then_accept(self):
        token = self.start(email="principal@riverbank.test").token
        url = reverse("accounts:ownership_accept", args=[token])
        self.assertContains(self.client.get(url), "Sign in to accept")

        self.client.force_login(self.bursar)
        self.assertContains(self.client.get(url), "but this transfer is for")
        self.client.post(url, {"action": "accept", "understood": "on"})
        self.assertEqual(self.owners(), [self.owner])

        self.client.force_login(self.principal)
        self.client.post(url, {"action": "accept", "understood": "on"})
        self.assertEqual(self.owners(), [self.principal])

    def test_declining_from_the_page(self):
        token = self.start().token
        url = reverse("accounts:ownership_accept", args=[token])
        response = self.client.post(url, {"action": "decline"})
        self.assertContains(response, "You declined")
        self.assertEqual(OwnershipTransfer.all_objects.get().status, Status.DECLINED)


class CommandTests(OwnershipFixtures):
    def test_the_platform_can_start_and_cancel_one(self):
        out = StringIO()
        call_command("transfer_school_ownership", school=self.school.slug,
                     email="new.owner@example.test", name="Chidi Eze", stdout=out)
        transfer = OwnershipTransfer.all_objects.get()
        self.assertIsNone(transfer.started_by)
        self.assertEqual(transfer.afterwards, Afterwards.LEAVE)
        self.assertIn("Current owner: owner", out.getvalue())
        self.assertEqual([m.to[0] for m in mail.outbox], ["new.owner@example.test"])

        call_command("transfer_school_ownership", school=self.school.slug, cancel=True, stdout=out)
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, Status.CANCELLED)

    def test_a_platform_transfer_replaces_every_owner_when_accepted(self):
        call_command("transfer_school_ownership", school=self.school.slug,
                     email="new.owner@example.test", name="Chidi Eze",
                     old_owner="principal", branch="main campus", stdout=StringIO())
        new_owner = accept_transfer(
            OwnershipTransfer.all_objects.get(),
            new_account=NewAccount("Chidi", "Eze", NEW_PASSWORD),
        )
        self.reload(self.owner)
        self.assertEqual(self.owners(), [new_owner])
        self.assertEqual(self.owner.role, Role.PRINCIPAL)

    def test_an_unknown_school_is_an_error(self):
        with self.assertRaises(CommandError):
            call_command("transfer_school_ownership", school="nope",
                         email="a@example.test", name="A B", stdout=StringIO())
