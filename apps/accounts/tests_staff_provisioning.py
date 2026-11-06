"""A proprietor creating logins for their own staff.

Before this, a school's second account came from somebody with a shell running
``invite_school_owner``. That is fine for the first owner -- there is nobody at
the school to do it yet -- and wrong for everybody after: a proprietor who
hires a bursar on Monday should not be waiting on the platform to let them in.

Three things these tests hold:

* the proprietor never learns the password -- the invited person sets their own
  from the emailed link, and the tests follow that link to the end;
* permission role and job title stay separate, because promoting a bursar to
  "Head of Finance" is an HR change and not an access change;
* an owner creates accounts inside their own school and nowhere else.
"""

from __future__ import annotations

import re

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.core.permissions import Capability, capabilities_for
from apps.core.roles import Role
from apps.schools.models import Branch, School

User = get_user_model()

CREATE_URL = reverse("staff:create")
LIST_URL = reverse("staff:list")

#: Long enough to pass the length validator and not in any breach list.
NEW_PASSWORD = "kedu-nwanne-2026-owerri"


def form_data(**overrides) -> dict:
    data = {
        "full_name": "Chidinma Eze",
        "email": "chidinma@example.com",
        "role": Role.BURSAR,
        "branch": "",
        "job_title": "Finance Officer",
        "phone": "08031234567",
    }
    data.update(overrides)
    return data


@override_settings(PUBLIC_BASE_URL="https://example.test")
class StaffProvisioningTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = School.all_objects.create(name="Fulfilled Academy")
        cls.branch = Branch.all_objects.create(school=cls.school, name="Main Campus")
        cls.other_school = School.all_objects.create(name="Rival Academy")
        cls.other_branch = Branch.all_objects.create(
            school=cls.other_school, name="Their Campus"
        )
        cls.owner = User.objects.create_user(
            "owner", email="owner@example.com", password="pw",
            role=Role.SCHOOL_OWNER, school=cls.school,
        )
        cls.principal = User.objects.create_user(
            "principal", email="principal@example.com", password="pw",
            role=Role.PRINCIPAL, school=cls.school, branch=cls.branch,
        )
        cls.bursar = User.objects.create_user(
            "bursar", email="bursar@example.com", password="pw",
            role=Role.BURSAR, school=cls.school, branch=cls.branch,
        )
        cls.other_owner = User.objects.create_user(
            "rival-owner", email="rival@example.com", password="pw",
            role=Role.SCHOOL_OWNER, school=cls.other_school,
        )

    def created(self, email="chidinma@example.com"):
        return User.objects.get(email=email)


