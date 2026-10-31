"""Settings shared by every environment.

Environment-specific modules (``dev``, ``prod``) import ``*`` from here and
override only what genuinely differs.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Imported rather than repeated: the product name belongs in exactly one file,
# and settings is not it. Safe at import time -- branding.py imports nothing.
from apps.core.branding import PRODUCT_NAME, SUPPORT_EMAIL

BASE_DIR = Path(__file__).resolve().parents[2]

load_dotenv(BASE_DIR / ".env")


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


#: The development fallback. Production refuses to start on it -- see
#: ``is_placeholder_secret_key`` and config/settings/prod.py.
DEV_SECRET_KEY = "dev-insecure-change-me"

SECRET_KEY = os.environ.get("SECRET_KEY", DEV_SECRET_KEY)


def is_placeholder_secret_key(value: str | None) -> bool:
    """Whether ``value`` is no secret at all: missing, blank, or a known stand-in.

    ``django-insecure-`` is the prefix ``startproject`` puts on the key it
    generates, which is as public as the dev fallback above.
    """
    key = (value or "").strip()
    return not key or key == DEV_SECRET_KEY or key.startswith("django-insecure-")


#: The account that may never be deactivated, by email address.
#:
#: Deactivation is a platform capability, so every platform owner holds it -- and
#: that includes being able to switch off another platform owner. One account has
#: to be out of reach or the platform can be locked out of itself: two owners can
#: disable each other, and nobody is left who can undo it.
#:
#: Matched case-insensitively against ``user.email``. Read from the environment
#: only -- an address belongs to whoever runs the platform, not to the code -- so
#: unset means no account is protected, and ``schoolcord.W005`` says so on every
#: deploy (see apps/core/checks.py).
PLATFORM_OWNER_EMAIL = os.environ.get("PLATFORM_OWNER_EMAIL", "").strip().lower()
DEBUG = env_bool("DEBUG", False)
ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", "localhost,127.0.0.1")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # sitemap.xml, for the public pages only. No models: the sites framework is
    # deliberately not installed -- see apps/core/seo.py::PublicPageSitemap.
    "django.contrib.sitemaps",
    # Anymail: the mail backend in production. Installed in every environment
    # so its checks run locally too, though dev still prints mail to the console.
    "anymail",
    # Tenancy foundation. `core` first: the others build on its abstractions.
    "apps.core",
    "apps.schools",
    "apps.accounts",
    # Onboarding step 2: classes and subjects.
    "apps.academics",
    # Onboarding step 3: terms and what each class owes.
    "apps.fees",
    # Onboarding step 4: the student roster.
    "apps.students",
    # Outbound parent messaging (SMS/WhatsApp). Staff-side only.
    "apps.messaging",
    # Money received, recorded against a student. Balances derive from here.
    "apps.payments",
    # Object storage for uploads. Installed in every environment so its checks
    # run locally too, though a machine with no MEDIA_BUCKET_NAME still writes to
    # the filesystem -- see STORAGES below.
    "storages",
    # Paid add-on: the enquiry-to-enrolment pipeline. Last, because it converts
    # an applicant into a student and prices the intake against an academics
    # class -- it depends on those apps, and nothing depends on it.
    "apps.admissions",
    # Reads every app above and writes to none of them. No models, so no
    # migrations: a report is a question asked of the data, not a record of it.
    "apps.reports",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Must follow AuthenticationMiddleware: it reads request.user.
    "apps.core.middleware.TenantMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # An account on a temporary password is held at Account settings until it
    # chooses its own. Reads request.user, so after AuthenticationMiddleware.
    "apps.accounts.middleware.RequirePasswordChangeMiddleware",
    # A URL belonging to a module this school does not have answers 403 with a
    # page saying so. Last, so the temporary-password redirect above wins: being
    # told a feature is unavailable is a dead end for somebody who cannot use any
    # screen yet. See apps/core/modules.py for why this is a middleware.
    "apps.core.middleware.ModuleAccessMiddleware",
]

# Sign in with an email address or a username. The username is derived from the
# email at signup and does not follow a later email change, so matching on email
# is what keeps a corrected address from stranding anyone's login.
AUTHENTICATION_BACKENDS = ["apps.accounts.backends.EmailOrUsernameBackend"]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.core.context_processors.tenancy",
                # The product's own name, on signed-out pages too -- which is
                # where it matters most.
                "apps.core.branding.branding",
                # Meta description, canonical URL and link-preview tags.
                "apps.core.seo.seo",
                # The password minimum and the generator's URL, for the
                # checklist in static/js/password.js.
                "apps.accounts.context_processors.password_policy",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# Set before the first migration and effectively permanent thereafter.
AUTH_USER_MODEL = "accounts.User"

# --- Password policy ----------------------------------------------------
# Modelled on NIST SP 800-63B: length and a check against known-breached
# secrets do the work, and there are deliberately no composition rules -- no
# "must contain a symbol", no forced rotation. Those push people towards
# P@ssw0rd1 and a sticky note, which is worse than the passphrase they would
# have picked unaided.
#
# The order matters only for which message a user sees first; every validator
# runs and any one of them can reject.
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    # NIST's floor. Django's default is also 8; stated explicitly because it is
    # policy here, not a default we happen to have inherited.
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 8},
    },
    # Django's bundled list of the 20k most common passwords. Kept alongside the
    # breach check rather than replaced by it: it is local, so it still rejects
    # the obvious ones when the network is down.
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
    # Have I Been Pwned, via k-anonymity -- see apps/accounts/password_validation.py.
    {"NAME": "apps.accounts.password_validation.BreachedPasswordValidator"},
]

# Read by pwned-passwords-django. The timeout is what makes a slow API a
# non-event: past this the validator gives up and falls through to the local
# validators rather than leaving someone staring at a spinner. ADD_PADDING asks
# the API to return a constant-size response, so the length of what comes back
# cannot be used to infer anything about the prefix we asked for.
PWNED_PASSWORDS = {
    "API_TIMEOUT": float(os.environ.get("PWNED_PASSWORDS_TIMEOUT", "1.5")),
    "ADD_PADDING": True,
}

# The breach check is the one validator that makes a network call. It is on
# everywhere except under the test runner, where an external dependency would
# make the suite slow, offline-hostile and non-deterministic. The validator's
# own tests turn it back on with a stubbed client, so the behaviour it guards
# is still covered -- see apps/accounts/tests_passwords.py.
PWNED_PASSWORDS_ENABLED = env_bool(
    "PWNED_PASSWORDS_ENABLED", default=sys.argv[1:2] != ["test"]
)

LOGIN_URL = "login"
# The role-aware dispatcher, not a screen: it forwards to whichever dashboard
# the signed-in role belongs on (apps/core/navigation.py::ROLE_HOME).
LOGIN_REDIRECT_URL = "core:dashboard"
# Signing out returns to the public front door rather than the login form --
# "you are out" reads better than being asked to go straight back in.
LOGOUT_REDIRECT_URL = "core:landing"

# The From: on password-reset mail. In dev the console backend prints the whole
# message, link included, so nothing has to be delivered for the flow to work.
DEFAULT_FROM_EMAIL = os.environ.get(
    "DEFAULT_FROM_EMAIL", f"{PRODUCT_NAME} <{SUPPORT_EMAIL}>"
)
PASSWORD_RESET_TIMEOUT = 60 * 60 * 24 * 3  # three days

# --- Public links -------------------------------------------------------
# Where this deployment answers from, e.g. https://app.schoolcord.com. Needed
# only by mail: an email has no request to resolve a relative path against, so
# the admissions reminder builds its "back to the school's enquiry page" link
# from this. Left blank the link is omitted rather than sent broken, which is
# why there is no invented default here.
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

# --- Messaging ----------------------------------------------------------
# Which gateway carries parent SMS is normally each school's own choice, stored
# on its SchoolMessagingConfig alongside the Sender ID it registered there.
#
# MESSAGING_PROVIDER is the platform override: when set it wins over every
# school's choice. It defaults to "console", which logs the message (Sender ID
# included) and marks it delivered, so a fresh checkout sends nothing anywhere
# and the whole feature still works with no credentials. Clear it in production
# -- MESSAGING_PROVIDER= -- and each school goes out through its own gateway.
MESSAGING_PROVIDER = os.environ.get("MESSAGING_PROVIDER", "console")

# There is deliberately no platform-wide sender ID. Who a message comes from is
# the school's own registered identity, resolved per send by
# apps/messaging/identity.py -- a school with no approved Sender ID is refused
# rather than falling back to a shared name.

# --- Gateway master credentials -----------------------------------------
# The platform holds the account and pays for the units; each school sends
# under its own Sender ID against it. A school that later takes out its own
# account puts its key on its messaging config, which wins over these.
#
# Termii is the gateway. BulkSMS Nigeria declined to support one account
# sending on behalf of many schools under each school's own Sender ID, which is
# the arrangement this platform is built on; Termii supports it explicitly.
TERMII_API_KEY = os.environ.get("TERMII_API_KEY", "")
TERMII_BASE_URL = os.environ.get("TERMII_BASE_URL", "https://api.ng.termii.com")

# --- Gateways kept but not the default ----------------------------------
# BulkSMS Nigeria's integration is complete and still works; a school already
# registered there keeps sending there, and MESSAGING_PROVIDER=bulksmsnigeria
# moves the whole platform back. Keeping it is the point of the provider
# abstraction -- see apps/messaging/providers/bulksmsnigeria.py.
BULKSMSNIGERIA_API_TOKEN = os.environ.get("BULKSMSNIGERIA_API_TOKEN", "")
BULKSMSNIGERIA_BASE_URL = os.environ.get(
    "BULKSMSNIGERIA_BASE_URL", "https://www.bulksmsnigeria.com"
)
# DND routing: 2 sends via the corporate route so reminders still reach the
# many Nigerian numbers on the Do-Not-Disturb register. See the provider.
BULKSMSNIGERIA_DND = os.environ.get("BULKSMSNIGERIA_DND", "2")

AFRICASTALKING_USERNAME = os.environ.get("AFRICASTALKING_USERNAME", "")
AFRICASTALKING_API_KEY = os.environ.get("AFRICASTALKING_API_KEY", "")
AFRICASTALKING_BASE_URL = os.environ.get(
    "AFRICASTALKING_BASE_URL", "https://api.africastalking.com"
)

LANGUAGE_CODE = "en-us"
TIME_ZONE = os.environ.get("TIME_ZONE", "UTC")
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

# ---------------------------------------------------------------------------
# Uploaded files
# ---------------------------------------------------------------------------
# What a school uploads -- its logo, and a parent's payment receipt -- used to be
# written to ``BASE_DIR / "media"`` and served by nothing. Two separate faults came
# out of that, and both of them are the reason this section exists:
#
#   * **nothing served it.** ``config/urls.py`` only routes ``/media/`` when DEBUG
#     is on, because Django refuses to serve uploads in production and whitenoise
#     deliberately covers ``static/`` only. So in production every uploaded file
#     404'd -- which is why a logo looked fine locally and was missing on the live
#     platform screens;
#   * **nothing kept it.** The container's filesystem on Railway is ephemeral. Even
#     with a route, every logo and every receipt vanished on the next deploy.
#
# So uploads go to object storage, and the bucket serves them. Static files are
# untouched: whitenoise does that job well, they are versioned by the manifest, and
# moving them would buy nothing.
#
# Any S3-compatible bucket works. Supabase Storage is the one configured, because
# the database is already there -- one vendor, one bill, one place credentials live.
# See the README for the Railway variables.
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

#: Reading this as set is what switches uploads over to the bucket. Left blank --
#: a developer's machine, the test suite, CI -- the filesystem backend stays, so
#: nobody needs credentials to run this project.
AWS_STORAGE_BUCKET_NAME = os.environ.get("MEDIA_BUCKET_NAME", "").strip()
USE_OBJECT_STORAGE = bool(AWS_STORAGE_BUCKET_NAME)

if USE_OBJECT_STORAGE:
    AWS_S3_ENDPOINT_URL = os.environ.get("MEDIA_S3_ENDPOINT_URL", "").strip()
    AWS_ACCESS_KEY_ID = os.environ.get("MEDIA_ACCESS_KEY_ID", "").strip()
    AWS_SECRET_ACCESS_KEY = os.environ.get("MEDIA_SECRET_ACCESS_KEY", "").strip()
    AWS_S3_REGION_NAME = os.environ.get("MEDIA_S3_REGION", "us-east-1").strip()

    #: Supabase Storage needs path-style addressing. The virtual-host style AWS
    #: prefers -- ``https://bucket.endpoint/key`` -- is not how Supabase routes,
    #: and boto3 defaults to it, so this has to be said.
    AWS_S3_ADDRESSING_STYLE = "path"

    #: Never overwrite. Two schools uploading ``logo.png`` must not become one
    #: school's logo on both of their receipts, and Django's own suffixing only
    #: happens when the storage says a name is taken.
    AWS_S3_FILE_OVERWRITE = False

    #: No ACL header. Supabase rejects requests that carry one, and bucket-level
    #: public/private is how it decides access anyway.
    AWS_DEFAULT_ACL = None
    #: Private by default. Uploads include children's birth certificates and
    #: parents' bank slips, and the app streams every file through its own views
    #: (apps/core/files.py), so nothing needs a public URL. Set false only for a
    #: bucket that is deliberately public.
    AWS_QUERYSTRING_AUTH = env_bool("MEDIA_PRIVATE_BUCKET", True)

    #: A year. Every stored name is unique -- see AWS_S3_FILE_OVERWRITE -- so a
    #: cached file can never be the wrong one.
    AWS_S3_OBJECT_PARAMETERS = {"CacheControl": "public, max-age=31536000, immutable"}

    #: Where a browser is sent for the file. Supabase serves a *public* bucket at
    #: ``/storage/v1/object/public/<bucket>/<key>``, which is not a path boto3
    #: would construct, so it is given explicitly. Unset for a non-Supabase
    #: bucket and django-storages builds the usual S3 URL.
    #:
    #: Ignored for a private bucket, and that is not an oversight: a signed URL
    #: carries its signature in the query string and is only valid against the
    #: endpoint that signed it, so pinning a custom domain would produce links
    #: that look right and 403. A private bucket gets signed endpoint URLs, which
    #: is the only thing that can work.
    _public_base = os.environ.get("MEDIA_PUBLIC_BASE_URL", "").strip().rstrip("/")
    if _public_base and not AWS_QUERYSTRING_AUTH:
        AWS_S3_CUSTOM_DOMAIN = _public_base.split("://", 1)[-1]
        AWS_S3_URL_PROTOCOL = _public_base.split("://", 1)[0] + ":"

STORAGES = {
    "default": (
        {
            "BACKEND": "storages.backends.s3.S3Storage",
            # Empty by default: the bucket is for uploads, so its root is the
            # media root and a stored key reads `logos/<school>/crest.png`.
            #
            # Set MEDIA_PREFIX to share the bucket with something else -- and then
            # leave the prefix *out* of MEDIA_PUBLIC_BASE_URL, because
            # django-storages appends it. Putting it in both is how the URL comes
            # out with the prefix twice.
            "OPTIONS": {"location": os.environ.get("MEDIA_PREFIX", "").strip("/")},
        }
        if USE_OBJECT_STORAGE
        else {"BACKEND": "django.core.files.storage.FileSystemStorage"}
    ),
    # Unchanged, and deliberately. Whitenoise serves these from the container,
    # the manifest versions them, and they are built at deploy time rather than
    # uploaded by anybody.
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

MESSAGE_STORAGE = "django.contrib.messages.storage.session.SessionStorage"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": os.environ.get("LOG_LEVEL", "INFO")},
}
