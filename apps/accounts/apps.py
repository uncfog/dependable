from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.accounts"
    label = "accounts"
    verbose_name = "Accounts"

    def ready(self):
        # Connects the sign-in failure counters, which have to be listening before
        # the first attempt rather than from whenever the backend is first loaded.
        from . import backends  # noqa: F401
