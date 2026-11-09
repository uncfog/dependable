"""The bell, the notifications page, settings, and 'Remind me'.

Every query starts from ``Notification.objects.for_user(request.user)`` or the
person's own reminders, so nobody can read, open or clear anyone else's.
"""

from __future__ import annotations

from datetime import timedelta

from django import forms
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from django.views.generic import ListView

from .models import (
    ALWAYS_ON,
    REMINDER_KINDS,
    Kind,
    Notification,
    NotificationPreference,
    Reminder,
    ReminderTarget,
)

#: How many the bell's panel shows.
PANEL_SIZE = 20


def _back(request, fallback: str) -> str:
    wanted = request.POST.get("next") or request.GET.get("next") or ""
    if wanted and url_has_allowed_host_and_scheme(
        wanted, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return wanted
    return fallback


class NotificationListView(LoginRequiredMixin, ListView):
    template_name = "notifications/index.html"
    context_object_name = "notifications"
    paginate_by = 30

    FILTERS = ("all", "unread", "reminders")

    def show(self) -> str:
        wanted = self.request.GET.get("show", "all")
        return wanted if wanted in self.FILTERS else "all"

    def get_queryset(self):
        rows = Notification.objects.for_user(self.request.user)
        show = self.show()
        if show == "unread":
            rows = rows.unread()
        elif show == "reminders":
            rows = rows.filter(kind__in=REMINDER_KINDS)
        return rows

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        mine = Notification.objects.for_user(self.request.user)
        context.update(
            page_title="Notifications",
            show=self.show(),
            unread_count=mine.unread().count(),
            upcoming=Reminder.objects.filter(
                user=self.request.user, sent_at__isnull=True
            ).order_by("due_on")[:20],
        )
        return context


class PanelView(LoginRequiredMixin, View):
    """The bell's dropdown, fetched when it opens."""

    def get(self, request):
        rows = Notification.objects.for_user(request.user)[:PANEL_SIZE]
        return render(request, "notifications/_panel.html", {"notifications": rows})


class OpenView(LoginRequiredMixin, View):
    """Mark one read and go where it points."""

    def get(self, request, pk):
        notification = get_object_or_404(Notification.objects.for_user(request.user), pk=pk)
        if notification.read_at is None:
            notification.read_at = timezone.now()
            notification.save(update_fields=["read_at"])
        return HttpResponseRedirect(notification.safe_url or reverse("notifications:index"))


class MarkAllReadView(LoginRequiredMixin, View):
    def post(self, request):
        count = Notification.objects.for_user(request.user).unread().update(
            read_at=timezone.now()
        )
        if count:
            messages.success(request, "All notifications marked as read.")
        return HttpResponseRedirect(_back(request, reverse("notifications:index")))


#: The kinds a person may turn off, in the order the settings page lists them.
MUTABLE_KINDS = [kind for kind in Kind if kind not in ALWAYS_ON]


class SettingsView(LoginRequiredMixin, View):
    template_name = "notifications/settings.html"

    def get(self, request):
        pref = NotificationPreference.objects.filter(user=request.user).first()
        muted = set(pref.muted if pref else [])
        rows = [{"kind": kind, "label": kind.label, "on": kind not in muted}
                for kind in MUTABLE_KINDS]
        always = [kind.label for kind in Kind if kind in ALWAYS_ON]
        from django.conf import settings as django_settings

        from . import push
        from .models import PushSubscription

        return render(request, self.template_name, {
            "page_title": "Notification settings", "rows": rows, "always": always,
            "push_ready": push.configured() and django_settings.PWA_ENABLED,
            "vapid_public_key": django_settings.WEBPUSH_VAPID_PUBLIC_KEY,
            "device_count": PushSubscription.objects.filter(user=request.user).count(),
        })

    def post(self, request):
        wanted_on = set(request.POST.getlist("on"))
        muted = [kind.value for kind in MUTABLE_KINDS if kind.value not in wanted_on]
        NotificationPreference.objects.update_or_create(
            user=request.user, defaults={"muted": muted}
        )
        messages.success(request, "Notification settings saved.")
        return HttpResponseRedirect(reverse("notifications:settings"))


class RemindForm(forms.Form):
    target_type = forms.ChoiceField(choices=ReminderTarget.choices, widget=forms.HiddenInput)
    target_id = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    due_on = forms.DateField(label="Remind me on", widget=forms.DateInput(attrs={"type": "date"}))
    note = forms.CharField(label="Note", max_length=200, required=False)

    def clean_due_on(self):
        due_on = self.cleaned_data["due_on"]
        today = timezone.localdate()
        if due_on < today:
            raise forms.ValidationError("Choose today or a later day.")
        if due_on > today + timedelta(days=366):
            raise forms.ValidationError("Choose a day within the next year.")
        return due_on


def _target(user, target_type: str, target_id: int):
    """The thing to be reminded about, if this person can see it.

    Read through the tenant-scoped managers, so a campus bursar cannot set a
    reminder on another campus's child by changing the id.
    """
    if target_type == ReminderTarget.STUDENT:
        from apps.students.models import Student

        student = Student.objects.filter(pk=target_id).first()
        if student is None:
            return None
        return student, student.full_name, reverse("students:student_detail", args=[student.pk])
    if target_type == ReminderTarget.PAYMENT:
        from apps.payments.models import Payment

        payment = Payment.objects.select_related("student").filter(pk=target_id).first()
        if payment is None:
            return None
        label = f"{payment.receipt_number} for {payment.student.full_name}"
        return payment, label, reverse("payments:payment_detail", args=[payment.pk])
    return None


class RemindCreateView(LoginRequiredMixin, View):
    def post(self, request):
        form = RemindForm(request.POST)
        fallback = reverse("notifications:index")
        if not form.is_valid() or not request.user.school_id:
            errors = "; ".join(e for errs in form.errors.values() for e in errs) if form.errors else ""
            messages.error(request, errors or "That reminder could not be set.")
            return HttpResponseRedirect(_back(request, fallback))
        data = form.cleaned_data
        found = _target(request.user, data["target_type"], data["target_id"])
        if found is None:
            raise Http404("Nothing to remind you about there.")
        _, label, url = found
        Reminder.objects.create(
            user=request.user, school_id=request.user.school_id, due_on=data["due_on"],
            note=data["note"].strip(), target_type=data["target_type"],
            target_id=data["target_id"], target_label=label[:160], url=url,
        )
        messages.success(
            request,
            f"We will remind you about {label} on {data['due_on'].day} {data['due_on']:%b %Y}.",
        )
        return HttpResponseRedirect(_back(request, url))


class ReminderCancelView(LoginRequiredMixin, View):
    def post(self, request, pk):
        reminder = get_object_or_404(Reminder, pk=pk, user=request.user, sent_at__isnull=True)
        reminder.delete()
        messages.success(request, "Reminder cancelled.")
        return HttpResponseRedirect(_back(request, reverse("notifications:index")))


class PushSubscribeView(LoginRequiredMixin, View):
    """The browser's push address for this person, sent by js/push.js."""

    def post(self, request):
        import json

        from django.http import JsonResponse

        from . import push
        from .models import PushSubscription

        if not push.configured():
            return JsonResponse({"error": "Push is not switched on for this site."}, status=503)
        try:
            data = json.loads(request.body.decode("utf-8") or "{}")
            endpoint = str(data["endpoint"])
            p256dh = str(data["keys"]["p256dh"])
            auth = str(data["keys"]["auth"])
        except (ValueError, KeyError, TypeError):
            return JsonResponse({"error": "That is not a push subscription."}, status=400)
        if not push.allowed_endpoint(endpoint) or not (0 < len(p256dh) <= 200 and 0 < len(auth) <= 100):
            return JsonResponse({"error": "That push address is not accepted."}, status=400)
        # A browser belongs to whoever is signed in on it now.
        PushSubscription.objects.update_or_create(
            endpoint=endpoint,
            defaults={
                "user": request.user, "p256dh": p256dh, "auth": auth,
                "user_agent": request.META.get("HTTP_USER_AGENT", "")[:200],
            },
        )
        push.remember_in_session(request, endpoint)
        return JsonResponse({"ok": True})


class PushUnsubscribeView(LoginRequiredMixin, View):
    def post(self, request):
        import json

        from django.http import JsonResponse

        from .models import PushSubscription

        try:
            endpoint = str(json.loads(request.body.decode("utf-8") or "{}")["endpoint"])
        except (ValueError, KeyError, TypeError):
            return JsonResponse({"error": "No push address given."}, status=400)
        PushSubscription.objects.filter(user=request.user, endpoint=endpoint).delete()
        return JsonResponse({"ok": True})
