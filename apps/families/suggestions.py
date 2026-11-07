"""Children who might be siblings, for a person to decide about.

The only signals are a shared phone number or email address. Surnames and guardian
names are never read here: two unrelated families can share both, and a wrong link puts
one family's balances beside another's. Even a shared phone is only a question.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass

from apps.onboarding.models import MatchDecision
from apps.students.models import Student, StudentStatus

from .models import FamilyMembership

#: More children than this on one number is more likely a driver or agent than a parent.
CROWDED = 5
LIMIT = 50


@dataclass(frozen=True)
class Group:
    signal: str
    value: str
    students: tuple[Student, ...]
    signature: str

    @property
    def is_crowded(self) -> bool:
        return len(self.students) > CROWDED


def _signature(students) -> str:
    # By who is in it, so a new child on the same number is asked about afresh.
    members = ",".join(str(pk) for pk in sorted(s.pk for s in students))
    return hashlib.sha256(f"family-suggestion:{members}".encode()).hexdigest()


def groups() -> list[Group]:
    """What the caller can see, scoped by the tenant manager. One child, one group."""
    linked = FamilyMembership.all_objects.filter(ended_at__isnull=True).values("student_id")
    students = (
        Student.objects.filter(status=StudentStatus.ACTIVE)
        .exclude(pk__in=linked)
        .select_related("school_class", "branch", "school")
        .order_by("id")
    )
    by_phone, by_email = defaultdict(list), defaultdict(list)
    for student in students:
        if student.parent_phone:
            by_phone[student.parent_phone].append(student)
        if student.parent_email:
            by_email[student.parent_email.strip().lower()].append(student)

    rejected = set(
        MatchDecision.objects.filter(decision="reject").values_list("signature", flat=True)
    )
    found, used = [], set()
    for signal, table in (("phone", by_phone), ("email", by_email)):
        for value, members in table.items():
            members = [s for s in members if s.pk not in used]
            if len(members) < 2:
                continue
            signature = _signature(members)
            if signature in rejected:
                continue
            used.update(s.pk for s in members)
            found.append(Group(signal, value, tuple(members), signature))
    return sorted(found, key=lambda g: len(g.students))[:LIMIT]


def reject(group: Group, *, by, school) -> None:
    MatchDecision.all_objects.update_or_create(
        school=school, signature=group.signature,
        defaults={"decision": "reject", "decided_by": by},
    )
