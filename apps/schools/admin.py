from collections import Counter

from django.contrib import admin
from django.db import transaction
from django.utils import timezone
from django.utils.html import format_html

from apps.core.admin import TenantScopedAdminMixin

from . import teardown
from .models import Branch, PlanOrder, School, SchoolModule, SchoolStatus

_STATUS_COLOURS = {
    SchoolStatus.ACTIVE: "#0f7a52",
    SchoolStatus.TRIALING: "#1d3f79",
    SchoolStatus.PAST_DUE: "#a86a09",
    SchoolStatus.SUSPENDED: "#b32424",
    SchoolStatus.CANCELLED: "#5a6b85",
}


class BranchInline(admin.TabularInline):
    model = Branch
    extra = 0
    fields = ("name", "city", "state", "head", "is_active")
    autocomplete_fields = ("head",)
    show_change_link = True


@admin.register(School)
class SchoolAdmin(TenantScopedAdminMixin):
    tenant_school_field = "id"
    tenant_branch_field = None

    list_display = ("name", "plan", "status_badge", "branch_count", "user_count", "created_at")
    list_filter = ("plan", "status")
    search_fields = ("name", "slug", "contact_email")
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ("created_at", "updated_at")
    inlines = [BranchInline]
    fieldsets = (
        (None, {"fields": ("name", "slug")}),
        ("Subscription", {"fields": ("plan", "status")}),
        ("Contact", {"fields": ("contact_email", "contact_phone")}),
        ("Audit", {"fields": ("created_at", "updated_at"), "classes": ("collapse",)}),
    )

    @admin.display(description="Status", ordering="status")
    def status_badge(self, obj):
        return format_html(
            '<span style="background:{}1a;color:{};padding:2px 8px;'
            'border-radius:8px;font-weight:600;font-size:12px">{}</span>',
            _STATUS_COLOURS.get(obj.status, "#5a6b85"),
            _STATUS_COLOURS.get(obj.status, "#5a6b85"),
            obj.get_status_display(),
        )

    @admin.display(description="Branches")
    def branch_count(self, obj):
        return obj.branches.count()

    @admin.display(description="Users")
    def user_count(self, obj):
        return obj.users.count()

    # -- deletion ----------------------------------------------------------
    #
    # Deleting a school is closing a tenant, and the four PROTECT foreign keys
    # in this codebase made that impossible from here: Django's delete view met
    # the first one, listed the protected rows, and offered no button. The way
    # through was to empty the payments by hand, then the students, then the
    # classes, one changelist at a time.
    #
    # So the question is asked once and the whole school goes. The PROTECTs stay
    # exactly as they are on the models, where they guard the everyday case of
    # somebody tidying one class; apps/schools/teardown.py explains why that is
    # the right place for them and this is the right place for the exception.

    def get_deleted_objects(self, objs, request):
        """Counts and one button, instead of a refusal and a list.

        The stock implementation walks the graph and puts every PROTECTed row in
        ``protected``, which is what makes the template hide the confirm button.
        Nothing is protected *from this operation* -- the teardown removes those
        rows deliberately, in the order the database asks for -- so ``protected``
        comes back empty and the page can be confirmed.

        ``perms_needed`` is still computed the way Django computes it. A staff
        account that may not delete payments must not acquire that power by
        going through a school, and dropping the check to simplify this would
        have been a quiet privilege escalation.
        """
        counts: dict[str, int] = {}
        perms_needed: set[str] = set()

        for school in objs:
            for label, count in teardown.summarise(school):
                counts[label] = counts.get(label, 0) + count

        for relation, model in teardown.countable_relations():
            opts = model._meta
            if request.user.has_perm(f"{opts.app_label}.delete_{opts.model_name}"):
                continue
            if any(
                model._base_manager.filter(
                    **{relation.field.name: school}
                ).exists()
                for school in objs
            ):
                perms_needed.add(str(opts.verbose_name))

        return [str(school) for school in objs], counts, perms_needed, []

    def delete_model(self, request, obj):
        removed = teardown.remove(obj)
        # Said out loud, because the admin's own "was deleted successfully"
        # names only the school. Whoever closes a tenant should be able to see
        # from the message log what went with it.
        self.message_user(request, teardown.describe(removed))

    def delete_queryset(self, request, queryset):
        # One transaction across the whole selection, for the same reason a
        # single teardown is atomic: three schools closed and the fourth refused
        # is the state hardest to reason about afterwards.
        removed: Counter = Counter()
        with transaction.atomic():
            # Resolved to a list first: the rows are about to be deleted from
            # under the queryset that found them.
            for school in list(queryset):
                removed.update(teardown.remove(school))
        self.message_user(request, teardown.describe(removed))