class OwnerCreatesStaffTests(StaffProvisioningTestCase):
    def setUp(self):
        self.client.force_login(self.owner)

    def test_the_form_opens(self):
        response = self.client.get(CREATE_URL)
        self.assertEqual(response.status_code, 200)

    def test_it_creates_an_account_in_their_own_school(self):
        response = self.client.post(
            CREATE_URL, form_data(branch=self.branch.id)
        )
        self.assertEqual(response.status_code, 302)
        person = self.created()
        self.assertEqual(person.school_id, self.school.pk)
        self.assertEqual(person.branch_id, self.branch.pk)
        self.assertEqual(person.role, Role.BURSAR)
        self.assertEqual(person.first_name, "Chidinma")
        self.assertEqual(person.last_name, "Eze")
        self.assertTrue(person.is_active)
        # Not platform staff, whatever their role at the school.
        self.assertFalse(person.is_staff)
        self.assertFalse(person.is_superuser)

    def test_the_permission_role_and_the_job_title_stay_separate(self):
        self.client.post(
            CREATE_URL,
            form_data(
                branch=self.branch.id, role=Role.BURSAR, job_title="Head of Finance"
            ),
        )
        person = self.created()
        self.assertEqual(person.role, Role.BURSAR)
        self.assertEqual(person.job_title, "Head of Finance")
        # The title bought them nothing.
        self.assertNotIn(Capability.MANAGE_FEES, capabilities_for(person.role))

    def test_nobody_ever_learns_the_password(self):
        self.client.post(CREATE_URL, form_data(branch=self.branch.id))
        person = self.created()
        # Usable -- an unusable one would make the reset email unreachable --
        # but random, and never printed anywhere.
        self.assertTrue(person.has_usable_password())
        for guess in ["", "password", NEW_PASSWORD, person.username, person.email]:
            self.assertFalse(person.check_password(guess))

    def test_the_invitation_is_emailed(self):
        self.client.post(CREATE_URL, form_data(branch=self.branch.id))
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["chidinma@example.com"])
        self.assertIn("SCHOOLCORD", message.subject)
        self.assertIn("/accounts/reset/", message.body)

    def test_a_principal_can_be_created_too(self):
        self.client.post(
            CREATE_URL,
            form_data(email="p2@example.com", role=Role.PRINCIPAL,
                      branch=self.branch.id),
        )
        self.assertEqual(self.created("p2@example.com").role, Role.PRINCIPAL)

    def test_a_second_owner_cannot_be_invited_at_all(self):
        """This used to create one, and check it was stored without a campus.

        It is refused now. There is one proprietor per school, and an owner who
        could invite a second would be handing over the only role that can invite
        and the only one that can deactivate an account -- with no way to take it
        back from this screen. "Can I add another owner?" is a conversation with
        the platform rather than a dropdown.

        Refused in `clean_role` as well as absent from the choices, which is what
        this posts past: a `<select>` is a suggestion and a POST is not.
        """
        response = self.client.post(
            CREATE_URL,
            form_data(email="co@example.com", role=Role.SCHOOL_OWNER,
                      branch=self.branch.id),
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(email="co@example.com").exists())

    def test_the_dropdown_offers_a_principal_and_a_bursar_and_nothing_else(self):
        response = self.client.get(CREATE_URL)
        offered = {
            value for value, _ in response.context["form"].fields["role"].choices
        }
        self.assertEqual(offered, {Role.PRINCIPAL, Role.BURSAR})

    def test_a_branch_role_without_a_campus_is_refused(self):
        response = self.client.post(CREATE_URL, form_data(branch=""))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(email="chidinma@example.com").exists())
        self.assertContains(response, "works at one campus")

    def test_a_duplicate_email_is_refused_rather_than_shadowing_an_account(self):
        response = self.client.post(
            CREATE_URL, form_data(email="bursar@example.com", branch=self.branch.id)
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already exists")
        self.assertEqual(User.objects.filter(email="bursar@example.com").count(), 1)

    def test_the_new_account_appears_on_the_staff_list(self):
        self.client.post(CREATE_URL, form_data(branch=self.branch.id))
        response = self.client.get(LIST_URL)
        self.assertContains(response, "Chidinma Eze")
        self.assertContains(response, "Finance Officer")

    def test_the_invite_can_be_sent_again(self):
        self.client.post(CREATE_URL, form_data(branch=self.branch.id))
        mail.outbox.clear()
        response = self.client.post(
            reverse("staff:resend_invite", args=[self.created().pk])
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)


class TheInvitedPersonGetsInTests(StaffProvisioningTestCase):
    """The link in the email has to actually work, or none of this does."""

    def invite(self) -> str:
        self.client.force_login(self.owner)
        self.client.post(CREATE_URL, form_data(branch=self.branch.id))
        self.client.logout()
        link = re.search(r"https?://\S+/accounts/reset/\S+", mail.outbox[0].body)
        self.assertIsNotNone(link, mail.outbox[0].body)
        return link.group(0)

    def test_the_link_lets_them_set_a_password_and_sign_in(self):
        url = self.invite()
        # Django's reset view swaps the token for a session-held one and
        # redirects; follow it, then post the new password to where we land.
        response = self.client.get(url, follow=True)
        self.assertEqual(response.status_code, 200)
        response = self.client.post(
            response.redirect_chain[-1][0] if response.redirect_chain
            else response.request["PATH_INFO"],
            {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)

        person = self.created()
        person.refresh_from_db()
        self.assertTrue(person.check_password(NEW_PASSWORD))

        self.assertTrue(
            self.client.login(username=person.username, password=NEW_PASSWORD)
        )

    def test_they_land_on_the_screens_their_role_allows(self):
        url = self.invite()
        response = self.client.get(url, follow=True)
        self.client.post(
            response.redirect_chain[-1][0],
            {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD},
            follow=True,
        )
        person = self.created()
        self.client.force_login(person)
        # A bursar reads fees and records payments...
        self.assertEqual(self.client.get(reverse("fees:structure_list")).status_code, 200)
        # ...and is not let near the staff list or branches.
        self.assertEqual(self.client.get(LIST_URL).status_code, 403)

    def test_the_link_is_single_use(self):
        url = self.invite()
        response = self.client.get(url, follow=True)
        self.client.post(
            response.redirect_chain[-1][0],
            {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD},
            follow=True,
        )
        # The token is spent: the same link now shows the expired-link page,
        # which offers to send a fresh one rather than a password form.
        again = self.client.get(url, follow=True)
        self.assertContains(again, "That link has expired")
        self.assertNotContains(again, 'name="new_password1"')


class OnlyTheOwnerProvisionsTests(StaffProvisioningTestCase):
    def test_a_bursar_cannot_reach_the_form_or_post_to_it(self):
        self.client.force_login(self.bursar)
        self.assertEqual(self.client.get(CREATE_URL).status_code, 403)
        self.assertEqual(
            self.client.post(CREATE_URL, form_data(branch=self.branch.id)).status_code,
            403,
        )
        self.assertFalse(User.objects.filter(email="chidinma@example.com").exists())

    def test_a_principal_cannot_either(self):
        self.client.force_login(self.principal)
        self.assertEqual(self.client.get(CREATE_URL).status_code, 403)
        self.assertEqual(
            self.client.post(CREATE_URL, form_data(branch=self.branch.id)).status_code,
            403,
        )

    def test_a_refused_attempt_sends_no_email(self):
        self.client.force_login(self.bursar)
        self.client.post(CREATE_URL, form_data(branch=self.branch.id))
        self.assertEqual(mail.outbox, [])

    def test_a_signed_out_visitor_is_sent_to_sign_in(self):
        response = self.client.get(CREATE_URL)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])

    def test_only_the_owner_roles_hold_the_capability(self):
        for role in [Role.SCHOOL_OWNER, Role.PLATFORM_OWNER]:
            self.assertIn(Capability.MANAGE_STAFF, capabilities_for(role), role)
        for role in [Role.PRINCIPAL, Role.BURSAR]:
            self.assertNotIn(Capability.MANAGE_STAFF, capabilities_for(role), role)

    def test_a_school_cannot_mint_a_platform_owner(self):
        """The one role that must never come from a tenant's own screen."""
        self.client.force_login(self.owner)
        response = self.client.post(
            CREATE_URL,
            form_data(email="attack@example.com", role=Role.PLATFORM_OWNER),
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(email="attack@example.com").exists())

    def test_the_role_choices_offered_exclude_platform_owner(self):
        self.client.force_login(self.owner)
        response = self.client.get(CREATE_URL)
        offered = dict(response.context["form"].fields["role"].choices)
        self.assertNotIn(Role.PLATFORM_OWNER, offered)
        self.assertIn(Role.BURSAR, offered)
        self.assertIn(Role.PRINCIPAL, offered)


