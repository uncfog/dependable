"""The fix/bugs3 batch, and the invariants each fix now rests on.

Five of the seven are bugs with a shared shape -- something was configured or
wired in a way that failed quietly -- so most of what is here is the assertion that
would have caught it:

* **step 4 sent a proprietor to the Django admin login.** The third time this class
  of bug has appeared (Branches, Staff and Terms were the first), so it is tested
  the way ``tests_role_access`` tests those: follow the link a person would click
  and assert on what comes back;
* **a school could invite another owner**, which would have handed over the only
  role that can invite and the only one that can switch an account off;
* **the platform's Dashboard and Schools shared a URL**, so both sidebar rows lit;
* **uploads went to the container's disk**, which nothing serves in production and
  which does not survive a restart -- the cause of both the 404s and of the logo
  that "worked on the school side";
* **the welcome email failed silently**, because best effort was implemented as
  best effort with no way to find out.

The date picker is the one feature rather than a fix, and what is testable about it
is that it reaches the fields it is for and leaves the real input alone.
"""

from __future__ import annotations

import pathlib
import re

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.academics.models import Class, Level
from apps.core.modules import ALL_KEYS
from apps.core.navigation import nav_for
from apps.core.permissions import Capability, capabilities_for
from apps.core.roles import Role
from apps.schools.models import Branch, School, SchoolModule

User = get_user_model()

PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6360000002000100ffff03000006"
    "000557bfabd40000000049454e44ae426082"
)


class BatchTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = School.all_objects.create(name="Fulfilled Academy")
        cls.main = Branch.all_objects.create(school=cls.school, name="Main Campus")
        cls.other = School.all_objects.create(name="Rival College")
        Branch.all_objects.create(school=cls.other, name="West Campus")

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

    def labels_for(self, role) -> set[str]:
        return {
            item.label
            for section in nav_for(
                role, "/", {c.value for c in capabilities_for(role)}, ALL_KEYS
            )
            for item in section.items
        }


# ===========================================================================
# 1. Step 4 stops asking the owner to authorise their own setup
# ===========================================================================


class OnboardingStepFourTests(BatchTestCase):
    """The reported bug: finishing your own school's setup asked you to log in.

    Same class as the Branches/Staff/Terms bug before it. A proprietor's account is
    deliberately not ``is_staff``, so any admin URL offered to them is a login form
    on a site they are already signed in to -- and letting them into the admin
    would have been worse, because it is not tenant-scoped.
    """

    def setUp(self):
        self.client.force_login(self.owner)

    def steps(self):
        from apps.core.tenancy import TenantContext, activate, deactivate
        from apps.core.views import onboarding_steps

        request = self.client.get(reverse("core:onboarding")).wsgi_request
        request.user = self.owner
        token = activate(TenantContext.from_user(self.owner))
        try:
            return {step["key"]: step for step in onboarding_steps(request)}
        finally:
            deactivate(token)

    def test_no_step_points_at_the_django_admin(self):
        for key, step in self.steps().items():
            with self.subTest(step=key):
                self.assertFalse(
                    step["url"].startswith("/admin/"),
                    f"{key} points at {step['url']}",
                )

    def test_the_staff_step_points_at_the_schools_own_screen(self):
        self.assertEqual(self.steps()["staff"]["url"], reverse("staff:list"))

    def test_the_onboarding_page_offers_no_admin_link(self):
        body = self.client.get(reverse("core:onboarding")).content.decode()
        self.assertNotIn('href="/admin/', body)

    def test_the_owner_can_follow_every_step_of_their_own_setup(self):
        """The whole point. Each one answers 200, not a redirect to a login."""
        for key, step in self.steps().items():
            with self.subTest(step=key):
                response = self.client.get(step["url"])
                self.assertEqual(response.status_code, 200, step["url"])

    def test_a_refusal_here_would_be_a_toast_and_not_a_login_form(self):
        """A role that may not invite gets the 403 page -- which carries a toast
        -- rather than being bounced to authorise."""
        self.client.force_login(self.bursar)
        response = self.client.get(reverse("staff:list"))
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("/accounts/login/", response.get("Location", ""))
        # The refusal page is the shell with a message in it, not an auth screen.
        body = response.content.decode()
        self.assertNotIn('name="password"', body)


