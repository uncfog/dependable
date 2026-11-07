"""Local development settings.  The default for ``manage.py``."""

from .base import *  # noqa: F401,F403
from .base import BASE_DIR, env_bool
from .database import get_database_config

DEBUG = env_bool("DEBUG", True)
PWA_ENABLED = env_bool("PWA_ENABLED", False)
ALLOWED_HOSTS = ["*"]

# Nobody needs an authenticator to run this on their own machine.
MFA_REQUIRED_FOR_PLATFORM = env_bool("MFA_REQUIRED_FOR_PLATFORM", False)

# Postgres when it is up, SQLite when it is not.
DATABASES = {"default": get_database_config(BASE_DIR, allow_fallback=True)}

# Plain static serving in dev -- the manifest storage would demand a
# collectstatic run before the first page load.
#
# Only the staticfiles entry. Replacing the whole dict also replaced the *media*
# backend, which pinned uploads to the filesystem even on a machine that had a
# bucket configured -- so the one way to check the production upload path before
# deploying it silently did not work. base.py decides that half from the
# environment; this leaves it alone.
STORAGES = {
    **STORAGES,  # noqa: F405 -- from base, via the star import above
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
