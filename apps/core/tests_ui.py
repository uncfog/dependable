"""The navigation and settings pass: what each fix must keep doing.

None of this is about how a screen looks -- a test cannot see a pulse or judge a
transition. What it can hold onto is everything underneath the visual work that
would be easy to break later:

* a person can change their own display name and job title, and cannot change
  their own role while they are at it;
* the logo is the proprietor's, in the form as well as on the page;
* "School settings" is gone from the one sidebar it 404'd in, and still present in
  the two where it works;
* the footer has no link that goes nowhere, and About is a real page in the
  sitemap;
* the platform's Users screen shows what a school's Staff screen does not, and
  deactivation locks an account out without deleting anything.

The animation work -- the toast sequence, the status-dot pulse, the collapsed rail
-- is asserted only where it has a structural footprint: the classes the
stylesheet keys off, and the stage constants the script and the stylesheet have to
agree on. A drifting duration is the failure mode those have, and it is the one a
test can catch.
"""

from __future__ import annotations

import pathlib
import re

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.core.modules import ALL_KEYS
from apps.core.navigation import nav_for
from apps.core.permissions import Capability, capabilities_for
from apps.core.roles import Role
from apps.schools.models import Branch, School

User = get_user_model()

#: A 1x1 PNG -- enough to exercise an upload without committing an image.
PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6360000002000100ffff03000006"
    "000557bfabd40000000049454e44ae426082"
)


def png(name="logo.png") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, PNG_1PX, content_type="image/png")


#: The protected account's address. The setting is environment-only, so the
#: suite names its own rather than inheriting whatever the machine has set.
PROTECTED_EMAIL = "protected@example.com"


