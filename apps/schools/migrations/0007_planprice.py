from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion

# Today's prices, written out rather than imported, so this migration means the same
# thing however apps/schools/plans.py changes later.
SEED = (
    ("free", 0, "Up to 100 students", 100),
    ("starter", 45000, "Up to 200 students", 200),
    ("growth", 90000, "Up to 600 students", 600),
    ("professional", 180000, "Up to 1,200 students", 1200),
    ("enterprise", None, "More than 1,200 students", None),
)


def seed(apps, schema_editor):
    PlanPrice = apps.get_model("schools", "PlanPrice")
    for plan, price, label, ceiling in SEED:
        PlanPrice.objects.get_or_create(
            plan=plan,
            defaults={"price_naira": price, "students_label": label, "max_students": ceiling},
        )


class Migration(migrations.Migration):

    dependencies = [
        ("schools", "0006_planorder"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="PlanPrice",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("plan", models.CharField(choices=[("free", "Free"), ("trial", "Trial"), ("starter", "Starter"), ("growth", "Growth"), ("professional", "Professional"), ("enterprise", "Enterprise")], max_length=20, unique=True)),
                ("price_naira", models.PositiveIntegerField(blank=True, null=True)),
                ("students_label", models.CharField(max_length=60)),
                ("max_students", models.PositiveIntegerField(blank=True, null=True)),
                ("updated_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
            ],
            options={"abstract": False},
        ),
        migrations.CreateModel(
            name="PlanPriceChange",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("plan", models.CharField(choices=[("free", "Free"), ("trial", "Trial"), ("starter", "Starter"), ("growth", "Growth"), ("professional", "Professional"), ("enterprise", "Enterprise")], max_length=20)),
                ("summary", models.CharField(max_length=300)),
                ("changed_at", models.DateTimeField(auto_now_add=True)),
                ("changed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-changed_at", "-id"]},
        ),
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
