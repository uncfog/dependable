"""Admin for payments.

The audit fields are read-only throughout. Who recorded, confirmed or voided a
payment and when is the record of what happened, not a field to be tidied up --
the way to change a payment is through the screens, which stamp them.
"""

from django.contrib import admin

from apps.core.admin import TenantScopedAdminMixin

from .models import Payment, PaymentStatus


@admin.register(Payment)
class PaymentAdmin(TenantScopedAdminMixin):
    list_display = ("receipt_number", "student", "amount", "label", "method",
                    "status", "date_paid", "term", "branch")
    list_filter = ("status", "label", "method", "source", "term", "branch")
    search_fields = ("student__first_name", "student__last_name",
                     "student__admission_number", "reference",
                     "gateway_reference")
    autocomplete_fields = ("branch", "student", "term", "recorded_by",
                           "confirmed_by", "voided_by")
    date_hierarchy = "date_paid"
    readonly_fields = ("school", "receipt_number", "confirmed_at", "voided_at",
                       "created_at", "updated_at")
    fieldsets = (
        (None, {"fields": ("student", "term", "branch", "amount", "date_paid")}),
        (
            "What it was for",
            {
                "fields": ("label", "method", "reference", "note", "receipt"),
                "description": "The label is descriptive only — the balance is "
                "one combined figure, not tracked per label.",
            },
        ),
        (
            "Status",
            {
                "fields": ("status", "source", "recorded_by", "confirmed_by",
                           "confirmed_at"),
                "description": "Only confirmed payments count toward a "
                "student's balance.",
            },
        ),
        (
            "Void",
            {
                "fields": ("voided_by", "voided_at", "void_reason"),
                "classes": ("collapse",),
                "description": "A voided payment keeps its row and stops "
                "counting. It is never deleted.",
            },
        ),
        (
            "Online payments",
            {
                "fields": ("gateway_provider", "gateway_reference"),
                "classes": ("collapse",),
                "description": "Null on every manually recorded payment. "
                "Reserved for a future gateway.",
            },
        ),
        ("Audit", {"fields": ("school", "receipt_number", "created_at",
                              "updated_at"),
                   "classes": ("collapse",)}),
    )

    @admin.display(description="Receipt no.", ordering="id")
    def receipt_number(self, obj):
        return obj.receipt_number

    def get_readonly_fields(self, request, obj=None):
        readonly = list(super().get_readonly_fields(request, obj))
        if obj is not None and obj.status == PaymentStatus.VOID:
            # A voided payment is a closed record; the fix for a wrong one is
            # to record the right payment, not to edit history.
            readonly += [
                f.name for f in self.model._meta.fields
                if f.name not in readonly and f.editable
            ]
        elif obj is not None and obj.transaction and obj.transaction.is_shared:
            # A share of a larger sum. Changing it here could hand out more than came
            # in, so it goes through apps.payments.allocation like every other change.
            readonly += ["student", "term", "branch", "amount", "status"]
        return readonly