@override_settings(PLATFORM_OWNER_EMAIL=PROTECTED_EMAIL)
class UITestCase(TestCase):
    """One school with every role, and a second school to stay out of."""

    @classmethod
    def setUpTestData(cls):
        cls.school = School.all_objects.create(name="Fulfilled Academy")
        cls.main = Branch.all_objects.create(school=cls.school, name="Main Campus")
        cls.other = School.all_objects.create(name="Rival College")
        cls.other_branch = Branch.all_objects.create(
            school=cls.other, name="West Campus"
        )

        cls.owner = User.objects.create_user(
            "owner", email="owner@example.com", password="pw",
            first_name="Ada", last_name="Owner",
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
        # The account PLATFORM_OWNER_EMAIL names, and a second platform owner who
        # is not it. `cls.platform` above is a third, also unprotected -- so the
        # tests can tell "holds the capability" apart from "is out of reach of it".
        cls.protected = User.objects.create_superuser(
            "protected", email=settings.PLATFORM_OWNER_EMAIL, password="pw",
            role=Role.PLATFORM_OWNER,
        )
        cls.stranger = User.objects.create_user(
            "stranger", email="stranger@example.com", password="pw",
            role=Role.BURSAR, school=cls.other, branch=cls.other_branch,
        )

    def labels_for(self, role) -> set[str]:
        return {
            item.label
            for section in nav_for(
                role, "/", {c.value for c in capabilities_for(role)}, ALL_KEYS
            )
            for item in section.items
        }


# ===========================================================================
# Your own name and job title
# ===========================================================================


class ProfileTests(UITestCase):
    def setUp(self):
        self.client.force_login(self.owner)
        self.url = reverse("accounts:settings")

    def test_the_page_offers_the_fields(self):
        response = self.client.get(self.url)
        self.assertContains(response, 'name="first_name"')
        self.assertContains(response, 'name="last_name"')
        self.assertContains(response, 'name="job_title"')

    def test_saving_changes_how_you_are_named(self):
        response = self.client.post(self.url, {
            "form": "profile", "first_name": "Chidi", "last_name": "Eze",
            "job_title": "Finance Officer",
        }, follow=True)
        self.assertEqual(response.status_code, 200)
        self.owner.refresh_from_db()
        self.assertEqual(self.owner.get_full_name(), "Chidi Eze")
        self.assertEqual(self.owner.job_title, "Finance Officer")

    def test_the_new_name_reaches_the_sidebar(self):
        self.client.post(self.url, {
            "form": "profile", "first_name": "Chidi", "last_name": "Eze",
            "job_title": "Finance Officer",
        })
        response = self.client.get(reverse("core:school_dashboard"))
        self.assertContains(response, "Chidi Eze")
        self.assertContains(response, "Finance Officer")

    def test_whitespace_is_tidied_rather_than_stored(self):
        self.client.post(self.url, {
            "form": "profile", "first_name": "  Chidi  ",
            "last_name": " Eze ", "job_title": "  Head   of  Maths ",
        })
        self.owner.refresh_from_db()
        self.assertEqual(self.owner.first_name, "Chidi")
        self.assertEqual(self.owner.last_name, "Eze")
        self.assertEqual(self.owner.job_title, "Head of Maths")

    def test_a_blank_name_is_refused(self):
        """The shell falls back to the username, which is derived from an email
        address and reads like one."""
        response = self.client.post(self.url, {
            "form": "profile", "first_name": "", "last_name": "", "job_title": "",
        })
        self.assertEqual(response.status_code, 200)
        self.owner.refresh_from_db()
        self.assertEqual(self.owner.first_name, "Ada")

    def test_the_job_title_may_be_emptied(self):
        """Free text, and not everybody has one."""
        self.client.post(self.url, {
            "form": "profile", "first_name": "Ada", "last_name": "Owner",
            "job_title": "",
        })
        self.owner.refresh_from_db()
        self.assertEqual(self.owner.job_title, "")

    def test_you_cannot_promote_yourself(self):
        """Role, school and campus decide what an account can see, so they are
        not on a form the account fills in for itself."""
        response = self.client.post(self.url, {
            "form": "profile", "first_name": "Ada", "last_name": "Owner",
            "job_title": "", "role": Role.PLATFORM_OWNER,
            "school": self.other.pk, "is_superuser": "on",
        }, follow=True)
        self.assertEqual(response.status_code, 200)
        self.owner.refresh_from_db()
        self.assertEqual(self.owner.role, Role.SCHOOL_OWNER)
        self.assertEqual(self.owner.school_id, self.school.pk)
        self.assertFalse(self.owner.is_superuser)

    def test_every_role_can_edit_their_own(self):
        for who in (self.owner, self.principal, self.bursar, self.platform):
            with self.subTest(user=who.username):
                self.client.force_login(who)
                response = self.client.post(self.url, {
                    "form": "profile", "first_name": "Given",
                    "last_name": "Family", "job_title": "Role",
                }, follow=True)
                self.assertEqual(response.status_code, 200)
                who.refresh_from_db()
                self.assertEqual(who.get_full_name(), "Given Family")

    def test_a_rejected_profile_does_not_wipe_the_other_forms(self):
        """Three forms on one page, told apart by the hidden `form` field."""
        response = self.client.post(self.url, {
            "form": "profile", "first_name": "", "last_name": "Eze",
        })
        self.assertContains(response, 'name="old_password"')
        self.assertContains(response, 'name="email"')


# ===========================================================================
# The school logo
# ===========================================================================


class LogoCapabilityTests(UITestCase):
    def test_only_the_proprietor_and_the_platform_hold_it(self):
        self.assertIn(
            Capability.MANAGE_SCHOOL_LOGO, capabilities_for(Role.SCHOOL_OWNER)
        )
        self.assertIn(
            Capability.MANAGE_SCHOOL_LOGO, capabilities_for(Role.PLATFORM_OWNER)
        )
        for role in (Role.PRINCIPAL, Role.BURSAR):
            with self.subTest(role=role):
                self.assertNotIn(
                    Capability.MANAGE_SCHOOL_LOGO, capabilities_for(role)
                )

    def test_a_principal_reads_the_rest_of_the_profile_but_does_not_change_it(self):
        """Pilot feedback: School settings are the owner's to change. A
        principal sees them, and edits none of them -- logo included."""
        self.assertIn(
            Capability.VIEW_SCHOOL_PROFILE, capabilities_for(Role.PRINCIPAL)
        )
        self.assertNotIn(
            Capability.MANAGE_SCHOOL_PROFILE, capabilities_for(Role.PRINCIPAL)
        )


class LogoScreenTests(UITestCase):
    def setUp(self):
        self.url = reverse("core:school_settings")

    def test_the_proprietor_is_offered_the_control(self):
        self.client.force_login(self.owner)
        response = self.client.get(self.url)
        self.assertContains(response, "Add your school logo")
        self.assertContains(response, 'class="logo-file"')

    def test_a_principal_is_told_whose_decision_it_is(self):
        self.client.force_login(self.principal)
        response = self.client.get(self.url)
        self.assertContains(response, "Only the school owner can")
        self.assertNotContains(response, 'class="logo-file"')

    def test_the_form_drops_the_field_rather_than_disabling_it(self):
        from apps.core.forms import SchoolProfileForm

        allowed = SchoolProfileForm(instance=self.school, can_manage_logo=True)
        refused = SchoolProfileForm(instance=self.school, can_manage_logo=False)
        self.assertIn("logo", allowed.fields)
        self.assertIn("remove_logo", allowed.fields)
        self.assertNotIn("logo", refused.fields)
        self.assertNotIn("remove_logo", refused.fields)

    def test_the_proprietor_can_upload_one(self):
        self.client.force_login(self.owner)
        response = self.client.post(self.url, {
            "name": "Fulfilled Academy", "contact_email": "",
            "contact_phone": "", "logo": png(),
        }, follow=True)
        self.assertEqual(response.status_code, 200)
        self.school.refresh_from_db()
        self.assertTrue(self.school.logo)

    def test_a_principal_cannot_even_by_posting_one(self):
        """The whole POST is refused before a form is built, so an upload
        posted by hand has nowhere to land."""
        self.client.force_login(self.principal)
        response = self.client.post(self.url, {
            "name": "Fulfilled Academy", "contact_email": "",
            "contact_phone": "", "logo": png(),
        })
        self.assertEqual(response.status_code, 403)
        self.school.refresh_from_db()
        self.assertFalse(self.school.logo)

    def test_a_principal_cannot_save_the_rest_either(self):
        self.client.force_login(self.principal)
        before = self.school.contact_email
        response = self.client.post(self.url, {
            "name": "Fulfilled Academy", "contact_email": "office@fa.example",
            "contact_phone": "08031234567",
        })
        self.assertEqual(response.status_code, 403)
        self.school.refresh_from_db()
        self.assertEqual(self.school.contact_email, before)

    def test_the_proprietor_can_remove_one(self):
        self.client.force_login(self.owner)
        self.client.post(self.url, {
            "name": "Fulfilled Academy", "contact_email": "",
            "contact_phone": "", "logo": png(),
        })
        self.school.refresh_from_db()
        self.assertTrue(self.school.logo)

        self.client.post(self.url, {
            "name": "Fulfilled Academy", "contact_email": "",
            "contact_phone": "", "remove_logo": "on",
        })
        self.school.refresh_from_db()
        self.assertFalse(self.school.logo)

    def test_a_new_file_wins_over_the_remove_tick(self):
        """Somebody who chose a replacement and left the box ticked meant to
        replace it -- the other reading throws away the upload."""
        self.client.force_login(self.owner)
        self.client.post(self.url, {
            "name": "Fulfilled Academy", "contact_email": "",
            "contact_phone": "", "logo": png("first.png"),
        })
        self.client.post(self.url, {
            "name": "Fulfilled Academy", "contact_email": "",
            "contact_phone": "", "logo": png("second.png"), "remove_logo": "on",
        })
        self.school.refresh_from_db()
        self.assertTrue(self.school.logo)
        self.assertIn("second", self.school.logo.name)


class LogoPromptTests(UITestCase):
    """The signpost on the dashboard, which is what people could not find."""

    def test_it_is_shown_to_the_proprietor_while_there_is_no_logo(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("core:school_dashboard"))
        self.assertContains(response, "Add your school logo")
        self.assertContains(response, f"{reverse('core:school_settings')}#logo")

    def test_it_goes_once_a_logo_is_uploaded(self):
        self.client.force_login(self.owner)
        self.client.post(reverse("core:school_settings"), {
            "name": "Fulfilled Academy", "contact_email": "",
            "contact_phone": "", "logo": png(),
        })
        response = self.client.get(reverse("core:school_dashboard"))
        self.assertNotContains(response, "Add your school logo")

    def test_a_principal_is_not_prompted_for_something_they_cannot_do(self):
        self.client.force_login(self.principal)
        response = self.client.get(reverse("core:branch_dashboard"))
        self.assertNotContains(response, "Add your school logo")


# ===========================================================================
# School settings in the sidebar
# ===========================================================================


class SchoolSettingsNavTests(UITestCase):
    def test_the_platform_owner_is_not_offered_it(self):
        self.assertNotIn("School settings", self.labels_for(Role.PLATFORM_OWNER))

    def test_the_school_side_roles_still_are(self):
        for role in (Role.SCHOOL_OWNER, Role.PRINCIPAL):
            with self.subTest(role=role):
                self.assertIn("School settings", self.labels_for(role))

    def test_a_bursar_never_was(self):
        self.assertNotIn("School settings", self.labels_for(Role.BURSAR))

    def test_the_platform_owner_reaches_a_school_profile_from_schools(self):
        self.client.force_login(self.platform)
        overview = self.client.get(reverse("core:platform_overview"))
        detail_url = reverse("core:platform_school", args=[self.school.pk])
        self.assertContains(overview, detail_url)

        detail = self.client.get(detail_url)
        self.assertContains(detail, 'name="name"')
        self.assertContains(detail, 'class="logo-file"')

    def test_the_platform_owner_can_edit_any_schools_profile(self):
        self.client.force_login(self.platform)
        response = self.client.post(
            reverse("core:platform_school_profile", args=[self.other.pk]),
            {"name": "Rival College Renamed",
             "contact_email": "office@rival.example", "contact_phone": ""},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.other.refresh_from_db()
        self.assertEqual(self.other.name, "Rival College Renamed")

    def test_a_school_owner_cannot_use_that_route(self):
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("core:platform_school_profile", args=[self.school.pk]),
            {"name": "Hacked", "contact_email": "", "contact_phone": ""},
        )
        self.assertEqual(response.status_code, 403)
        self.school.refresh_from_db()
        self.assertEqual(self.school.name, "Fulfilled Academy")

    def test_that_route_has_nothing_to_get(self):
        self.client.force_login(self.platform)
        response = self.client.get(
            reverse("core:platform_school_profile", args=[self.school.pk])
        )
        self.assertRedirects(
            response, reverse("core:platform_school", args=[self.school.pk])
        )

    def test_a_rejected_form_comes_back_on_the_whole_page(self):
        """Not a stub: the campuses and the module switches are still around it."""
        self.client.force_login(self.platform)
        response = self.client.post(
            reverse("core:platform_school_profile", args=[self.school.pk]),
            {"name": "", "contact_email": "", "contact_phone": ""},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Campuses")
        self.assertContains(response, "Features")
        self.assertContains(response, "Main Campus")


# ===========================================================================
# The footer and the About page
# ===========================================================================


class FooterTests(UITestCase):
    def body(self, url) -> str:
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, url)
        return response.content.decode()

    def test_careers_is_gone(self):
        self.assertNotIn("Careers", self.body(reverse("core:landing")))

    def test_contact_and_socials_share_the_landing_footer(self):
        from apps.core.branding import CONTACT_EMAIL

        body = self.body(reverse("core:landing"))
        footer = body[body.index("<footer"):body.index("</footer>")]
        self.assertIn("CONTACT &amp; SUPPORT", footer)
        self.assertIn(f'href="mailto:{CONTACT_EMAIL}"', footer)
        self.assertIn('aria-label="Social accounts"', footer)

    def test_about_is_linked(self):
        self.assertIn(reverse("core:about"), self.body(reverse("core:landing")))

    def test_no_footer_link_goes_nowhere(self):
        """Every href in the Company and About columns resolves. The social
        icons are the documented exception -- real links with real labels, whose
        hrefs the product's own accounts will replace."""
        body = self.body(reverse("core:landing"))
        footer = body[body.index("<footer"):]
        columns = footer[: footer.index("Social accounts")] + footer[
            footer.index("</ul>", footer.index("Social accounts")):
        ]
        dead = re.findall(r'class="footer-link"[^>]*href="#"', columns)
        dead += re.findall(r'href="#"[^>]*class="footer-link"', columns)
        self.assertEqual(dead, [])


class AboutPageTests(UITestCase):
    def test_it_is_public(self):
        response = self.client.get(reverse("core:about"))
        self.assertEqual(response.status_code, 200)

    def test_it_reads_inside_the_shell_too(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("core:about"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Run Your School. Not Your Paperwork.")

    def test_it_describes_the_product_from_the_one_source(self):
        from apps.core.branding import PRODUCT_SUBHEAD
        from apps.core.seo import FEATURES

        response = self.client.get(reverse("core:about"))
        self.assertContains(response, PRODUCT_SUBHEAD)
        for title, _ in FEATURES:
            with self.subTest(feature=title):
                self.assertContains(response, title)

    def test_it_is_in_the_sitemap_and_llms_txt(self):
        """A new public page has to be declared in apps/core/seo.py, or
        tests_seo's URLconf walk fails it as neither public nor private."""
        self.assertContains(self.client.get(reverse("sitemap")), "/about/")
        self.assertContains(self.client.get(reverse("llms_txt")), "/about/")

    def test_it_has_its_own_title_and_description(self):
        response = self.client.get(reverse("core:about"))
        self.assertContains(response, "<title>About SCHOOLCORD</title>")
        self.assertContains(response, 'name="description"')

    def test_it_links_on_to_the_privacy_policy_and_signup(self):
        response = self.client.get(reverse("core:about"))
        self.assertContains(response, reverse("core:privacy"))
        self.assertContains(response, reverse("core:signup"))


# ===========================================================================
# Staff, and Users
# ===========================================================================


class UsersScreenTests(UITestCase):
    def test_the_platform_sidebar_says_users(self):
        labels = self.labels_for(Role.PLATFORM_OWNER)
        self.assertIn("Users", labels)
        self.assertNotIn("Staff", labels)

    def test_a_schools_sidebar_still_says_staff(self):
        for role in (Role.SCHOOL_OWNER, Role.PRINCIPAL):
            with self.subTest(role=role):
                labels = self.labels_for(role)
                self.assertIn("Staff", labels)
                self.assertNotIn("Users", labels)

    def test_both_entries_point_at_the_same_screen(self):
        for role in (Role.PLATFORM_OWNER, Role.SCHOOL_OWNER):
            with self.subTest(role=role):
                hrefs = {
                    item.label: item.href
                    for section in nav_for(
                        role, "/",
                        {c.value for c in capabilities_for(role)}, ALL_KEYS,
                    )
                    for item in section.items
                }
                label = "Users" if role == Role.PLATFORM_OWNER else "Staff"
                self.assertEqual(hrefs[label], reverse("staff:list"))

    def table_headings(self, response) -> list[str]:
        """The column headings, and only those.

        Read out of the table rather than looked for in the page: the sidebar has
        a nav *section* called "School", so a bare search for the word finds it in
        both scopes and proves nothing.
        """
        body = response.content.decode()
        table = body[body.index("<table"): body.index("</thead>")]
        return [
            re.sub(r"\s+", " ", cell).strip()
            # `<th\s` and not `<th`, or the pattern matches `<thead` too and the
            # first "heading" comes back as the whole opening row.
            for cell in re.findall(r"<th\s[^>]*>(.*?)</th>", table, re.S)
        ]

    def test_the_platform_screen_shows_the_school_column(self):
        self.client.force_login(self.platform)
        response = self.client.get(reverse("staff:list"))
        self.assertIn("School", self.table_headings(response))
        self.assertTrue(response.context["is_platform"])
        # And every school's accounts, which is what the column is for.
        self.assertContains(response, "Rival College")
        self.assertContains(response, "stranger@example.com")

    def test_a_schools_screen_does_not(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("staff:list"))
        self.assertNotIn("School", self.table_headings(response))
        self.assertFalse(response.context["is_platform"])

    def test_a_school_sees_only_its_own_accounts(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("staff:list"))
        self.assertContains(response, "owner@example.com")
        self.assertNotContains(response, "stranger@example.com")

    def test_the_columns_asked_for_are_all_there(self):
        self.client.force_login(self.platform)
        headings = self.table_headings(self.client.get(reverse("staff:list")))
        for heading in ("Name", "Job title", "School", "Campus", "Role",
                        "Status", "Actions"):
            with self.subTest(column=heading):
                self.assertIn(heading, headings)

    def test_it_is_titled_for_its_scope(self):
        self.client.force_login(self.platform)
        self.assertEqual(
            self.client.get(reverse("staff:list")).context["page_title"], "Users"
        )
        self.client.force_login(self.owner)
        self.assertEqual(
            self.client.get(reverse("staff:list")).context["page_title"], "Staff"
        )

    def test_invitation_is_offered_on_both(self):
        for who in (self.platform, self.owner):
            with self.subTest(user=who.username):
                self.client.force_login(who)
                response = self.client.get(reverse("staff:list"))
                self.assertContains(response, reverse("staff:create"))
                self.assertContains(response, reverse("staff:bulk_create"))


class DeactivationTests(UITestCase):
    def activate(self, person, on: bool):
        data = {"active": "on"} if on else {}
        return self.client.post(
            reverse("staff:activation", args=[person.pk]), data, follow=True
        )

    def test_only_the_platform_holds_the_capability(self):
        self.assertIn(
            Capability.DEACTIVATE_ACCOUNTS, capabilities_for(Role.PLATFORM_OWNER)
        )
        for role in (Role.SCHOOL_OWNER, Role.PRINCIPAL, Role.BURSAR):
            with self.subTest(role=role):
                self.assertNotIn(
                    Capability.DEACTIVATE_ACCOUNTS, capabilities_for(role)
                )

    def test_the_platform_owner_can_deactivate(self):
        self.client.force_login(self.platform)
        response = self.activate(self.bursar, False)
        self.assertEqual(response.status_code, 200)
        self.bursar.refresh_from_db()
        self.assertFalse(self.bursar.is_active)

    def test_a_deactivated_account_cannot_sign_in(self):
        self.client.force_login(self.platform)
        self.activate(self.bursar, False)
        self.assertFalse(
            self.client_class().login(username="bursar@example.com", password="pw")
        )

    def test_reactivating_restores_access_with_the_same_password(self):
        self.client.force_login(self.platform)
        self.activate(self.bursar, False)
        self.activate(self.bursar, True)
        self.bursar.refresh_from_db()
        self.assertTrue(self.bursar.is_active)
        self.assertTrue(
            self.client_class().login(username="bursar@example.com", password="pw")
        )

    def test_it_deletes_nothing(self):
        self.client.force_login(self.platform)
        self.activate(self.bursar, False)
        self.assertTrue(User.objects.filter(pk=self.bursar.pk).exists())
        self.bursar.refresh_from_db()
        self.assertEqual(self.bursar.email, "bursar@example.com")
        self.assertEqual(self.bursar.school_id, self.school.pk)

    def test_a_school_owner_cannot_deactivate_anyone(self):
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("staff:activation", args=[self.bursar.pk]), {}
        )
        self.assertEqual(response.status_code, 403)
        self.bursar.refresh_from_db()
        self.assertTrue(self.bursar.is_active)

    def test_a_principal_and_a_bursar_cannot_either(self):
        for who in (self.principal, self.bursar):
            with self.subTest(user=who.username):
                self.client.force_login(who)
                self.assertEqual(
                    self.client.post(
                        reverse("staff:activation", args=[self.owner.pk]), {}
                    ).status_code,
                    403,
                )
                self.owner.refresh_from_db()
                self.assertTrue(self.owner.is_active)

    def test_you_cannot_deactivate_yourself(self):
        """A footgun rather than a permission question: switching off the account
        you are signed in as locks you out of the screen that switches it back."""
        self.client.force_login(self.platform)
        self.activate(self.platform, False)
        self.platform.refresh_from_db()
        self.assertTrue(self.platform.is_active)

    def test_the_control_is_not_drawn_against_your_own_row(self):
        self.client.force_login(self.platform)
        response = self.client.get(reverse("staff:list"))
        self.assertNotContains(
            response, reverse("staff:activation", args=[self.platform.pk])
        )
        self.assertContains(
            response, reverse("staff:activation", args=[self.bursar.pk])
        )

    def test_a_school_screen_draws_no_deactivate_control_at_all(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("staff:list"))
        self.assertNotContains(
            response, reverse("staff:activation", args=[self.bursar.pk])
        )

    def test_it_is_never_a_get(self):
        self.client.force_login(self.platform)
        response = self.client.get(
            reverse("staff:activation", args=[self.bursar.pk])
        )
        self.assertEqual(response.status_code, 405)
        self.bursar.refresh_from_db()
        self.assertTrue(self.bursar.is_active)

    def test_a_signed_out_visitor_is_sent_to_sign_in(self):
        response = self.client.post(
            reverse("staff:activation", args=[self.bursar.pk]), {}
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])
        self.bursar.refresh_from_db()
        self.assertTrue(self.bursar.is_active)

    def test_an_account_that_does_not_exist_is_a_404(self):
        self.client.force_login(self.platform)
        response = self.client.post(reverse("staff:activation", args=[99999]), {})
        self.assertEqual(response.status_code, 404)


# ===========================================================================
# The moving parts, where they have a structural footprint
# ===========================================================================


class MotionContractTests(TestCase):
    """The stylesheet and the script have to agree about the toast's stages.

    A test cannot see a transition. What it can see is the two files drifting
    apart, which is the failure mode this animation actually has: ui.js decides
    when each stage starts and app.css decides how long it takes, and a stage cut
    short is exactly the jank that was reported.
    """

    CSS = pathlib.Path(settings.BASE_DIR) / "assets" / "app.css"
    JS = pathlib.Path(settings.BASE_DIR) / "static" / "js" / "ui.js"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.css = cls.CSS.read_text(encoding="utf-8")
        cls.js = cls.JS.read_text(encoding="utf-8")

    def css_ms(self, token: str) -> int:
        match = re.search(rf"--{token}:\s*(\d+)ms", self.css)
        self.assertIsNotNone(match, f"--{token} is not declared in app.css")
        return int(match.group(1))

    def js_ms(self, name: str) -> int:
        match = re.search(rf"var {name} = (\d+);", self.js)
        self.assertIsNotNone(match, f"{name} is not declared in ui.js")
        return int(match.group(1))

    def test_each_stage_is_given_at_least_as_long_as_it_takes(self):
        for token, constant in (
            ("toast-fade", "TOAST_FADE_MS"),
            ("toast-open", "TOAST_OPEN_MS"),
            ("toast-rise", "TOAST_RISE_MS"),
        ):
            with self.subTest(stage=token):
                self.assertGreaterEqual(
                    self.js_ms(constant),
                    self.css_ms(token),
                    f"ui.js starts the next stage before --{token} has finished",
                )

    def test_the_toast_reads_rather_than_flashes(self):
        """The reported complaint was that it was too fast. Each stage is now
        slower than the design system's general-purpose 'settle'."""
        settle = int(re.search(r"--duration-settle:\s*(\d+)ms", self.css).group(1))
        for token in ("toast-rise", "toast-open"):
            with self.subTest(stage=token):
                self.assertGreater(self.css_ms(token), settle)

    def test_the_words_fade_separately_from_the_track(self):
        """The crushed-text fix. The message has its own opacity, keyed off a
        class the script adds last and removes first."""
        self.assertIn(".toast.is-lit .toast-panel > div", self.css)
        self.assertIn(".toast-panel > div", self.css)
        # Removed before the panel closes, added after it opens.
        exit_order = self.js.index("el.classList.remove('is-lit')")
        collapse = self.js.index("el.classList.remove('is-open')")
        self.assertLess(exit_order, collapse)
        entry_open = self.js.index("el.classList.add('is-open')")
        entry_lit = self.js.index("el.classList.add('is-lit')")
        self.assertLess(entry_open, entry_lit)

    def test_the_panel_no_longer_springs(self):
        """A spring overshoots, and overshooting a width makes the track spring
        past its content and snap back.

        Asserted on the declaration rather than on the block, because the block
        also carries a comment explaining which easing it is *not* using.
        """
        panel = self.css[self.css.index(".toast-panel {"):]
        panel = panel[: panel.index("}")]
        declaration = re.search(r"transition:\s*grid-template-columns[^;]*;", panel)
        self.assertIsNotNone(declaration, "the panel has no width transition")
        self.assertIn("--toast-open", declaration.group(0))
        self.assertNotIn("ease-spring", declaration.group(0))

    def test_a_server_rendered_toast_is_readable_without_the_script(self):
        template = (
            pathlib.Path(settings.BASE_DIR)
            / "templates" / "partials" / "_toast.html"
        ).read_text(encoding="utf-8")
        self.assertIn("is-in is-open is-lit", template)
        # And the script takes all three off before replaying the entrance.
        self.assertIn("remove('is-in', 'is-open', 'is-lit')", self.js)

    def test_the_status_dot_pulse_is_compositor_friendly_and_opt_out(self):
        """Transform and opacity, not box-shadow: a legend or a long table can
        have a couple of dozen dots animating at once."""
        keyframes = self.css[self.css.index("@keyframes status-pulse"):]
        keyframes = keyframes[: keyframes.index("\n}")]
        self.assertIn("transform:", keyframes)
        self.assertIn("opacity:", keyframes)
        self.assertNotIn("box-shadow", keyframes)
        # Ends transparent, so the global reduced-motion cut leaves no ring.
        self.assertRegex(keyframes, r"100%\s*\{[^}]*opacity:\s*0")
        self.assertIn(':root[data-perf="lite"] .status-dot::after', self.css)

    def test_reduced_motion_is_honoured_globally(self):
        """One opt-out the pulse and the toast both fall under."""
        self.assertIn("@media (prefers-reduced-motion: reduce)", self.css)
        self.assertIn("animation-iteration-count: 1 !important", self.css)

    def test_the_sidebar_toggle_no_longer_rotates(self):
        """It is a hamburger now, not a chevron that had to flip."""
        self.assertNotIn(
            ':root[data-sidebar="collapsed"] .sidebar-toggle svg', self.css
        )
        header = (
            pathlib.Path(settings.BASE_DIR)
            / "templates" / "partials" / "_sidebar_header.html"
        ).read_text(encoding="utf-8")
        # The three-bar path, twice -- once per toggle.
        self.assertEqual(header.count("M3.75 6.75h16.5M3.75 12h16.5m-16.5 5.25h16.5"), 2)

    def test_the_collapsed_rail_drops_its_padding_to_one_axis(self):
        """The cramped/misaligned collapsed state: the header, the nav and the
        footer each centred their glyphs on a different line because each kept
        its own expanded padding."""
        self.assertIn("--sidebar-pad", self.css)
        self.assertRegex(
            self.css,
            r':root\[data-sidebar="collapsed"\]\s*\{[^}]*--sidebar-pad:\s*0',
        )
        for name in ("_sidebar_header.html", "_nav.html", "_sidebar_footer.html"):
            path = (
                pathlib.Path(settings.BASE_DIR) / "templates" / "partials" / name
            )
            with self.subTest(template=name):
                self.assertIn("sidebar-pad", path.read_text(encoding="utf-8"))


# ===========================================================================
# One account is out of reach of deactivation
# ===========================================================================


class ProtectedAccountTests(UITestCase):
    """Every platform owner can deactivate; one account cannot be deactivated.

    Without this the platform can lock itself out of itself: two owners hold the
    same capability, they can switch each other off, and nobody is left who can
    undo it. So the capability stays with the role and one *target* is removed
    from its reach.
    """

    def activate(self, actor, target, on: bool):
        self.client.force_login(actor)
        data = {"active": "on"} if on else {}
        return self.client.post(
            reverse("staff:activation", args=[target.pk]), data, follow=True
        )

    def test_the_setting_names_an_account(self):
        self.assertEqual(settings.PLATFORM_OWNER_EMAIL, PROTECTED_EMAIL)

    def test_the_address_comes_from_the_environment_not_the_code(self):
        """Nobody's personal address is a default any more: unset means unset."""
        source = (
            pathlib.Path(settings.BASE_DIR) / "config" / "settings" / "base.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'PLATFORM_OWNER_EMAIL = os.environ.get("PLATFORM_OWNER_EMAIL", "")', source
        )
        self.assertNotIn("@gmail.com", source)

    @override_settings(PLATFORM_OWNER_EMAIL="")
    def test_unset_protects_nobody(self):
        from apps.core.permissions import is_protected_account

        for user in (self.protected, self.platform, self.owner):
            with self.subTest(user=user.username):
                self.assertFalse(is_protected_account(user))

    def test_it_recognises_that_account_and_no_other(self):
        from apps.core.permissions import is_protected_account

        self.assertTrue(is_protected_account(self.protected))
        for other in (self.platform, self.owner, self.principal, self.bursar):
            with self.subTest(user=other.username):
                self.assertFalse(is_protected_account(other))

    def test_the_match_ignores_case_and_surrounding_space(self):
        self.protected.email = f"  {settings.PLATFORM_OWNER_EMAIL.upper()}  "
        from apps.core.permissions import is_protected_account

        self.assertTrue(is_protected_account(self.protected))

    def test_another_platform_owner_still_deactivates_everyone_else(self):
        for target in (self.owner, self.principal, self.bursar, self.platform):
            with self.subTest(target=target.username):
                self.activate(self.protected, target, False)
                target.refresh_from_db()
                self.assertFalse(target.is_active)
                self.activate(self.protected, target, True)

    def test_a_platform_owner_may_deactivate_another_platform_owner(self):
        """The capability is unchanged -- it is one target that is protected, not
        a whole role."""
        self.activate(self.protected, self.platform, False)
        self.platform.refresh_from_db()
        self.assertFalse(self.platform.is_active)

    def test_nobody_may_deactivate_the_protected_account(self):
        for actor in (self.platform, self.owner, self.principal, self.bursar):
            with self.subTest(actor=actor.username):
                self.activate(actor, self.protected, False)
                self.protected.refresh_from_db()
                self.assertTrue(self.protected.is_active)

    def test_not_even_itself(self):
        """The self-deactivation guard still applies, so the top account cannot be
        locked out by the person holding it either."""
        self.activate(self.protected, self.protected, False)
        self.protected.refresh_from_db()
        self.assertTrue(self.protected.is_active)

    def test_the_refusal_says_why(self):
        response = self.activate(self.platform, self.protected, False)
        self.assertContains(response, "cannot be deactivated")

    def test_the_control_is_not_drawn_on_that_row(self):
        self.client.force_login(self.platform)
        response = self.client.get(reverse("staff:list"))
        self.assertNotContains(
            response, reverse("staff:activation", args=[self.protected.pk])
        )
        # And the row says why, rather than leaving a gap to wonder about.
        self.assertContains(response, "protected")

    def test_the_control_is_still_drawn_on_other_platform_owners(self):
        self.client.force_login(self.protected)
        response = self.client.get(reverse("staff:list"))
        self.assertContains(
            response, reverse("staff:activation", args=[self.platform.pk])
        )

    def test_may_deactivate_is_the_one_answer_both_places_use(self):
        """The template draws the button from the same function the view checks,
        so the two cannot disagree about a row."""
        from apps.core.permissions import may_deactivate

        self.assertFalse(may_deactivate(self.platform, self.protected))
        self.assertFalse(may_deactivate(self.platform, self.platform))
        self.assertFalse(may_deactivate(self.owner, self.bursar))
        self.assertTrue(may_deactivate(self.platform, self.bursar))
        self.assertTrue(may_deactivate(self.protected, self.platform))


# ===========================================================================
# The platform's own sidebar
# ===========================================================================


class PlatformSidebarTests(UITestCase):
    """A platform owner's menu is not a school's menu with extra rows on it."""

    def platform_labels(self) -> set[str]:
        return self.labels_for(Role.PLATFORM_OWNER)

    def test_admissions_is_absent(self):
        labels = self.platform_labels()
        for entry in ("Applications", "New enquiry", "Admission fees",
                      "Requirements", "Admissions settings"):
            with self.subTest(entry=entry):
                self.assertNotIn(entry, labels)

    def test_a_schools_messaging_is_absent(self):
        self.assertNotIn("Messaging", self.platform_labels())

    def test_the_sender_id_roll_is_present_and_under_platform(self):
        """Registering and approving a Sender ID is the platform's job; a school
        only reads its own. So it sits beside Schools."""
        sections = {
            section.label: [item.label for item in section.items]
            for section in nav_for(
                Role.PLATFORM_OWNER, "/",
                {c.value for c in capabilities_for(Role.PLATFORM_OWNER)}, ALL_KEYS,
            )
        }
        self.assertIn("Sender IDs", sections["Platform"])
        self.assertNotIn("Communication", sections)

    def test_a_school_keeps_both_of_its_own(self):
        for role in (Role.SCHOOL_OWNER, Role.PRINCIPAL):
            with self.subTest(role=role):
                labels = self.labels_for(role)
                self.assertIn("Messaging", labels)
                self.assertIn("Sender ID", labels)
        self.assertIn("Messaging", self.labels_for(Role.BURSAR))

    def test_a_school_keeps_its_admissions(self):
        labels = self.labels_for(Role.SCHOOL_OWNER)
        self.assertIn("Applications", labels)
        self.assertIn("Admissions settings", labels)

    def test_the_routes_are_still_reachable_for_support(self):
        """The menu is scoped, not the permission. A platform owner opening a
        school's pipeline to help them is not something to break."""
        self.client.force_login(self.platform)
        for url_name in ("admissions:pipeline", "messaging:index",
                         "messaging:identity"):
            with self.subTest(url=url_name):
                self.assertEqual(
                    self.client.get(reverse(url_name)).status_code, 200
                )

    def test_exactly_one_row_is_active_on_every_platform_screen(self):
        """The reported bug: two rows highlighted at once. Their Dashboard and
        their Schools entry both resolved to /platform/, and the tie-break
        compares href lengths, so neither was cleared."""
        caps = {c.value for c in capabilities_for(Role.PLATFORM_OWNER)}
        for path in ("/platform/", "/platform/schools/1/", "/reports/",
                     "/staff/", "/messaging/identity/", "/branches/"):
            with self.subTest(path=path):
                active = [
                    item.label
                    for section in nav_for(Role.PLATFORM_OWNER, path, caps, ALL_KEYS)
                    for item in section.items
                    if item.active
                ]
                self.assertEqual(len(active), 1, f"{path}: {active}")

    def test_the_rendered_sidebar_highlights_one_row_per_copy(self):
        """base.html renders the nav twice -- the desktop rail and the mobile
        drawer -- so the page carries one active row per copy and no more."""
        self.client.force_login(self.platform)
        body = self.client.get(reverse("core:platform_overview")).content.decode()
        self.assertEqual(body.count('aria-label="Main"'), 1)
        self.assertEqual(body.count('aria-label="Mobile"'), 1)
        self.assertEqual(body.count("nav-link-active"), 2)


# ===========================================================================
# The About page's content and links
# ===========================================================================


class AboutContentTests(UITestCase):
    def body(self) -> str:
        response = self.client.get(reverse("core:about"))
        self.assertEqual(response.status_code, 200)
        return " ".join(response.content.decode().split())

    def test_the_who_we_are_copy_is_there(self):
        body = self.body()
        for sentence in (
            "Running a school is hard enough without fees getting in the way.",
            "We replace the notebooks, the scattered spreadsheets",
            # A literal apostrophe: Django autoescapes variables, not the
            # template's own text.
            "Parents don't need to install anything.",
            "Nothing is charged until you say so.",
        ):
            with self.subTest(sentence=sentence[:40]):
                self.assertIn(sentence, body)

    def test_nothing_is_still_marked_as_a_placeholder(self):
        self.assertNotIn("data-placeholder", self.body())

    def test_the_contact_addresses_are_the_real_ones(self):
        from apps.core.branding import CONTACT_EMAIL, PRIVACY_EMAIL

        body = self.body()
        self.assertIn(f"mailto:{CONTACT_EMAIL}", body)
        # The privacy address is still named, as this page's own aside -- but it
        # is no longer what a general enquiry falls back to.
        self.assertIn(f"mailto:{PRIVACY_EMAIL}", body)

    def test_every_social_account_is_linked(self):
        """Compared against the *escaped* URL: one of these carries a query string
        with an `&` in it, which Django renders as `&amp;` in the href."""
        from django.utils.html import escape

        from apps.core.branding import SOCIAL_LINKS

        body = self.body()
        for account in SOCIAL_LINKS:
            with self.subTest(account=account["label"]):
                self.assertIn(escape(account["url"]), body)
                self.assertIn(account["handle"], body)

    def test_the_outbound_links_open_safely(self):
        """`rel="noopener noreferrer"` on anything leaving the site."""
        from django.utils.html import escape

        from apps.core.branding import SOCIAL_LINKS

        body = self.body()
        for account in SOCIAL_LINKS:
            if account["icon"] == "mail":
                continue   # a mailto: opens a client, not a tab
            with self.subTest(account=account["label"]):
                url = escape(account["url"])
                at = body.index(url)
                self.assertIn('rel="noopener noreferrer"', body[at - 250: at + 350])

    def test_it_carries_the_glass_and_reveal_treatment(self):
        """Asserted because both are load-bearing for how the page reads, and
        both degrade on their own -- glass drops its blur on low-end devices and
        the reveal is skipped under prefers-reduced-motion."""
        body = self.body()
        self.assertIn("card-glass", body)
        self.assertIn("glass-ground", body)
        self.assertIn("reveal", body)
        self.assertIn("data-reveal-group", body)


class FooterAccountTests(UITestCase):
    def footer(self) -> str:
        body = self.client.get(reverse("core:landing")).content.decode()
        return body[body.index("<footer"):]

    def test_the_three_accounts_are_linked_with_their_real_addresses(self):
        """The whole URL, share parameters included -- asserted on the escaped
        form, since one of them has an `&` in its query string."""
        from django.utils.html import escape

        from apps.core.branding import SOCIAL_LINKS

        footer = self.footer()
        for account in SOCIAL_LINKS:
            with self.subTest(account=account["label"]):
                self.assertIn(f'href="{escape(account["url"])}"', footer)

    def test_the_share_parameters_survive_into_the_href(self):
        """They are the platform's own attribution; stripping them would quietly
        drop whatever they report back."""
        footer = self.footer()
        self.assertIn("?s=11", footer)
        self.assertIn("stkn=MWdxbmx3NXhxZXNhdg%3D%3D", footer)
        self.assertIn("utm_source=qr", footer)

    def test_no_social_icon_points_at_a_placeholder(self):
        import re

        footer = self.footer()
        self.assertEqual(re.findall(r'footer-social[^>]*href="#"', footer), [])

    def test_the_accounts_we_do_not_have_are_gone(self):
        footer = self.footer().lower()
        for absent in ("linkedin", "facebook", "whatsapp"):
            with self.subTest(account=absent):
                self.assertNotIn(absent, footer)

    def test_it_is_driven_from_branding_rather_than_pasted_in(self):
        """One list, read by the footer and the About page, so an account added
        there cannot appear on one and not the other."""
        from django.utils.html import escape

        from apps.core.branding import SOCIAL_LINKS

        footer = self.footer()
        about = self.client.get(reverse("core:about")).content.decode()
        for account in SOCIAL_LINKS:
            with self.subTest(account=account["label"]):
                self.assertIn(escape(account["url"]), footer)
                self.assertIn(escape(account["url"]), about)


# ===========================================================================
# The branded email shell
# ===========================================================================


class SystemEmailTests(UITestCase):
    """The password reset, which is also the invitation -- one message.

    Checked on what actually leaves: a mail client is not going to be talked out
    of a relative image path or an SVG.
    """

    def send(self):
        from django.core import mail

        mail.outbox.clear()
        response = self.client.post(
            reverse("password_reset"), {"email": "owner@example.com"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)
        return mail.outbox[0]

    def html(self) -> str:
        message = self.send()
        self.assertEqual(len(message.alternatives), 1)
        content, mimetype = message.alternatives[0]
        self.assertEqual(mimetype, "text/html")
        return content

    def test_it_is_multipart_with_a_plain_text_body(self):
        """The HTML is the alternative; the text is the body. A client that
        refuses HTML still gets a complete message."""
        message = self.send()
        self.assertTrue(message.body.strip())
        self.assertIn("Somebody asked to set a password", message.body)
        self.assertIn("SCHOOLCORD", message.body)
        # No markup in the text half -- that is the point of it.
        self.assertNotIn("<table", message.body)
        self.assertEqual(message.alternatives[0][1], "text/html")

    def test_the_sender_name_is_the_product(self):
        self.assertIn("SCHOOLCORD", self.send().from_email)

    def test_the_subject_reads_for_an_invitation_as_well_as_a_reset(self):
        """The same message goes to a brand-new account, which has never had a
        password to reset."""
        self.assertEqual(
            self.send().subject, "Choose a new SCHOOLCORD password"
        )

    def test_the_logo_is_a_png_at_an_absolute_url(self):
        import re

        html = self.html()
        match = re.search(r'<img src="(?P<src>[^"]+)"', html)
        self.assertIsNotNone(match, "no image in the email at all")
        src = match.group("src")
        self.assertTrue(src.startswith("http"), src)
        self.assertTrue(src.endswith(".png"), src)
        self.assertIn("schoolcord-logo", src)

    def test_no_svg_reaches_a_mail_client(self):
        self.assertNotIn(".svg", self.html())

    def test_it_carries_the_brand(self):
        html = self.html()
        self.assertIn("#0f2547", html)   # the header band
        self.assertIn("#1d3f79", html)   # the button
        self.assertIn("SCHOOLCORD", html)

    def test_a_blocked_image_still_leaves_the_brand_on_the_page(self):
        """The wordmark is live text beside the logo, and the alt text names the
        product rather than saying "logo"."""
        html = self.html()
        self.assertIn('alt="SCHOOLCORD"', html)

    def test_it_reads_on_a_phone(self):
        html = self.html()
        self.assertIn("max-width:560px", html)
        self.assertIn('name="viewport"', html)
        # 16px body copy: anything smaller is zoomed by iOS Mail, which then
        # reflows the table.
        self.assertIn("font-size:16px", html)

    def test_it_uses_tables_rather_than_flexbox(self):
        html = self.html()
        self.assertIn("<table", html)
        self.assertNotIn("display:flex", html)
        self.assertNotIn("display:grid", html)

    def test_the_reset_link_is_absolute_and_in_both_halves(self):
        import re

        message = self.send()
        html = message.alternatives[0][0]
        links = set(re.findall(r'href="(https?://[^"]*/accounts/reset/[^"]+)"', html))
        self.assertTrue(links, "no absolute reset link in the HTML")
        self.assertTrue(any(link in message.body for link in links))

    def test_the_footer_gives_a_real_address_to_write_to(self):
        from apps.core.branding import CONTACT_EMAIL

        html = self.html()
        self.assertIn(CONTACT_EMAIL, html)
        self.assertIn(CONTACT_EMAIL, self.send().body)

    def test_it_never_asks_for_a_password(self):
        """Said in the message, because it is the line that makes a phishing copy
        of it easier to spot."""
        self.assertIn("never ask for your password", self.html())

    def test_the_branding_reaches_it_despite_there_being_no_request(self):
        """A mail template renders with no request, so no context processor runs.
        A blank product name here is not an error anywhere -- it just ships."""
        html = self.html()
        self.assertNotIn("Choose a new  password", html)
        self.assertNotIn("{{", html)

    def test_the_invitation_is_the_same_message(self):
        """apps/accounts/invites reuses these three templates, so there is one
        email to keep working rather than two that drift."""
        from apps.core.views import BrandedPasswordResetView

        self.assertEqual(
            BrandedPasswordResetView.html_email_template_name,
            "registration/password_reset_email_html.html",
        )
        self.assertIn("contact_email", BrandedPasswordResetView.extra_email_context)
        self.assertIn("product_name", BrandedPasswordResetView.extra_email_context)


# ===========================================================================
# Buttons in flight
# ===========================================================================


class ButtonLoadingTests(TestCase):
    """A form submit is a page load away, and on a slow connection the only thing
    that happens when you press Save is nothing.

    The behaviour itself needs a browser, so what is held here is the contract the
    behaviour rests on -- and in particular the one decision that would silently
    change what a form *does* if somebody "tidied" it.
    """

    CSS = pathlib.Path(settings.BASE_DIR) / "assets" / "app.css"
    JS = pathlib.Path(settings.BASE_DIR) / "static" / "js" / "ui.js"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.css = cls.CSS.read_text(encoding="utf-8")
        cls.js = cls.JS.read_text(encoding="utf-8")

    def handler(self) -> str:
        """The body of initButtonLoading and its helpers."""
        start = self.js.index("function initButtonLoading()")
        return self.js[start: self.js.index("function initSidebarScroll()")]

    def test_one_listener_covers_every_form(self):
        """Sign in, sign up, save, invite, record a payment -- and every form
        written after this, without anybody remembering to opt in."""
        handler = self.handler()
        self.assertIn("addEventListener('submit'", handler)
        self.assertIn("document.addEventListener", handler)

    def test_it_marks_the_button_that_was_pressed(self):
        """A form with Save and "Save and add another" must not light both."""
        self.assertIn("event.submitter", self.handler())

    def test_it_never_sets_the_disabled_attribute(self):
        """The decision that matters. Several submit buttons here carry a name and
        value the view reads -- `save_and_add_another`, `action=import` -- and a
        button disabled inside its own submit handler can be dropped from the
        payload, silently changing what the form did."""
        handler = self.handler()
        self.assertNotIn("disabled = true", handler)
        self.assertNotIn('setAttribute("disabled"', handler)
        self.assertNotIn("setAttribute('disabled'", handler)
        # What it does instead.
        self.assertIn("pointer-events: none", self.css)
        self.assertIn("aria-busy", handler)

    def test_those_buttons_really_do_carry_a_name(self):
        """Not a hypothetical: if this ever stops being true the rule above can be
        revisited, and if it stays true the rule has to stay."""
        root = pathlib.Path(settings.BASE_DIR) / "templates"
        named = [
            path.name
            for path in root.rglob("*.html")
            if re.search(r'<button[^>]*type="submit"[^>]*name=',
                         path.read_text(encoding="utf-8"), re.S)
        ]
        self.assertTrue(named, "no submit button carries a name any more")

    def test_it_never_prevents_the_submit(self):
        """If the browser is going to submit, it submits; if HTML validation
        refuses, `submit` never fires and no button is left spinning."""
        self.assertNotIn("preventDefault", self.handler())

    def test_a_restored_page_does_not_keep_spinning(self):
        """The back button, or a browser restoring a cached page."""
        handler = self.handler()
        self.assertIn("pageshow", handler)
        self.assertIn("clearAllLoading", handler)

    def test_there_is_an_opt_out(self):
        """For a submit that does not navigate, where a spinner would never stop."""
        self.assertIn("data-no-loading", self.handler())

    def test_the_spinner_replaces_the_buttons_own_glyph(self):
        """"Swap the arrow for a spinner" -- so the arrow goes."""
        self.assertIn(".btn.is-loading > svg", self.css)
        self.assertIn("display: none", self.css)
        self.assertIn("btn-spin", self.css)

    def test_it_still_reads_as_busy_without_motion(self):
        """The global reduced-motion rule freezes the ring, and a still ring is not
        a spinner -- so the button leans on what is not motion."""
        block = self.css[self.css.index("@media (prefers-reduced-motion: reduce) {\n    .btn.is-loading"):]
        self.assertIn("opacity", block[:200])

    def test_the_sign_in_and_sign_up_buttons_are_the_shape_it_expects(self):
        """Both are a <button type=submit> with an svg inside, which is what the
        CSS hides and the JS marks."""
        root = pathlib.Path(settings.BASE_DIR) / "templates"
        for path in ("registration/_panel_login.html", "core/_panel_signup.html"):
            with self.subTest(panel=path):
                source = (root / path).read_text(encoding="utf-8")
                match = re.search(
                    r'<button type="submit"[^>]*>(.*?)</button>', source, re.S
                )
                self.assertIsNotNone(match)
                self.assertIn("<svg", match.group(1))


# ===========================================================================
# The password reveal
# ===========================================================================


class PasswordRevealTests(UITestCase):
    JS = pathlib.Path(settings.BASE_DIR) / "static" / "js" / "password.js"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.js = cls.JS.read_text(encoding="utf-8")

    def test_the_reveal_is_offered_to_every_password_field(self):
        """It used to be `[autocomplete=new-password]` only, so sign-in fields had
        no eye -- and half the fields having one reads as a bug."""
        self.assertIn(
            "document.querySelectorAll('input[type=\"password\"]')", self.js
        )

    def test_generate_and_the_checklist_stay_on_the_choose_a_password_fields(self):
        """Offering to generate a password into a *sign-in* box would be offering
        to lock somebody out."""
        self.assertIn("autocomplete') === 'new-password'", self.js)
        init = self.js[self.js.index("function init()"):]
        self.assertLess(init.index("addReveal"), init.index("addGenerate"))

    def test_it_does_not_wrap_a_field_twice(self):
        self.assertIn("closest('.password-wrap')", self.js)

    def test_every_screen_with_a_password_gets_the_script(self):
        """The reveal is injected, so the script has to be on the page. These are
        the screens that have a password field on them."""
        pages = [
            (reverse("login"), None),
            (reverse("core:signup"), None),
            (reverse("accounts:settings"), self.owner),
        ]
        for url, who in pages:
            with self.subTest(url=url):
                if who:
                    self.client.force_login(who)
                response = self.client.get(url)
                body = response.content.decode()
                self.assertIn('type="password"', body)
                self.assertIn("js/password.js", body)

    def test_the_admin_add_user_form_is_covered_too(self):
        """It carries its own stylesheet for exactly this -- see
        static/css/password-controls.css."""
        css = (
            pathlib.Path(settings.BASE_DIR)
            / "static" / "css" / "password-controls.css"
        ).read_text(encoding="utf-8")
        self.assertIn(".password-reveal", css)
        self.assertIn(".password-wrap", css)


# ===========================================================================
# The toast's geometry
# ===========================================================================


class ToastGeometryTests(TestCase):
    """The reported warp. The earlier fix faded the words before the collapse,
    which stopped them *looking* crushed -- and left them still being laid out,
    so the message went on reflowing into a narrow column, the toast grew tall to
    fit it, and the pill distorted anyway."""

    CSS = pathlib.Path(settings.BASE_DIR) / "assets" / "app.css"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.css = cls.CSS.read_text(encoding="utf-8")

    def rule(self, selector: str) -> str:
        start = self.css.index(selector + " {")
        return self.css[start: self.css.index("}", start)]

    def test_the_icon_cannot_be_resized_by_the_row(self):
        block = self.rule(".toast-icon")
        self.assertIn("flex: 0 0 3rem", block)
        self.assertIn("width: 3rem", block)
        self.assertIn("height: 3rem", block)
        self.assertIn("aspect-ratio: 1", block)

    def test_the_glyph_inside_it_is_pinned_too(self):
        """An <svg> is a replaced element: give it a flex context and no explicit
        size and it will take the one it is handed."""
        block = self.rule(".toast-icon > svg")
        self.assertIn("flex: none", block)
        self.assertIn("width: 1.5rem", block)
        self.assertIn("height: 1.5rem", block)

    def test_the_close_button_is_pinned(self):
        block = self.rule(".toast-close")
        self.assertIn("flex: none", block)

    def test_the_message_does_not_reflow_while_the_track_moves(self):
        block = self.rule(".toast-panel > div")
        self.assertIn("white-space: nowrap", block)

    def test_and_wraps_again_once_it_is_open(self):
        """A long message still gets two lines -- just not while the width is
        animating."""
        block = self.rule(".toast.is-lit .toast-panel > div")
        self.assertIn("white-space: normal", block)

    def test_the_fade_still_comes_before_the_collapse(self):
        """The earlier half of the fix, which is still load-bearing."""
        js = (
            pathlib.Path(settings.BASE_DIR) / "static" / "js" / "ui.js"
        ).read_text(encoding="utf-8")
        self.assertLess(
            js.index("el.classList.remove('is-lit')"),
            js.index("el.classList.remove('is-open')"),
        )


# ===========================================================================
# The collapsed rail
# ===========================================================================


class CollapsedRailTests(TestCase):
    CSS = pathlib.Path(settings.BASE_DIR) / "assets" / "app.css"
    TEMPLATES = pathlib.Path(settings.BASE_DIR) / "templates" / "partials"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.css = cls.CSS.read_text(encoding="utf-8")

    def test_the_headers_vertical_spacing_is_controlled_from_css(self):
        """It was `pt-5 pb-4` in the markup, which is a utility -- a later layer
        than these rules, so the collapsed rail could not tighten it."""
        self.assertIn(".sidebar-head", self.css)
        header = (self.TEMPLATES / "_sidebar_header.html").read_text(encoding="utf-8")
        self.assertIn("sidebar-head", header)
        self.assertNotIn("sidebar-pad pt-5 pb-4", header)

    def test_the_gap_above_the_first_glyph_is_tighter_when_collapsed(self):
        collapsed = self.css[
            self.css.index(':root[data-sidebar="collapsed"] .sidebar-head'):
        ]
        block = collapsed[: collapsed.index("}")]
        self.assertIn("padding-bottom: 0.25rem", block)

    def test_the_hamburger_row_sits_close_to_the_icons(self):
        collapsed = self.css[
            self.css.index(':root[data-sidebar="collapsed"] .sidebar-collapsed-only'):
        ]
        block = collapsed[: collapsed.index("}")]
        self.assertIn("margin-top: 0.35rem", block)

    def test_the_tenant_cards_margin_is_controlled_too(self):
        header = (self.TEMPLATES / "_sidebar_header.html").read_text(encoding="utf-8")
        self.assertNotIn("sidebar-card mt-5", header)
        self.assertIn(":root[data-sidebar=\"collapsed\"] .sidebar-card", self.css)

    def test_no_layout_utility_is_left_for_the_collapsed_rail_to_undo(self):
        """`flex-1`, `ml-auto` and `mt-*` in the markup sit in a later layer than
        these rules and win whatever their specificity -- which is how folded
        labels went on expanding and pushing glyphs off centre."""
        for name in ("_sidebar_header.html", "_sidebar_footer.html"):
            source = (self.TEMPLATES / name).read_text(encoding="utf-8")
            # Comments explain the history; only real class attributes count.
            attrs = " ".join(re.findall(r'class="([^"]*)"', source))
            with self.subTest(template=name):
                for utility in ("ml-auto", "flex-1", "mt-5", "mt-3"):
                    self.assertNotIn(utility, attrs.split(), f"{name}: {utility}")
