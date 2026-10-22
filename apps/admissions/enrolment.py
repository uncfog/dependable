"""The conversion: an accepted applicant becomes a student on the roll.

This is the one moment the admissions pipeline touches the rest of the
platform, and the only place in the app that writes to another app's table. It
is deliberately a service function rather than a model method, because it is a
*transaction across two apps* -- a Student is created, an Applicant is closed,
and either both happen or neither does. A half-run conversion would leave a
child enrolled with nobody able to find the application they were admitted on.

What the conversion does not do is copy fees or touch the termly fee
structure. The new student is billed by the same rule as every other student
-- their class's structure for the current term -- from the moment they exist.

What it does do with money is one thing: a family at a walk-in intake pays the
term's fees in the same lump as the admission fee, before there is a student
to credit them to, so all of it was recorded as admission money. At conversion
:func:`apps.admissions.split.settle_at_enrolment` moves the term's share onto
the new student as an ordinary payment -- otherwise the roster shows a term as
owed that the family has already paid. What is genuinely intake money stays on
the applicant, which is what keeps it out of a term's collection figure.

The applicant is kept, not deleted. ``applicant.student`` is the link, and the
history -- who enquired, when, from which Instagram post, what they scored --
survives the conversion. Losing it would make the intake unanswerable a term
later.
"""

from __future__ import annotations

import re
from datetime import date

from django.db import transaction
from django.utils import timezone

from apps.students.models import Student, StudentStatus
from apps.students.validators import normalise_admission_number

from .models import AdmissionsConfig, Applicant, ApplicantStatus
from .split import settle_at_enrolment


class EnrolmentError(Exception):
    """Why this applicant cannot be enrolled yet.

    Carries a message written for the person at the front desk, because that is
    where it is shown -- the views turn it straight into a form error.
    """


def can_enrol(applicant: Applicant) -> list[str]:
    """Everything standing between this applicant and the roll.

    Returns a list rather than raising, so a screen can show all of the
    blockers at once instead of revealing them one refresh at a time.
    """
    blockers: list[str] = []

    if applicant.status == ApplicantStatus.ENROLLED or applicant.student_id:
        blockers.append(f"{applicant.full_name} has already been enrolled.")
        return blockers
    if applicant.status != ApplicantStatus.OFFERED:
        blockers.append(
            "Only an applicant who has been offered a place can be enrolled. "
            f"{applicant.full_name} is at the "
            f"{applicant.status_label.lower()} stage."
        )
    if not applicant.school_class_id:
        blockers.append(
            "Choose the class to enrol them into -- a student cannot be on the "
            "roll without one."
        )
    config = AdmissionsConfig.for_school(applicant.school_id)
    if config.require_payment_before_enrolment and not applicant.has_paid_admission:
        blockers.append(
            "The admission fee has not been confirmed. Record the payment "
            "first, or turn off the payment requirement in admissions settings."
        )
    return blockers


def enrol(
    applicant: Applicant,
    *,
    admission_number: str,
    school_class=None,
    date_admitted: date | None = None,
    actor=None,
    status: str = StudentStatus.ACTIVE,
) -> Student:
    """Create the Student this applicant becomes, and close the application.

    ``school_class`` overrides the class applied for -- a child offered a place
    in Primary 3 is sometimes placed in Primary 2 after the assessment, and the
    front desk needs to say so at the moment of enrolling rather than by
    editing the applicant first.

    Raises :class:`EnrolmentError` when :func:`can_enrol` finds anything, so no
    caller has to remember to check.
    """
    if school_class is not None:
        applicant.school_class = school_class

    blockers = can_enrol(applicant)
    if blockers:
        raise EnrolmentError(" ".join(blockers))

    with transaction.atomic():
        student = convert(
            applicant,
            admission_number=admission_number,
            date_admitted=date_admitted,
            actor=actor,
            status=status,
        )
        # Inside the same transaction: a student whose term money was left
        # behind on the applicant would show a balance the family has paid.
        settle_at_enrolment(applicant, student, actor=actor)

    return student


