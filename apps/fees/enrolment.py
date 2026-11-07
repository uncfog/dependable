"""Keeping each child's record of which class they were in for which term.

Two moments change it, and both are watched here so no screen has to remember:

* a term becomes current: every active child at that campus is recorded for it;
* a student is saved: their row for the current term follows their class, and a
  student who is new or back gets one.

Rows for earlier terms are never touched. So a promotion belongs after the new term
has started: moving a child before then changes the class recorded for the term that
is ending, and that term's fees with it.
"""

from __future__ import annotations

from django.db.models.signals import post_save


def start_term(term) -> None:
    """Record every active student at the term's campus. Safe to run again."""
    from apps.students.models import Student, StudentStatus

    from .models import StudentTermEnrolment

    students = Student.all_objects.filter(
        branch_id=term.branch_id, status=StudentStatus.ACTIVE
    ).values("pk", "school_class_id", "school_id")
    StudentTermEnrolment.all_objects.bulk_create(
        [
            StudentTermEnrolment(
                school_id=row["school_id"],
                branch_id=term.branch_id,
                student_id=row["pk"],
                term_id=term.pk,
                school_class_id=row["school_class_id"],
            )
            for row in students
        ],
        batch_size=500,
        ignore_conflicts=True,
    )


def sync_student(student) -> None:
    """Bring this student's row for their campus's current term up to date."""
    from apps.students.models import StudentStatus

    from .models import StudentTermEnrolment, Term

    term = Term.all_objects.filter(branch_id=student.branch_id, is_current=True).first()
    if term is None:
        return
    row = StudentTermEnrolment.all_objects.filter(student=student, term=term).first()
    if row is not None:
        if row.school_class_id != student.school_class_id:
            row.school_class_id = student.school_class_id
            row.save(update_fields=["school_class", "updated_at"])
    elif student.status == StudentStatus.ACTIVE:
        StudentTermEnrolment.all_objects.create(
            school_id=student.school_id,
            branch_id=student.branch_id,
            student=student,
            term=term,
            school_class_id=student.school_class_id,
        )


def _term_saved(sender, instance, raw=False, **kwargs):
    if not raw and instance.is_current:
        start_term(instance)


def _student_saved(sender, instance, raw=False, **kwargs):
    if not raw:
        sync_student(instance)


def connect() -> None:
    from apps.students.models import Student

    from .models import Term

    post_save.connect(_term_saved, sender=Term, dispatch_uid="fees_term_started")
    post_save.connect(_student_saved, sender=Student, dispatch_uid="fees_student_saved")
