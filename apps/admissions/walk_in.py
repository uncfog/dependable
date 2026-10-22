"""Enrol a walk-in: the parent is at the desk, pays, and the child is on the roll.

The full pipeline -- enquiry, application, assessment, offer, enrolment -- is
built for a school whose parents find it online and come back days later. At a
walk-in school none of those stages happens on a different day: the parent sees
the requirements, pays, and leaves with the child enrolled. Making them click
through five screens to record one conversation is the pipeline getting in the
way.

So this is one function and one transaction. It still writes the same records
the pipeline would, so nothing downstream can tell the difference:

* an :class:`~apps.admissions.models.Applicant`, marked enrolled with source
  "walk-in" -- the intake statistics ("how many walk-ins did we enrol this
  year?") and the child's admission history survive exactly as they would;
* the :class:`~apps.students.models.Student`, through the same conversion the
  pipeline's enrol step uses (:func:`apps.admissions.enrolment.convert`);
* an :class:`~apps.admissions.models.Assessment`, only if a score was given;
* the money, split by :func:`apps.admissions.split.record` into the intake and
  term ledgers.

Either all of that lands or none of it does. A child on the roll with the
money nowhere, or money recorded against nobody, is exactly the half-state a
front desk cannot see and cannot repair.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from apps.students.models import Student

from . import split
from .enrolment import convert
from .models import (
    Applicant,
    ApplicantSource,
    ApplicantStatus,
    Assessment,
    AssessmentOutcome,
)


@dataclass(frozen=True)
class WalkIn:
    applicant: Applicant
    student: Student
    split: split.Split | None


@transaction.atomic
def enrol_walk_in(data: dict, *, actor=None) -> WalkIn:
    """Create the applicant, the student and the payment from a cleaned form.

    ``data`` is :class:`~apps.admissions.forms.WalkInForm`'s ``cleaned_data``.
    Raises ``ValidationError`` from the student's own checks -- a clashing
    admission number, most often -- and rolls everything back when it does.
    """
    klass = data["school_class"]
    now = timezone.now()

    applicant = Applicant(
        school_id=klass.branch.school_id,
        branch_id=klass.branch_id,
        school_class=klass,
        level=klass.level,
        source=ApplicantSource.WALK_IN,
        # Offered, so the conversion below accepts it; it leaves as enrolled.
        status=ApplicantStatus.OFFERED,
        first_name=data["first_name"].strip(),
        last_name=data["last_name"].strip(),
        other_names=(data.get("other_names") or "").strip(),
        sex=data["sex"],
        date_of_birth=data.get("date_of_birth"),
        parent_name=data["parent_name"].strip(),
        parent_phone=data["parent_phone"],
        parent_email=data.get("parent_email") or "",
        address=data.get("address") or "",
        applied_at=now,
        decided_at=now,
        decided_by=actor,
        # Nothing is waiting on this record, so it has no window to expire in.
        expires_at=now,
        requirements_received=list(data.get("requirements_received") or []),
    )
    for kind in data.get("requirements_received") or []:
        upload = data.get(f"upload_{kind}")
        if upload:
            setattr(applicant, kind, upload)
    applicant.save()

    if data.get("assessment_score") is not None:
        Assessment.all_objects.create(
            school_id=applicant.school_id,
            branch_id=applicant.branch_id,
            applicant=applicant,
            scheduled_for=data["date_admitted"],
            sat_on=data["date_admitted"],
            score=data["assessment_score"],
            max_score=data["assessment_max_score"],
            # The child is being admitted on this sitting, which is the school
            # judging it a pass -- and an outcome other than "not yet sat" is
            # what marks the entrance-exam requirement as met.
            outcome=AssessmentOutcome.PASSED,
            recorded_by=actor,
        )

    student = convert(
        applicant,
        admission_number=data["admission_number"],
        school_class=klass,
        date_admitted=data["date_admitted"],
        actor=actor,
    )
    # The second phone belongs to the family's contact record, which is the
    # student's; the applicant form has never asked for one.
    if data.get("parent_phone_alt"):
        student.parent_phone_alt = data["parent_phone_alt"]
        student.save(update_fields=["parent_phone_alt", "updated_at"])

    money = None
    if data.get("amount"):
        money = split.record(
            applicant=applicant,
            student=student,
            amount=data["amount"],
            method=data["method"],
            reference=data.get("reference") or "",
            received_on=data["date_admitted"]
            if data["date_admitted"] <= timezone.localdate()
            else timezone.localdate(),
            actor=actor,
        )
    return WalkIn(applicant=applicant, student=student, split=money)