# ===========================================================================
# 2 and 3. Who may invite, and as what
# ===========================================================================


class StaffInviteTests(BatchTestCase):
    def test_only_the_owner_and_the_platform_may_invite(self):
        self.assertIn(Capability.MANAGE_STAFF, capabilities_for(Role.SCHOOL_OWNER))
        self.assertIn(Capability.MANAGE_STAFF, capabilities_for(Role.PLATFORM_OWNER))
        for role in (Role.PRINCIPAL, Role.BURSAR):
            with self.subTest(role=role):
                self.assertNotIn(
                    Capability.MANAGE_STAFF, capabilities_for(role)
                )

    def test_the_invite_screens_agree_with_that(self):
        for who, allowed in (
            (self.owner, True), (self.platform, True),
            (self.principal, False), (self.bursar, False),
        ):
            with self.subTest(user=who.username):
                self.client.force_login(who)
                for url_name in ("staff:create", "staff:bulk_create"):
                    status = self.client.get(reverse(url_name)).status_code
                    self.assertEqual(status, 200 if allowed else 403, url_name)

    def test_a_principal_still_reads_the_list(self):
        """They need to know who works there; they do not get to add anybody."""
        self.client.force_login(self.principal)
        self.assertEqual(self.client.get(reverse("staff:list")).status_code, 200)

    def test_the_sidebar_offers_no_invite_a_principal_cannot_use(self):
        self.client.force_login(self.principal)
        body = self.client.get(reverse("staff:list")).content.decode()
        self.assertNotIn(reverse("staff:create"), body)
        self.assertNotIn(reverse("staff:bulk_create"), body)

    # -- the role dropdown -------------------------------------------------

    def form(self, **data):
        from apps.accounts.forms import StaffAccountForm

        return StaffAccountForm(data=data or None, creator=self.owner)

    def test_school_owner_is_not_an_option(self):
        offered = {value for value, _ in self.form().fields["role"].choices}
        self.assertNotIn(Role.SCHOOL_OWNER, offered)
        self.assertNotIn(Role.PLATFORM_OWNER, offered)
        self.assertEqual(offered, {Role.PRINCIPAL, Role.BURSAR})

    def test_nor_is_it_accepted_from_a_hand_made_post(self):
        """A `<select>` is a suggestion; a POST is not."""
        form = self.form(
            full_name="Sneaky Owner", email="sneaky@example.com",
            role=Role.SCHOOL_OWNER, branch="", job_title="", phone="",
        )
        self.assertFalse(form.is_valid())
        self.assertIn("role", form.errors)

    def test_nor_is_platform_owner(self):
        form = self.form(
            full_name="Sneaky Platform", email="sneaky2@example.com",
            role=Role.PLATFORM_OWNER, branch="", job_title="", phone="",
        )
        self.assertFalse(form.is_valid())
        self.assertIn("role", form.errors)

    def test_the_screen_does_not_render_the_option_either(self):
        self.client.force_login(self.owner)
        body = self.client.get(reverse("staff:create")).content.decode()
        select = body[body.index('name="role"'):]
        select = select[: select.index("</select>")]
        self.assertIn("Bursar", select)
        self.assertNotIn("School Owner", select)

    def test_posting_it_through_the_screen_creates_nobody(self):
        self.client.force_login(self.owner)
        before = User.objects.count()
        response = self.client.post(reverse("staff:create"), {
            "full_name": "Sneaky Owner", "email": "sneaky3@example.com",
            "role": Role.SCHOOL_OWNER, "branch": "", "job_title": "", "phone": "",
        })
        self.assertEqual(response.status_code, 200)   # redisplayed with errors
        self.assertEqual(User.objects.count(), before)

    def test_a_principal_or_bursar_can_still_be_invited(self):
        self.client.force_login(self.owner)
        response = self.client.post(reverse("staff:create"), {
            "full_name": "New Bursar", "email": "newbursar@example.com",
            "role": Role.BURSAR, "branch": self.main.pk,
            "job_title": "Finance Officer", "phone": "",
        })
        self.assertEqual(response.status_code, 302)
        created = User.objects.get(email="newbursar@example.com")
        self.assertEqual(created.role, Role.BURSAR)
        self.assertEqual(created.school_id, self.school.pk)