@transaction.atomic
def convert(
    applicant: Applicant,
    *,
    admission_number: str,
    school_class=None,
    date_admitted: date | None = None,
    actor=None,
    status: str = StudentStatus.ACTIVE,
) -> Student:
    """Create the Student and close the applicant -- with no gate in front.

    The conversion itself, shared by the two ways in: :func:`enrol`, which
    puts :func:`can_enrol` in front of it, and the walk-in screen
    (:mod:`apps.admissions.walk_in`), which decides and enrols in one sitting
    and applies the same payment rule to the money on its own form. Anything
    else should call :func:`enrol`.
    """
    if school_class is not None:
        applicant.school_class = school_class
    klass = applicant.school_class
    if klass is None:
        raise EnrolmentError(
            "Choose the class to enrol them into -- a student cannot be on the "
            "roll without one."
        )
    if klass.branch_id != applicant.branch_id:
        # Belt and braces: the forms already scope the class list to the
        # applicant's branch, and a mismatch here would put a child on another
        # campus's register.
        raise EnrolmentError("That class belongs to a different campus.")

    student = Student(
        school_id=applicant.school_id,
        branch_id=applicant.branch_id,
        school_class=klass,
        admission_number=normalise_admission_number(admission_number),
        first_name=applicant.first_name,
        last_name=applicant.last_name,
        other_names=applicant.other_names,
        sex=applicant.sex,
        date_of_birth=applicant.date_of_birth,
        date_admitted=date_admitted or timezone.localdate(),
        status=status,
        parent_name=applicant.parent_name,
        parent_phone=applicant.parent_phone,
        parent_email=applicant.parent_email,
        address=applicant.address,
    )
    # full_clean, not a bare save: the admission number has to collide
    # inside this branch here rather than at the database, so the front
    # desk gets "that number is taken" and not a 500.
    #
    # Nothing is excluded, and that is load-bearing: the roster's
    # uniqueness rule is UniqueConstraint(Upper("admission_number"),
    # "branch"), and Django skips a constraint that mentions an excluded
    # field. Excluding `branch` here -- the obvious thing to do, since it
    # is derived rather than typed -- would silently turn the check off and
    # let the collision through to the database as an IntegrityError.
    student.full_clean()
    student.save()

    applicant.student = student
    applicant.status = ApplicantStatus.ENROLLED
    applicant.enrolled_at = timezone.now()
    if actor is not None and applicant.decided_by_id is None:
        applicant.decided_by = actor
    applicant.save()
    return student


# ---------------------------------------------------------------------------
# Admission numbers
# ---------------------------------------------------------------------------

#: "FA/2026/007" -- the shape the client's own records use, and the shape the
#: student importer documents. Suggested, never imposed: the field stays free
#: text because a school that numbers differently must be able to keep doing so.
_NUMBER_PATTERN = re.compile(r"^(?P<prefix>.+)/(?P<year>\d{4})/(?P<seq>\d+)$")


def school_prefix(school) -> str:
    """"Fulfilled Academy" -> "FA". A one-word school keeps its first letters."""
    words = [w for w in re.split(r"\s+", (school.name or "").strip()) if w]
    if not words:
        return "SCH"
    if len(words) == 1:
        return words[0][:3].upper()
    return "".join(word[0] for word in words)[:4].upper()


def suggest_admission_number(branch, *, when: date | None = None) -> str:
    """The next free number for this branch, in the school's own shape.

    Numbers restart each year and are unique per branch, not globally -- the
    same rule the roster enforces -- so this only ever looks at one branch's
    rows for one year.
    """
    when = when or timezone.localdate()
    prefix = f"{school_prefix(branch.school)}/{when.year}/"

    highest = 0
    existing = Student.all_objects.filter(
        branch=branch, admission_number__istartswith=prefix
    ).values_list("admission_number", flat=True)
    for number in existing:
        match = _NUMBER_PATTERN.match(number)
        if match:
            highest = max(highest, int(match.group("seq")))

    return f"{prefix}{highest + 1:03d}"
