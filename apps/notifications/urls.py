from django.urls import path

from . import views

app_name = "notifications"

urlpatterns = [
    path("", views.NotificationListView.as_view(), name="index"),
    path("panel/", views.PanelView.as_view(), name="panel"),
    path("live/", views.LiveView.as_view(), name="live"),
    path("<int:pk>/open/", views.OpenView.as_view(), name="open"),
    path("read/", views.MarkAllReadView.as_view(), name="mark_all_read"),
    path("settings/", views.SettingsView.as_view(), name="settings"),
    path("remind/", views.RemindCreateView.as_view(), name="remind"),
    path("reminders/<int:pk>/cancel/", views.ReminderCancelView.as_view(), name="reminder_cancel"),
    path("push/subscribe/", views.PushSubscribeView.as_view(), name="push_subscribe"),
    path("push/unsubscribe/", views.PushUnsubscribeView.as_view(), name="push_unsubscribe"),
]
