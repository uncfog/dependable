"""Record every active child for the term their campus is in now.

Only that term. Earlier terms get no rows, because nobody can say which class a child
was in back then, and a guess would be a debt. So no balance moves when this runs:
arrears start from the first term change after it, or from opening balances a school
enters itself. Adds rows and changes none, so reversing it has nothing to undo.
"""

from django.db import migrations


def backfill(apps, schema_editor):
    Term = apps.get_model("fees", "Term")
    Student = apps.get_model("students", "Student")
    Enrolment = apps.get_model("fees", "StudentTermEnrolment")

    for term in Term.objects.filter(is_current=True).iterator():
        rows = [
            Enrolment(
                school_id=school_id,
                branch_id=term.branch_id,
                student_id=student_id,
                term_id=term.pk,
                school_class_id=class_id,
            )
            for student_id, school_id, class_id in Student.objects.filter(
                branch_id=term.branch_id, status="active"
            ).values_list("pk", "school_id", "school_class_id")
        ]
        Enrolment.objects.bulk_create(rows, batch_size=500, ignore_conflicts=True)


class Migration(migrations.Migration):
    dependencies = [("fees", "0003_ledger")]

    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