# ===========================================================================
# 6. The platform Dashboard is back, and nothing lights twice
# ===========================================================================


class PlatformDashboardTests(BatchTestCase):
    def setUp(self):
        self.client.force_login(self.platform)

    def test_the_dashboard_is_back_in_their_sidebar(self):
        labels = self.labels_for(Role.PLATFORM_OWNER)
        self.assertIn("Dashboard", labels)
        self.assertIn("Schools", labels)

    def test_they_are_two_different_screens(self):
        """Which is what makes having both rows possible: they shared a URL, and
        the tie-break compares href lengths, so a tie cleared neither."""
        self.assertNotEqual(
            reverse("core:platform_overview"), reverse("core:platform_schools")
        )

    def test_both_open(self):
        for url_name in ("core:platform_overview", "core:platform_schools"):
            with self.subTest(screen=url_name):
                self.assertEqual(
                    self.client.get(reverse(url_name)).status_code, 200
                )

    def test_signing_in_still_lands_on_the_dashboard(self):
        from apps.core.navigation import home_url_for

        self.assertEqual(
            home_url_for(self.platform), reverse("core:platform_overview")
        )

    def test_exactly_one_row_is_active_on_every_platform_screen(self):
        caps = {c.value for c in capabilities_for(Role.PLATFORM_OWNER)}
        paths = (
            reverse("core:platform_overview"),
            reverse("core:platform_schools"),
            reverse("core:platform_school", args=[self.school.pk]),
            reverse("reports:index"),
            reverse("staff:list"),
        )
        for path in paths:
            with self.subTest(path=path):
                active = [
                    item.label
                    for section in nav_for(Role.PLATFORM_OWNER, path, caps, ALL_KEYS)
                    for item in section.items
                    if item.active
                ]
                self.assertEqual(len(active), 1, f"{path}: {active}")

    def test_the_right_row_is_the_active_one(self):
        caps = {c.value for c in capabilities_for(Role.PLATFORM_OWNER)}
        for path, expected in (
            (reverse("core:platform_overview"), "Dashboard"),
            (reverse("core:platform_schools"), "Schools"),
            (reverse("core:platform_school", args=[self.school.pk]), "Schools"),
        ):
            with self.subTest(path=path):
                active = [
                    item.label
                    for section in nav_for(Role.PLATFORM_OWNER, path, caps, ALL_KEYS)
                    for item in section.items
                    if item.active
                ]
                self.assertEqual(active, [expected])

    def test_the_dashboard_surfaces_what_needs_a_look(self):
        """A grid of cards cannot tell you which school has no current term."""
        response = self.client.get(reverse("core:platform_overview"))
        rows = {r["school"].name: r for r in response.context["needs_attention"]}
        # Neither fixture school has a term or any students.
        self.assertIn("Fulfilled Academy", rows)
        self.assertEqual(rows["Fulfilled Academy"]["campuses_without_a_term"], 1)
        self.assertTrue(rows["Fulfilled Academy"]["has_no_students"])

    def test_a_school_that_is_set_up_drops_off_that_list(self):
        from apps.fees.models import Term, TermSequence
        from apps.students.models import Sex, Student

        Term.all_objects.create(
            school=self.school, branch=self.main, name="First Term 2026/2027",
            academic_year="2026/2027", sequence=TermSequence.FIRST, is_current=True,
        )
        klass = Class.all_objects.create(
            school=self.school, branch=self.main, name="Primary 1",
            level=Level.PRIMARY, year_in_level=1,
        )
        Student.all_objects.create(
            school=self.school, branch=self.main, school_class=klass,
            admission_number="FA/001", first_name="Ada", last_name="Nwosu",
            sex=Sex.choices[0][0], parent_name="Parent",
            parent_phone="08030000000",
        )
        response = self.client.get(reverse("core:platform_overview"))
        names = {r["school"].name for r in response.context["needs_attention"]}
        self.assertNotIn("Fulfilled Academy", names)

    def test_the_grid_still_lists_every_school_with_its_modules(self):
        response = self.client.get(reverse("core:platform_schools"))
        by_name = {c["school"].name: c for c in response.context["cards"]}
        self.assertEqual(set(by_name), {"Fulfilled Academy", "Rival College"})
        keys = {m["key"] for m in by_name["Fulfilled Academy"]["modules"]}
        self.assertEqual(keys, {"messaging", "admissions", "families"})

    def test_no_school_role_can_reach_either(self):
        for who in (self.owner, self.principal, self.bursar):
            with self.subTest(user=who.username):
                self.client.force_login(who)
                for url_name in ("core:platform_overview", "core:platform_schools"):
                    response = self.client.get(reverse(url_name))
                    self.assertEqual(response.status_code, 302)


