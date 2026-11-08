"""Account routes that are ours rather than django.contrib.auth's."""

from django.urls import path

from . import mfa_views, ownership_views, views

app_name = "accounts"

urlpatterns = [
    path("settings/", views.AccountSettingsView.as_view(), name="settings"),
    path("two-step/setup/", mfa_views.MFASetupView.as_view(), name="mfa_setup"),
    path("two-step/verify/", mfa_views.MFAVerifyView.as_view(), name="mfa_verify"),
    path("generate-password/", views.generate_password_view, name="generate_password"),
    path("ownership/transfer/", ownership_views.StartTransferView.as_view(),
         name="ownership_start"),
    path("ownership/cancel/", ownership_views.CancelTransferView.as_view(),
         name="ownership_cancel"),
    path("ownership/accept/<str:token>/", ownership_views.AcceptTransferView.as_view(),
         name="ownership_accept"),
]
