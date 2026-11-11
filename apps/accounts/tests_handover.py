"""A principal or bursar handing their own role to someone new."""

from __future__ import annotations

import re
import secrets
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.core.roles import Role
from apps.schools.models import Branch, School

from . import handover as service
from .handover import HandoverError
from .models import RoleHandover
from .ownership import NewAccount

User = get_user_model()
Status = RoleHandover.Status
PW = secrets.token_urlsafe(16)
NEW = "tobi.new@example.test"


@override_settings(PUBLIC_BASE_URL="https://app.example.test")
class HandoverCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = School.all_objects.create(name="Alpha Schools")
        cls.north = Branch.all_objects.create(school=cls.school, name="North")
        cls.south = Branch.all_objects.create(school=cls.school, name="South")
        cls.owner = User.objects.create_user("owner", email="owner@example.test", password=PW,
                                             role=Role.SCHOOL_OWNER, school=cls.school)
        cls.bursar = User.objects.create_user("bursar", email="bursar@example.test", password=PW,
                                              role=Role.BURSAR, school=cls.school, branch=cls.north)
        cls.principal = User.objects.create_user("principal", email="principal@example.test",
                                                 password=PW, role=Role.PRINCIPAL,
                                                 school=cls.school, branch=cls.south)
        other = School.all_objects.create(name="Beta Schools")
        cls.other_owner = User.objects.create_user("beta", email="beta@example.test", password=PW,
                                                   role=Role.SCHOOL_OWNER, school=other)

    def ask(self, user=None, email=NEW):
        return service.request_handover(user=user or self.bursar, email=email, name="Tobi New")

    def link(self) -> str:
        for message in reversed(mail.outbox):
            found = re.search(r"https?://[^/\s]+(/accounts/handover/accept/\S+)", message.body)
            if found:
                return found.group(1)
        raise AssertionError("no accept link was emailed")

    def approved(self):
        handover = self.ask()
        service.approve(handover, by=self.owner)
        handover.refresh_from_db()
        return handover