# ===========================================================================
# 4 and 8. Uploads, and the logo that "worked on the school side"
# ===========================================================================


class UploadStorageTests(BatchTestCase):
    """One bug, reported twice.

    The logo rendered for the school and not for the platform, and uploads 404'd in
    production -- and both were the same thing: nothing served ``/media/``.
    ``config/urls.py`` routes it only when DEBUG is on, whitenoise covers
    ``static/`` by design, and the container's disk does not survive a restart
    either. The templates were never wrong.
    """

    def upload_a_logo(self):
        self.client.force_login(self.owner)
        response = self.client.post(reverse("core:school_settings"), {
            "name": "Fulfilled Academy", "contact_email": "", "contact_phone": "",
            "logo": SimpleUploadedFile("crest.png", PNG_1PX, content_type="image/png"),
        })
        self.assertEqual(response.status_code, 302)
        self.school.refresh_from_db()
        self.assertTrue(self.school.logo)
        # The app's own gated URL, never the storage one -- see apps/core/files.py.
        return self.school.logo_url

    def test_the_logo_renders_on_every_screen_that_references_it(self):
        """Including the platform ones -- which is the half that was reported
        missing, and which was never a template problem."""
        url = self.upload_a_logo()
        screens = (
            (self.owner, reverse("core:school_settings")),
            (self.owner, reverse("core:school_dashboard")),
            (self.platform, reverse("core:platform_schools")),
            (self.platform, reverse("core:platform_overview")),
            (self.platform, reverse("core:platform_school", args=[self.school.pk])),
        )
        for who, path in screens:
            with self.subTest(screen=path):
                self.client.force_login(who)
                self.assertContains(self.client.get(path), url)

    def test_the_default_backend_is_the_filesystem_without_a_bucket(self):
        """So nobody needs credentials to run this project or its tests."""
        self.assertFalse(settings.USE_OBJECT_STORAGE)
        self.assertEqual(
            settings.STORAGES["default"]["BACKEND"],
            "django.core.files.storage.FileSystemStorage",
        )

    def test_static_files_are_left_on_whitenoise(self):
        """Only uploads move. Static files are built at deploy time, versioned by
        the manifest, and whitenoise does that job well."""
        self.assertIn("staticfiles", settings.STORAGES["staticfiles"]["BACKEND"])
        self.assertNotIn("storages", settings.STORAGES["staticfiles"]["BACKEND"])

    def test_the_settings_switch_on_a_bucket_name_alone(self):
        """One variable decides it, so a half-configured deployment cannot end up
        writing to the bucket with no credentials."""
        source = (
            pathlib.Path(settings.BASE_DIR) / "config" / "settings" / "base.py"
        ).read_text(encoding="utf-8")
        self.assertIn('AWS_STORAGE_BUCKET_NAME = os.environ.get("MEDIA_BUCKET_NAME"', source)
        self.assertIn("USE_OBJECT_STORAGE = bool(AWS_STORAGE_BUCKET_NAME)", source)

    def test_supabase_needs_path_addressing_and_no_acl(self):
        """Two settings boto3 would otherwise get wrong: it defaults to
        virtual-host URLs, which is not how Supabase routes, and it sends an ACL
        header, which Supabase rejects."""
        source = (
            pathlib.Path(settings.BASE_DIR) / "config" / "settings" / "base.py"
        ).read_text(encoding="utf-8")
        self.assertIn('AWS_S3_ADDRESSING_STYLE = "path"', source)
        self.assertIn("AWS_DEFAULT_ACL = None", source)
        self.assertIn("AWS_S3_FILE_OVERWRITE = False", source)

    def test_dev_settings_no_longer_clobber_the_media_backend(self):
        """`dev.py` replaced the whole STORAGES dict, which pinned uploads to the
        filesystem even on a machine with a bucket configured -- so the one way to
        check the production path before deploying it silently did not work."""
        source = (
            pathlib.Path(settings.BASE_DIR) / "config" / "settings" / "dev.py"
        ).read_text(encoding="utf-8")
        self.assertIn("**STORAGES", source)

    def test_a_receipt_upload_uses_the_same_storage(self):
        """Both upload flows go through `default_storage`, so configuring the
        bucket moves both and neither needs its own switch."""
        from apps.payments.models import Payment

        field = Payment._meta.get_field("receipt")
        self.assertEqual(field.storage, School._meta.get_field("logo").storage)


