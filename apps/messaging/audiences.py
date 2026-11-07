"""Turning "JSS 1A parents" into a list of phone numbers.

One module, because the same resolution has to answer three different questions
and they must never disagree:

* the live count on the compose screen, before anything is sent;
* the rows actually written when Send is pressed;
* the description stored on the message, which is what the log will say
  happened.

All three come out of :func:`resolve`, so the count a bursar saw is by
construction the batch that went out.

Every queryset here goes through ``Student.objects``, which is tenant-scoped.
That is what stops one branch messaging another's parents -- not a filter in a
view, which someone would eventually forget to write.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from apps.students import fees
from apps.students.models import Student, StudentStatus

from .models import AudienceType

ZERO = Decimal("0")


@dataclass(frozen=True)
class Audience:
    """A resolved audience: who gets the message, and what to call them."""

    type: str
    description: str
    #: Active students with a parent number, in roster order.
    students: list[Student] = field(default_factory=list)
    #: Students the filter picked but who have no parent number on file. They
    #: are surfaced rather than silently dropped: a parent nobody can reach is
    #: a data problem the school can fix, and pretending the batch was complete
    #: hides it.
    unreachable: list[Student] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.students)

    @property
    def unreachable_count(self) -> int:
        return len(self.unreachable)

    def __bool__(self) -> bool:
        return bool(self.students)


def contactable(branch=None):
    """The base roster a message can go to: enrolled, with a number on file.

    Already narrowed to the caller's own school and branch by the manager.
    ``branch`` narrows it once more, which matters for a school owner: they can
    see two campuses, but a message belongs to one, so the count they are shown
    must be the campus they picked and not the sum of both.

    Withdrawn and graduated students are excluded -- their parents are not who
    a fee reminder is for, and a school that texts a family about a child who
    left two years ago will not send a second batch.
    """
    queryset = Student.objects.filter(status=StudentStatus.ACTIVE).select_related(
        "school_class", "branch"
    )
    if branch is not None:
        queryset = queryset.filter(branch=branch)
    return queryset


def _split(students) -> tuple[list[Student], list[Student]]:
    reachable, unreachable = [], []
    for student in students:
        (reachable if student.parent_phone.strip() else unreachable).append(student)
    return reachable, unreachable


def owing_students(
    students=None, *, branch=None, schedule: fees.FeeSchedule | None = None
):
    """The students whose fees are not settled for their branch's current term.

    Derived, like every other fee figure on the platform: expected comes from
    the class's structure, paid from confirmed payments, and a student owes
    when the difference is positive. A class with no fee structure this term is
    *not* owing -- nobody has said what it costs, so there is nothing to chase,
    and texting those parents would be the platform inventing a debt.

    ``schedule`` is injectable so a caller that has already loaded one (or a
    test that wants to state who has paid) does not pay for a second pass.
    """
    students = list(contactable(branch) if students is None else students)
    if not students:
        return []
    if schedule is None:
        schedule = fees.load(students)
    return [
        student
        for student in students
        if _owes(schedule.position_for(student))
    ]


def _owes(position: fees.FeePosition) -> bool:
    # Earlier terms included: a child whose only debt is from last term still owes.
    return position.total_owed > ZERO


def resolve(
    audience_type: str,
    *,
    branch=None,
    school_class=None,
    students=None,
    schedule: fees.FeeSchedule | None = None,
) -> Audience:
    """Work out who ``audience_type`` means.

    ``branch`` scopes every audience to one campus. ``school_class`` is
    required by :attr:`AudienceType.CLASS` and ``students``
    by :attr:`AudienceType.STUDENTS`; both are expected to have come off a form
    whose querysets were tenant-scoped, so a guessed id has already been
    rejected before it reaches here. An audience with neither resolves to
    nobody rather than to everybody -- the failure mode of a broken filter must
    be an empty batch, never a text to the whole school.
    """
    if audience_type == AudienceType.CLASS:
        if school_class is None:
            return Audience(type=audience_type, description="No class chosen")
        picked = list(contactable(branch).filter(school_class=school_class))
        description = f"{school_class.display_name} parents"

    elif audience_type == AudienceType.STUDENTS:
        picked = list(students or [])
        if branch is not None:
            picked = [s for s in picked if s.branch_id == branch.pk]
        if not picked:
            return Audience(type=audience_type, description="No students chosen")
        description = _describe_students(picked)

    elif audience_type == AudienceType.OWING:
        picked = owing_students(branch=branch, schedule=schedule)
        description = "Parents who owe fees this term"

    else:
        picked = list(contactable(branch))
        description = "All parents"

    reachable, unreachable = _split(picked)
    return Audience(
        type=audience_type,
        description=description,
        students=reachable,
        unreachable=unreachable,
    )


def _describe_students(students: list[Student]) -> str:
    """Name a handful; count the rest.

    The description is what the log will show months later, so "Chinaza
    Okonkwo and Daniel Ekanem" beats "2 students" when it fits, and "12
    selected students" beats a paragraph when it does not.
    """
    names = [student.full_name for student in students[:2]]
    remaining = len(students) - len(names)
    if remaining <= 0:
        return f"{' and '.join(names)} — parents"
    if len(students) > 3:
        return f"{len(students)} selected students — parents"
    return f"{', '.join(names)} and 1 other — parents"