class RulesTests(HandoverCase):
    def test_asking_tells_the_owner_and_emails_no_link(self):
        handover = self.ask()
        self.assertEqual(handover.status, Status.AWAITING_APPROVAL)
        self.assertEqual(handover.role, Role.BURSAR)
        self.assertEqual(handover.branch, self.north)
        self.assertEqual([m.to for m in mail.outbox], [["owner@example.test"]])
        self.assertNotIn("handover/accept", mail.outbox[0].body)

    def test_only_principals_and_bursars(self):
        with self.assertRaises(HandoverError):
            self.ask(user=self.owner)

    def test_an_email_with_an_account_is_refused_without_saying_where(self):
        for taken in ("beta@example.test", "principal@example.test", "BURSAR@example.test"):
            with self.subTest(email=taken), self.assertRaises(HandoverError) as caught:
                self.ask(email=taken)
            self.assertNotIn("Beta", str(caught.exception))

    def test_one_open_handover_at_a_time(self):
        self.ask()
        with self.assertRaises(HandoverError):
            self.ask(email="someone.else@example.test")

    def test_only_this_schools_owner_decides(self):
        handover = self.ask()
        for wrong in (self.other_owner, self.principal, self.bursar):
            with self.subTest(by=wrong.username), self.assertRaises(HandoverError):
                service.approve(handover, by=wrong)
        handover.refresh_from_db()
        self.assertEqual(handover.status, Status.AWAITING_APPROVAL)

    def test_approval_emails_a_single_use_link(self):
        handover = self.approved()
        self.assertEqual(handover.status, Status.PENDING)
        self.assertEqual(len(handover.token_hash), 64)
        self.assertIn([NEW], [m.to for m in mail.outbox])
        self.assertNotIn(handover.token_hash, self.link())

    def test_accepting_gives_the_same_role_and_campus_and_nothing_more(self):
        handover = self.approved()
        newcomer = service.accept(handover, new_account=NewAccount("Tobi", "New", PW))
        self.assertEqual((newcomer.role, newcomer.school, newcomer.branch),
                         (Role.BURSAR, self.school, self.north))
        self.assertFalse(newcomer.is_superuser or newcomer.is_staff)
        self.bursar.refresh_from_db()
        self.assertFalse(self.bursar.is_active)
        handover.refresh_from_db()
        self.assertEqual((handover.status, handover.to_user), (Status.ACCEPTED, newcomer))
        self.assertTrue(AuditEvent.all_objects.filter(action="handover.completed").exists())

    def test_a_role_change_after_asking_cannot_widen_it(self):
        handover = self.approved()
        self.bursar.role = Role.PRINCIPAL
        self.bursar.save()
        with self.assertRaises(HandoverError):
            service.accept(handover, new_account=NewAccount("Tobi", "New", PW))
        self.assertFalse(User.objects.filter(email=NEW).exists())

    def test_the_link_works_once_and_expires(self):
        handover = self.approved()
        service.accept(handover, new_account=NewAccount("Tobi", "New", PW))
        with self.assertRaises(HandoverError):
            service.accept(handover, new_account=NewAccount("Tobi", "New", PW))
        again = service.request_handover(user=self.principal, email="p.next@example.test", name="P Next")
        service.approve(again, by=self.owner)
        RoleHandover.objects.filter(pk=again.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        with self.assertRaises(HandoverError):
            service.accept(again, new_account=NewAccount("P", "Next", PW))
        self.principal.refresh_from_db()
        self.assertTrue(self.principal.is_active)

    def test_cancel_reject_and_decline_change_nothing(self):
        handover = self.ask()
        service.reject(handover, by=self.owner)
        handover = self.approved()
        service.decline(handover)
        handover = self.approved()
        service.cancel(handover, by=self.bursar)
        handover.refresh_from_db()
        self.assertEqual((handover.status, handover.token_hash), (Status.CANCELLED, ""))
        self.bursar.refresh_from_db()
        self.assertTrue(self.bursar.is_active)
        self.assertFalse(User.objects.filter(email=NEW).exists())


class ScreenTests(HandoverCase):
    def test_the_whole_flow_through_the_screens(self):
        self.client.force_login(self.bursar)
        self.assertContains(self.client.get(reverse("accounts:settings")), "Hand over my role")
        response = self.client.post(reverse("accounts:handover_start"), {
            "full_name": "Tobi New", "email": NEW, "password": PW})
        self.assertRedirects(response, reverse("accounts:settings") + "#handover")
        handover = RoleHandover.objects.get()

        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse("accounts:settings")), "Role handovers waiting for you")
        self.client.post(reverse("accounts:handover_decide", args=[handover.pk]), {"decision": "approve"})
        self.client.logout()

        url = self.link()
        page = self.client.get(url)
        self.assertEqual(page["Referrer-Policy"], "same-origin")
        self.assertContains(page, "Take over as Bursar")
        response = self.client.post(url, {
            "action": "accept", "first_name": "Tobi", "last_name": "New",
            "password": PW, "password_confirm": PW})
        self.assertEqual(response.status_code, 302)
        self.assertContains(self.client.get(url), "already been accepted")  # used once
        newcomer = User.objects.get(email=NEW)
        self.assertEqual(int(self.client.session["_auth_user_id"]), newcomer.pk)

    def test_the_wrong_password_starts_nothing(self):
        self.client.force_login(self.bursar)
        self.client.post(reverse("accounts:handover_start"), {
            "full_name": "Tobi New", "email": NEW, "password": "not-it"})
        self.assertFalse(RoleHandover.objects.exists())

    def test_the_owner_has_no_handover_card_and_cannot_start_one(self):
        self.client.force_login(self.owner)
        self.assertNotContains(self.client.get(reverse("accounts:settings")), "Hand over my role")
        self.assertEqual(self.client.get(reverse("accounts:handover_start")).status_code, 403)

    def test_staff_and_other_schools_cannot_decide(self):
        handover = self.ask()
        url = reverse("accounts:handover_decide", args=[handover.pk])
        for user, status in ((self.principal, 403), (self.other_owner, 404)):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                self.assertEqual(self.client.post(url, {"decision": "approve"}).status_code, status)
        handover.refresh_from_db()
        self.assertEqual(handover.status, Status.AWAITING_APPROVAL)

    def test_a_bad_link_is_a_404_and_a_signed_in_visitor_is_asked_to_sign_out(self):
        self.assertEqual(self.client.get(reverse("accounts:handover_accept", args=["nope"])).status_code, 404)
        self.approved()
        self.client.force_login(self.principal)
        self.assertContains(self.client.get(self.link()), "sign out")

    def test_the_owner_is_notified_in_the_app(self):
        from apps.notifications.models import Notification

        self.client.force_login(self.bursar)
        self.client.post(reverse("accounts:handover_start"), {
            "full_name": "Tobi New", "email": NEW, "password": PW})
        notice = Notification.objects.get(recipient=self.owner, title__contains="hand over")
        self.assertTrue(notice.url.endswith("#handovers"))