class DeploymentCheckTests(TestCase):
    """The checks that turn a silent misconfiguration into a deploy-time warning.

    Every one of them exists because something was wrong and nothing said so.
    """

    def run_checks(self, tags=None):
        from django.core.checks import run_checks

        return {w.id for w in run_checks(tags=tags)}

    @override_settings(DEBUG=True)
    def test_they_stay_quiet_in_development(self):
        """A developer's machine has all of these "problems" by design."""
        self.assertEqual(
            {w for w in self.run_checks() if w.startswith("schoolcord.")}, set()
        )

    @override_settings(DEBUG=False, PUBLIC_BASE_URL="")
    def test_mail_with_no_public_address_is_reported(self):
        self.assertIn("schoolcord.W001", self.run_checks())

    @override_settings(
        DEBUG=False,
        EMAIL_BACKEND="anymail.backends.resend.EmailBackend",
        ANYMAIL={"RESEND_API_KEY": ""},
    )
    def test_anymail_with_no_key_is_reported(self):
        self.assertIn("schoolcord.W002", self.run_checks())

    @override_settings(DEBUG=False, USE_OBJECT_STORAGE=False)
    def test_uploads_on_the_local_disk_are_reported(self):
        """The 404 that took two rounds to find now announces itself on deploy."""
        self.assertIn("schoolcord.W004", self.run_checks())

    @override_settings(DEBUG=False, USE_OBJECT_STORAGE=True)
    def test_and_not_reported_once_a_bucket_is_configured(self):
        self.assertNotIn("schoolcord.W004", self.run_checks())

    @override_settings(DEBUG=False, PLATFORM_OWNER_EMAIL="")
    def test_no_protected_account_is_reported(self):
        self.assertIn("schoolcord.W005", self.run_checks())

    @override_settings(DEBUG=False, PLATFORM_OWNER_EMAIL="owner@example.com")
    def test_and_not_reported_once_one_is_named(self):
        self.assertNotIn("schoolcord.W005", self.run_checks())

    @override_settings(DEBUG=False, MESSAGING_PROVIDER="console")
    def test_console_messaging_in_production_is_reported(self):
        """It marks texts delivered and sends nothing, so a school would be misled."""
        self.assertIn("schoolcord.W006", self.run_checks())

    @override_settings(DEBUG=False, MESSAGING_PROVIDER="")
    def test_and_not_reported_once_real_gateways_are_in_use(self):
        self.assertNotIn("schoolcord.W006", self.run_checks())