@admin.register(Branch)
class BranchAdmin(TenantScopedAdminMixin):
    tenant_branch_field = "id"

    list_display = ("name", "school", "city", "state", "head", "is_active")
    list_filter = ("is_active", "school", "state")
    search_fields = ("name", "city", "state", "address", "school__name")
    autocomplete_fields = ("school", "head")
    readonly_fields = ("created_at", "updated_at")
    fieldsets = (
        (None, {"fields": ("school", "name", "is_active")}),
        ("Location", {"fields": ("city", "state", "address")}),
        ("Leadership", {"fields": ("head",)}),
        ("Audit", {"fields": ("created_at", "updated_at"), "classes": ("collapse",)}),
    )


@admin.register(SchoolModule)
class SchoolModuleAdmin(TenantScopedAdminMixin):
    """The stored module decisions, for support and for reading history.

    The screen a human uses is ``/platform/schools/<id>/`` -- it explains what each
    module is, refuses a key the registry does not know, and says what switching
    one off does. This exists for the questions the admin is good at: when did this
    change, and who changed it.

    ``key`` is a free-text choice field here on purpose. The registry in
    ``apps.core.modules`` is the authority on which keys exist, and a row carrying
    one it does not recognise is ignored by every reader rather than obeyed -- so a
    typo here is inert rather than dangerous.
    """

    tenant_school_field = "school_id"
    tenant_branch_field = None

    list_display = ("school", "key", "enabled", "changed_by", "updated_at")
    list_filter = ("key", "enabled")
    search_fields = ("school__name", "key")
    autocomplete_fields = ("school", "changed_by")
    readonly_fields = ("created_at", "updated_at")


@admin.register(PlanOrder)
class PlanOrderAdmin(admin.ModelAdmin):
    """Where the platform owner confirms a bank transfer and switches the plan.

    A school cannot move itself onto a paid plan. Its order waits here until the
    money has arrived, and the only way to change the plan is the action below.
    Status and the plan itself are read-only, so the action is the only path.
    """

    list_display = (
        "reference",
        "school",
        "plan",
        "amount_naira",
        "status",
        "transfer_reported_at",
        "paid_at",
    )
    list_filter = ("status", "plan")
    search_fields = ("reference", "school__name")
    readonly_fields = (
        "reference",
        "school",
        "plan",
        "amount_naira",
        "status",
        "transfer_reported_at",
        "paid_at",
        "created_at",
        "updated_at",
    )
    actions = ("mark_paid_and_activate",)

    def get_queryset(self, request):
        return PlanOrder.all_objects.select_related("school")

    def has_add_permission(self, request):
        return False

    @admin.action(description="Confirm payment and switch the school to this plan")
    def mark_paid_and_activate(self, request, queryset):
        now = timezone.now()
        switched = 0
        with transaction.atomic():
            open_orders = queryset.exclude(
                status__in=(PlanOrder.Status.PAID, PlanOrder.Status.CANCELLED)
            ).select_related("school")
            for order in open_orders:
                school = order.school
                school.plan = order.plan
                school.status = SchoolStatus.ACTIVE
                school.save(update_fields=["plan", "status", "updated_at"])
                order.status = PlanOrder.Status.PAID
                order.paid_at = now
                order.save(update_fields=["status", "paid_at", "updated_at"])
                switched += 1
        self.message_user(request, f"{switched} school(s) switched to the plan they paid for.")
