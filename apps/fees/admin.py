from django.contrib import admin
from django.db.models import Sum

from apps.core.admin import TenantScopedAdminMixin

from .models import (
    FeeComponent,
    FeeStructure,
    StudentFeeAdjustment,
    StudentTermEnrolment,
    Term,
)


class ReadOnlyAdmin(TenantScopedAdminMixin):
    """A record to read. These change through the app's own rules, or not at all."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(StudentFeeAdjustment)
class StudentFeeAdjustmentAdmin(ReadOnlyAdmin):
    list_display = ("student", "term", "kind", "amount", "description", "voided_at")
    list_filter = ("kind", "term", "branch")
    search_fields = ("student__admission_number", "student__last_name", "description")


@admin.register(StudentTermEnrolment)
class StudentTermEnrolmentAdmin(ReadOnlyAdmin):
    list_display = ("student", "term", "school_class", "branch")
    list_filter = ("term", "branch")
    search_fields = ("student__admission_number", "student__last_name")


@admin.register(Term)
class TermAdmin(TenantScopedAdminMixin):
    list_display = ("name", "branch", "academic_year", "get_sequence_display",
                    "is_current", "structure_count")
    list_filter = ("is_current", "academic_year", "sequence", "branch")
    search_fields = ("name", "academic_year", "branch__name")
    autocomplete_fields = ("branch",)
    readonly_fields = ("school", "created_at", "updated_at")
    fieldsets = (
        (None, {"fields": ("branch", "name", "academic_year", "sequence")}),
        (
            "Status",
            {
                "fields": ("is_current",),
                "description": "Only one term per branch can be current; "
                "setting this clears the others.",
            },
        ),
        ("Audit", {"fields": ("school", "created_at", "updated_at"),
                   "classes": ("collapse",)}),
    )

    @admin.display(description="Fee structures")
    def structure_count(self, obj):
        return obj.fee_structures.count()


class FeeComponentInline(admin.TabularInline):
    model = FeeComponent
    extra = 1
    fields = ("position", "name", "amount")
    ordering = ("position", "id")


@admin.register(FeeStructure)
class FeeStructureAdmin(TenantScopedAdminMixin):
    list_display = ("school_class", "term", "branch", "component_count", "total_display")
    list_filter = ("term", "branch", "school_class__level")
    search_fields = ("school_class__name", "term__name", "branch__name")
    autocomplete_fields = ("branch", "school_class", "term")
    readonly_fields = ("school", "created_at", "updated_at")
    inlines = [FeeComponentInline]
    fieldsets = (
        (None, {"fields": ("school_class", "term", "branch")}),
        (
            "Audit",
            {
                "fields": ("school", "created_at", "updated_at"),
                "classes": ("collapse",),
                "description": "Branch and school are derived from the class.",
            },
        ),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("components")

    @admin.display(description="Line items")
    def component_count(self, obj):
        return len(obj.components.all())

    @admin.display(description="Total")
    def total_display(self, obj):
        return f"₦{obj.total:,.2f}"


@admin.register(FeeComponent)
class FeeComponentAdmin(TenantScopedAdminMixin):
    list_display = ("name", "amount", "fee_structure", "position")
    list_filter = ("fee_structure__term", "branch")
    search_fields = ("name", "fee_structure__school_class__name")
    autocomplete_fields = ("fee_structure",)
    readonly_fields = ("school", "branch", "created_at", "updated_at")