class StaffProvisioningIsTenantScopedTests(StaffProvisioningTestCase):
    def test_an_owner_cannot_file_an_account_under_another_school(self):
        self.client.force_login(self.owner)
        # The campus id belongs to the rival school; the form must not take it.
        response = self.client.post(
            CREATE_URL, form_data(branch=self.other_branch.id)
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(email="chidinma@example.com").exists())

    def test_the_school_comes_from_the_session_not_the_post(self):
        self.client.force_login(self.other_owner)
        # A posted school is simply not a field; whatever is sent, the account
        # lands in the caller's own school.
        self.client.post(
            CREATE_URL,
            dict(form_data(branch=self.other_branch.id), school=self.school.pk),
        )
        self.assertEqual(self.created().school_id, self.other_school.pk)

    def test_the_campus_dropdown_offers_only_this_schools_campuses(self):
        self.client.force_login(self.owner)
        response = self.client.get(CREATE_URL)
        offered = list(response.context["form"].fields["branch"].queryset)
        self.assertEqual(offered, [self.branch])

    def test_an_owner_cannot_resend_an_invite_to_another_schools_account(self):
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("staff:resend_invite", args=[self.other_owner.pk])
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(mail.outbox, [])


BULK_URL = reverse("staff:bulk_create")


def bulk_rows(*entries, prefix="form") -> dict:
    """POST data for the invite-many table, management form included."""
    data = {
        f"{prefix}-TOTAL_FORMS": str(len(entries)),
        f"{prefix}-INITIAL_FORMS": "0",
        f"{prefix}-MIN_NUM_FORMS": "0",
        f"{prefix}-MAX_NUM_FORMS": "1000",
    }
    for index, entry in enumerate(entries):
        for field in ("full_name", "email", "role", "branch"):
            data[f"{prefix}-{index}-{field}"] = entry.get(field, "")
    return data


