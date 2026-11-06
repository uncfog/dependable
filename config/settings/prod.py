"""Production settings.  No SQLite fallback -- a missing database must fail loudly."""

import os

from .base import *  # noqa: F401,F403
from django.core.exceptions import ImproperlyConfigured

from .base import (
    BASE_DIR,
    PRODUCT_NAME,
    SECRET_KEY,
    SUPPORT_EMAIL,
    env_list,
    is_placeholder_secret_key,
)
from .database import get_database_config
from .monitoring import start as start_error_reporting

DEBUG = False

# Off until SENTRY_DSN is set. See config/settings/monitoring.py for what is kept
# out of the reports.
start_error_reporting(
    os.environ.get("SENTRY_DSN", ""),
    os.environ.get("SENTRY_ENVIRONMENT", "production"),
)

# --- Signing key ---------------------------------------------------------
# A crash, not a warning. Sessions, password-reset links and CSRF tokens are all
# signed with this; on a key anyone can read in the repository, anyone can forge
# them. A deploy that will not start is the cheaper failure.
if is_placeholder_secret_key(SECRET_KEY):
    raise ImproperlyConfigured(
        "SECRET_KEY is missing or is a development placeholder. Set SECRET_KEY "
        "in the host's environment to a long random value, e.g. the output of "
        "python -c \"import secrets; print(secrets.token_urlsafe(50))\". "
        "Changing it signs everybody out."
    )

# --- Host and origin allow-lists ----------------------------------------
# ALLOWED_HOSTS / CSRF_TRUSTED_ORIGINS carry the domains we own and can write
# down (theschoolcord.com and friends). The platform's own domain is appended
# here instead: Railway generates it (schoolcord-production.up.railway.app) and
# exposes it as RAILWAY_PUBLIC_DOMAIN, so hardcoding it into an env var means
# re-editing the dashboard every time the service is renamed or recreated.
ALLOWED_HOSTS = env_list("ALLOWED_HOSTS")
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS")

_platform_domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
if _platform_domain and _platform_domain not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append(_platform_domain)
    # Django matches CSRF origins by scheme://host, so the origin needs the
    # https:// prefix the host entry must not have.
    origin = f"https://{_platform_domain}"
    if origin not in CSRF_TRUSTED_ORIGINS:
        CSRF_TRUSTED_ORIGINS.append(origin)

DATABASES = {"default": get_database_config(BASE_DIR, allow_fallback=False)}
DATABASES["default"]["CONN_MAX_AGE"] = int(os.environ.get("DB_CONN_MAX_AGE", "60"))

SECURE_SSL_REDIRECT = True
# Railway terminates TLS at its edge and forwards plain HTTP to the container,
# so request.is_secure() is False without this -- which would send the SSL
# redirect above into an infinite loop and mark no cookie as secure.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 60 * 60 * 24 * 30
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True

# --- Mail ---------------------------------------------------------------
# Resend over HTTPS, through Anymail. SMTP is deliberately gone: the host
# blocks outbound 587, so django.core.mail's smtp backend did not fail -- it
# hung, holding a web worker until the request timed out, which is how a
# password reset came to look like a broken site rather than a mail problem.
#
# There is no EMAIL_HOST / EMAIL_PORT / EMAIL_HOST_USER / EMAIL_HOST_PASSWORD
# here on purpose. Django reads those only for the smtp backend, so leaving
# them set would be a live-looking configuration that nothing uses and that
# the next person would reasonably believe is what sends the mail. Any such
# variables still set on the host are ignored.
EMAIL_BACKEND = "anymail.backends.resend.EmailBackend"
ANYMAIL = {"RESEND_API_KEY": os.environ.get("RESEND_API_KEY", "")}

# The From: address, which must be on the domain verified with Resend --
# send.theschoolcord.com, not the root domain. See apps/core/branding.py.
DEFAULT_FROM_EMAIL = os.environ.get(
    "DEFAULT_FROM_EMAIL", f"{PRODUCT_NAME} <{SUPPORT_EMAIL}>"
)