class MailcheckCommandTests(TestCase):
    """The command that answers "why didn't it arrive?" from the server."""

    def run_command(self, *args):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command("mailcheck", *args, stdout=out, stderr=out)
        return out.getvalue()

    def test_it_reports_the_configuration_without_sending(self):
        from django.core import mail

        mail.outbox.clear()
        output = self.run_command()
        self.assertIn("EMAIL_BACKEND", output)
        self.assertIn("PUBLIC_BASE_URL", output)
        self.assertIn("DEFAULT_FROM_EMAIL", output)
        self.assertEqual(mail.outbox, [])

    @override_settings(PUBLIC_BASE_URL="")
    def test_it_names_the_commonest_cause(self):
        output = self.run_command()
        self.assertIn("PUBLIC_BASE_URL", output)
        self.assertIn("skipped entirely", output)

    def test_it_can_send_a_test_message(self):
        from django.core import mail

        mail.outbox.clear()
        self.run_command("--to", "someone@example.com")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("mail check", mail.outbox[0].subject.lower())

    @override_settings(PUBLIC_BASE_URL="https://www.theschoolcord.com")
    def test_it_can_send_the_real_welcome_email(self):
        """So what lands in the inbox is the message a new owner would get,
        template and logo and all -- and nothing is left in the database."""
        from django.core import mail

        mail.outbox.clear()
        before = School.all_objects.count()
        self.run_command("--to", "someone@example.com", "--welcome")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Welcome", mail.outbox[0].subject)
        self.assertIn("schoolcord-logo", mail.outbox[0].alternatives[0][0])
        self.assertEqual(School.all_objects.count(), before)

    @override_settings(PUBLIC_BASE_URL="")
    def test_it_fails_loudly_when_the_welcome_would_be_skipped(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            self.run_command("--to", "someone@example.com", "--welcome")


# ===========================================================================
# 7. The date picker
# ===========================================================================


class DatePickerTests(BatchTestCase):
    JS = pathlib.Path(settings.BASE_DIR) / "static" / "js" / "datepicker.js"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.js = cls.JS.read_text(encoding="utf-8")

    def test_it_reaches_every_screen_with_a_date_on_it(self):
        self.client.force_login(self.owner)
        Class.all_objects.create(
            school=self.school, branch=self.main, name="Primary 1",
            level=Level.PRIMARY, year_in_level=1,
        )
        screens = (
            (self.owner, reverse("students:student_create")),
            (self.owner, reverse("fees:term_create")),
            (self.bursar, reverse("payments:record")),
            (self.bursar, reverse("payments:index")),
        )
        for who, path in screens:
            with self.subTest(screen=path):
                self.client.force_login(who)
                body = self.client.get(path).content.decode()
                self.assertIn('type="date"', body)
                self.assertIn("js/datepicker.js", body)

    def test_it_reaches_the_public_enquiry_page_too(self):
        """The date of birth a parent is asked for is the field the native picker
        is worst at, and that audience is least likely to persevere."""
        Class.all_objects.create(
            school=self.school, branch=self.main, name="Primary 1",
            level=Level.PRIMARY, year_in_level=1,
        )
        SchoolModule.set_state(self.school, "admissions", True)
        body = self.client.get(
            reverse("admissions_public:public_enquiry", args=[self.school.slug])
        ).content.decode()
        self.assertIn('type="date"', body)
        self.assertIn("js/datepicker.js", body)

    def test_the_real_input_keeps_its_type_name_and_value(self):
        """It enhances the field, it does not replace it. Switching the type to
        `text` would cost the browser's own validation and its mobile keyboard."""
        self.assertNotIn("type = 'text'", self.js)
        self.assertNotIn('type="text"', self.js)
        self.assertIn("this.input.value = iso(date)", self.js)

    def test_it_tells_the_page_the_value_changed(self):
        """The payment filters and the fee preview listen for it, so a pick has to
        look like a real edit."""
        self.assertIn("new Event('input'", self.js)
        self.assertIn("new Event('change'", self.js)

    def test_it_bows_out_on_touch(self):
        """A phone's own date wheel is better than any popover, it is what people
        there expect, and it does not take the viewport."""
        self.assertIn("any-pointer: fine", self.js)

    def test_there_is_an_opt_out(self):
        self.assertIn("data-no-datepicker", self.js)

    def test_the_fast_way_back_exists(self):
        """The complaint was about getting *back*: a date of birth is years away
        and the native picker steps a month at a time. Day -> month -> year is two
        presses to cross a decade."""
        self.assertIn("VIEW_MONTHS", self.js)
        self.assertIn("VIEW_YEARS", self.js)
        self.assertIn("renderMonths", self.js)
        self.assertIn("renderYears", self.js)
        # The header label is the control that zooms out.
        self.assertIn("dp-zoom", self.js)

    def test_it_honours_the_fields_own_range(self):
        """A date of birth with `max` last year must not offer next month."""
        for name in ("outOfRange", "monthOutOfRange", "yearOutOfRange"):
            with self.subTest(guard=name):
                self.assertIn(name, self.js)

    def test_dates_are_built_from_parts_rather_than_parsed(self):
        """`new Date('2026-10-03')` parses as UTC and comes back as the 2nd in a
        negative offset, which is how an off-by-one-day bug gets into a birthday."""
        self.assertIn("new Date(+match[1], +match[2] - 1, +match[3])", self.js)
        # Not *called* -- the comment above that line names it to say why it is
        # avoided, so the bare word is in the file on purpose.
        self.assertNotIn(".toISOString(", self.js)

    def test_the_grid_is_a_table_a_screen_reader_can_read(self):
        self.assertIn("role', 'grid'", self.js)
        self.assertIn("aria-label", self.js)

    def test_it_supports_the_keyboard(self):
        for key in ("ArrowLeft", "ArrowRight", "PageUp", "PageDown", "Home", "End"):
            with self.subTest(key=key):
                self.assertIn(key, self.js)

    def test_its_animation_is_cut_by_the_global_reduced_motion_rule(self):
        css = (
            pathlib.Path(settings.BASE_DIR) / "assets" / "app.css"
        ).read_text(encoding="utf-8")
        self.assertIn("@keyframes dp-in", css)
        # The one global opt-out, which reaches any animation by name.
        self.assertIn("animation-duration: 0.01ms !important", css)

    def test_it_is_styled_from_tokens_so_both_themes_work(self):
        css = (
            pathlib.Path(settings.BASE_DIR) / "assets" / "app.css"
        ).read_text(encoding="utf-8")
        panel = css[css.index(".dp-panel {"):]
        panel = panel[: panel.index("}")]
        self.assertIn("bg-surface", panel)
        self.assertIn("border-ink-200", panel)
        # No literal colour anywhere in the picker's styling.
        picker = css[css.index(".dp-wrap {"): css.index("--- The guided tour")]
        self.assertNotRegex(picker, r"#[0-9a-fA-F]{6}")