class OwnerInvitesSeveralAtOnceTests(StaffProvisioningTestCase):
    """The table form. Same accounts, same emails, one page load.

    What matters is that nothing about an account differs from one made on the
    single form -- these tests check the batch path lands in the same place,
    not that it is a different feature.
    """

    def setUp(self):
        self.client.force_login(self.owner)

    def test_the_table_opens(self):
        self.assertEqual(self.client.get(BULK_URL).status_code, 200)

    def test_several_accounts_are_created_from_one_submission(self):
        response = self.client.post(
            BULK_URL,
            bulk_rows(
                {"full_name": "Chidinma Eze", "email": "chidinma@example.com",
                 "role": Role.BURSAR, "branch": str(self.branch.pk)},
                {"full_name": "Tunde Bello", "email": "tunde@example.com",
                 "role": Role.PRINCIPAL, "branch": str(self.branch.pk)},
            ),
        )
        self.assertRedirects(response, LIST_URL)
        self.assertEqual(self.created("chidinma@example.com").role, Role.BURSAR)
        self.assertEqual(self.created("tunde@example.com").role, Role.PRINCIPAL)

    def test_everybody_is_filed_under_the_creators_school(self):
        """The school is never taken from the form, in either shape."""
        self.client.post(
            BULK_URL,
            bulk_rows({"full_name": "Chidinma Eze", "email": "chidinma@example.com",
                       "role": Role.BURSAR, "branch": str(self.branch.pk)}),
        )
        self.assertEqual(self.created().school_id, self.school.pk)

    def test_each_person_is_emailed_a_link_and_no_password_is_set_here(self):
        self.client.post(
            BULK_URL,
            bulk_rows(
                {"full_name": "Chidinma Eze", "email": "chidinma@example.com",
                 "role": Role.BURSAR, "branch": str(self.branch.pk)},
                {"full_name": "Tunde Bello", "email": "tunde@example.com",
                 "role": Role.PRINCIPAL, "branch": str(self.branch.pk)},
            ),
        )
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(
            {to for message in mail.outbox for to in message.to},
            {"chidinma@example.com", "tunde@example.com"},
        )
        # The proprietor never learns it, so it cannot be anything they typed.
        for email in ("chidinma@example.com", "tunde@example.com"):
            self.assertTrue(self.created(email).has_usable_password())

    def test_blank_rows_are_ignored(self):
        before = User.objects.count()
        response = self.client.post(
            BULK_URL,
            bulk_rows(
                {"full_name": "Chidinma Eze", "email": "chidinma@example.com",
                 "role": Role.BURSAR, "branch": str(self.branch.pk)},
                {}, {},
            ),
        )
        self.assertRedirects(response, LIST_URL)
        self.assertEqual(User.objects.count(), before + 1)

    def test_the_same_address_twice_in_one_batch_is_a_row_error(self):
        """Nothing is created: the second insert would fail after the first
        person had already been emailed."""
        before = User.objects.count()
        response = self.client.post(
            BULK_URL,
            bulk_rows(
                {"full_name": "Chidinma Eze", "email": "chidinma@example.com",
                 "role": Role.BURSAR, "branch": str(self.branch.pk)},
                {"full_name": "Someone Else", "email": "CHIDINMA@example.com",
                 "role": Role.BURSAR, "branch": str(self.branch.pk)},
            ),
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("email", response.context["formset"].forms[1].errors)
        self.assertEqual(User.objects.count(), before)
        self.assertEqual(mail.outbox, [])

    def test_one_bad_row_writes_nothing(self):
        """One transaction, so a half-invited batch is never left behind."""
        before = User.objects.count()
        self.client.post(
            BULK_URL,
            bulk_rows(
                {"full_name": "Chidinma Eze", "email": "chidinma@example.com",
                 "role": Role.BURSAR, "branch": str(self.branch.pk)},
                # A bursar works at one campus, so a blank branch is refused.
                {"full_name": "Tunde Bello", "email": "tunde@example.com",
                 "role": Role.BURSAR, "branch": ""},
            ),
        )
        self.assertEqual(User.objects.count(), before)
        self.assertEqual(mail.outbox, [])

    def test_another_schools_campus_is_not_a_choice(self):
        before = User.objects.count()
        response = self.client.post(
            BULK_URL,
            bulk_rows({"full_name": "Chidinma Eze", "email": "chidinma@example.com",
                       "role": Role.BURSAR, "branch": str(self.other_branch.pk)}),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(User.objects.count(), before)

    def test_save_and_invite_more_comes_back_to_the_table(self):
        response = self.client.post(
            BULK_URL,
            {
                "save_and_add_another": "1",
                **bulk_rows({"full_name": "Chidinma Eze",
                             "email": "chidinma@example.com",
                             "role": Role.BURSAR, "branch": str(self.branch.pk)}),
            },
        )
        self.assertRedirects(response, BULK_URL)

    def test_a_bursar_cannot_reach_it(self):
        self.client.force_login(self.bursar)
        self.assertEqual(self.client.get(BULK_URL).status_code, 403)
        before = User.objects.count()
        self.assertEqual(
            self.client.post(
                BULK_URL,
                bulk_rows({"full_name": "Chidinma Eze",
                           "email": "chidinma@example.com",
                           "role": Role.BURSAR, "branch": str(self.branch.pk)}),
            ).status_code,
            403,
        )
        self.assertEqual(User.objects.count(), before)
