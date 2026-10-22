"""What an applicant must supply, by level -- and where that answer comes from.

The client's rule is not one list: a passport photograph is asked of every
applicant, an entrance examination is asked of Primary and Secondary but never
of Nursery, and the supporting documents differ again between a four-year-old
starting Pre-KG and a fifteen-year-old transferring into SSS 2.

So the requirement set is *data*, not a chain of ``if level ==`` in a form.
Three layers answer "what does this applicant owe us?", narrowest first:

1. a branch's own :class:`~apps.admissions.models.RequirementSet` for that level,
2. the school's set for that level (``branch`` null -- one policy, every campus),
3. the defaults below.

A school that has configured nothing still gets a sensible, complete answer,
and a school that disagrees with one line changes that line rather than being
told the platform knows better. :func:`profile_for` is the only function any
caller needs; nothing outside this module should be reading the table.

The defaults are ordinary day-school practice rather than a guess dressed as
one: a child entering Nursery has no previous school to produce results from,
and a child entering JSS 1 from another school carries a transfer letter
because their old school issues one on the way out.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import models

from apps.academics.models import Level


class RequirementKind(models.TextChoices):
    """One thing an applicant can be asked for.

    Documents map to a file field on the applicant (see :data:`DOCUMENT_FIELDS`);
    :attr:`ENTRANCE_EXAM` deliberately does not -- it is satisfied by an
    :class:`~apps.admissions.models.Assessment` row, not by an upload.
    """

    PASSPORT_PHOTO = "passport_photo", "Passport photograph"
    BIRTH_CERTIFICATE = "birth_certificate", "Birth certificate"
    PREVIOUS_RESULTS = "previous_results", "Previous school results"
    TRANSFER_LETTER = "transfer_letter", "Transfer letter / testimonial"
    IMMUNISATION_RECORD = "immunisation_record", "Immunisation record"
    ENTRANCE_EXAM = "entrance_exam", "Entrance examination"


#: Requirement -> the field on ``Applicant`` that satisfies it. A kind absent
#: from this map is not satisfied by an upload; today that is only the exam.
DOCUMENT_FIELDS: dict[str, str] = {
    RequirementKind.PASSPORT_PHOTO: "passport_photo",
    RequirementKind.BIRTH_CERTIFICATE: "birth_certificate",
    RequirementKind.PREVIOUS_RESULTS: "previous_results",
    RequirementKind.TRANSFER_LETTER: "transfer_letter",
    RequirementKind.IMMUNISATION_RECORD: "immunisation_record",
}

#: Every document field, in the order the application form shows them.
DOCUMENT_KINDS: tuple[str, ...] = tuple(DOCUMENT_FIELDS)


@dataclass(frozen=True)
class RequirementLine:
    """One requirement as the rest of the system sees it."""

    kind: str
    is_required: bool

    @property
    def label(self) -> str:
        return RequirementKind(self.kind).label

    @property
    def field_name(self) -> str | None:
        """The applicant field that satisfies it, or ``None`` if it is not a file."""
        return DOCUMENT_FIELDS.get(self.kind)

    @property
    def is_document(self) -> bool:
        return self.kind in DOCUMENT_FIELDS


@dataclass(frozen=True)
class RequirementProfile:
    """The full answer for one level: what is asked, and what is compulsory."""

    level: int
    lines: tuple[RequirementLine, ...]
    #: True when these requirements came from a configured row rather than the
    #: defaults -- the settings screen says which, so a school can tell whether
    #: it is looking at its own policy or ours.
    is_configured: bool = False

    @property
    def level_label(self) -> str:
        try:
            return Level(self.level).label
        except ValueError:
            return "Unknown level"

    @property
    def documents(self) -> tuple[RequirementLine, ...]:
        """Only the lines satisfied by an upload, in form order."""
        return tuple(line for line in self.lines if line.is_document)

    @property
    def required_documents(self) -> tuple[RequirementLine, ...]:
        return tuple(line for line in self.documents if line.is_required)

    @property
    def optional_documents(self) -> tuple[RequirementLine, ...]:
        return tuple(line for line in self.documents if not line.is_required)

    @property
    def requires_assessment(self) -> bool:
        """Does this level sit an entrance exam?

        The single question the assessment and decision steps ask. Nursery says
        no, which is what "skipped for Nursery/KG" means everywhere else.
        """
        return any(
            line.kind == RequirementKind.ENTRANCE_EXAM and line.is_required
            for line in self.lines
        )

    def requires(self, kind: str) -> bool:
        return any(line.kind == kind and line.is_required for line in self.lines)

    def is_met_by(self, applicant, line: RequirementLine) -> bool:
        """Has this applicant satisfied ``line``?

        A document is satisfied by a file, or by the paper being ticked off at
        the desk; the entrance exam by an assessment that has been sat. Asked
        of required and optional lines alike -- a
        checklist that showed nothing against the optional ones would leave
        staff wondering whether the file they uploaded had landed.
        """
        if line.is_document:
            # A file on record, or the paper ticked off at the desk.
            received = getattr(applicant, "requirements_received", None) or ()
            return line.kind in received or bool(
                getattr(applicant, line.field_name, None)
            )
        if line.kind == RequirementKind.ENTRANCE_EXAM:
            return applicant.has_sat_assessment
        return False

    def checklist_for(self, applicant) -> tuple[tuple[RequirementLine, bool], ...]:
        """Every line paired with whether it is satisfied.

        What the detail screen renders. Built here rather than in the template
        because the mapping from a requirement to the field that satisfies it
        belongs with the requirements, not in markup.
        """
        return tuple((line, self.is_met_by(applicant, line)) for line in self.lines)

    def missing_for(self, applicant) -> tuple[RequirementLine, ...]:
        """The compulsory lines this applicant has not satisfied yet.

        A document is satisfied by a file; the entrance exam is satisfied by an
        assessment that has been sat. Optional lines never appear here -- that
        is the whole difference between "we would like this" and "we need this".
        """
        return tuple(
            line
            for line in self.lines
            if line.is_required and not self.is_met_by(applicant, line)
        )


def _lines(**kinds: bool) -> tuple[RequirementLine, ...]:
    """``_lines(passport_photo=True, ...)`` -- readable default declarations."""
    return tuple(
        RequirementLine(kind=kind, is_required=required)
        for kind, required in kinds.items()
    )


#: The fallback policy, one entry per level. Edit here and every school that has
#: not overridden it moves; edit a school's own RequirementSet and only they do.
DEFAULT_REQUIREMENTS: dict[int, tuple[RequirementLine, ...]] = {
    # Nursery and KG: no exam, and no previous school to produce results from.
    # Previous results and a transfer letter are not listed at all rather than
    # listed as optional -- a form that offers a four-year-old's school report
    # as an optional upload is asking for something that does not exist.
    Level.NURSERY: _lines(
        passport_photo=True,
        birth_certificate=True,
        immunisation_record=True,
        entrance_exam=False,
    ),
    # Primary: the exam starts here, and so does a school record worth reading.
    Level.PRIMARY: _lines(
        passport_photo=True,
        birth_certificate=True,
        previous_results=True,
        entrance_exam=True,
        immunisation_record=False,
        transfer_letter=False,
    ),
    # Junior secondary: a child arriving from elsewhere carries a transfer
    # letter, because their previous school issues one on the way out.
    Level.JUNIOR_SECONDARY: _lines(
        passport_photo=True,
        birth_certificate=True,
        previous_results=True,
        transfer_letter=True,
        entrance_exam=True,
        immunisation_record=False,
    ),
    Level.SENIOR_SECONDARY: _lines(
        passport_photo=True,
        birth_certificate=True,
        previous_results=True,
        transfer_letter=True,
        entrance_exam=True,
        immunisation_record=False,
    ),
}

#: What an unknown level falls back to. Narrow on purpose: a passport photo and
#: a birth certificate are asked of everybody, and nothing is invented beyond
#: them for a level the ladder does not have.
FALLBACK = _lines(passport_photo=True, birth_certificate=True, entrance_exam=False)


def default_profile(level: int) -> RequirementProfile:
    """The built-in policy for ``level``, with nothing configured."""
    return RequirementProfile(
        level=level,
        lines=DEFAULT_REQUIREMENTS.get(level, FALLBACK),
        is_configured=False,
    )


def profile_for(level: int, *, school=None, branch=None) -> RequirementProfile:
    """The requirements in force for ``level``, narrowest configuration first.

    Called with no school -- or with one that has configured nothing -- this is
    exactly :func:`default_profile`, which is why every screen can ask for a
    profile without first checking whether the school has an opinion.

    Reads through ``all_objects``: the public enquiry form runs with no tenant
    context at all, and a scoped manager would hand an anonymous parent an
    empty policy rather than the school's own.
    """
    from .models import RequirementSet

    if school is None:
        return default_profile(level)

    school_id = getattr(school, "pk", school)
    branch_id = getattr(branch, "pk", branch)

    candidates = list(
        RequirementSet.all_objects.filter(
            school_id=school_id, level=level, is_active=True
        )
        .filter(models.Q(branch_id=branch_id) | models.Q(branch__isnull=True))
        .prefetch_related("requirements")
    )
    if not candidates:
        return default_profile(level)

    # A campus that has set its own policy overrides the school-wide one; the
    # choice is made explicitly rather than left to the queryset's ordering.
    chosen = next(
        (c for c in candidates if branch_id is not None and c.branch_id == branch_id),
        candidates[0],
    )
    lines = tuple(
        RequirementLine(kind=req.kind, is_required=req.is_required)
        for req in chosen.requirements.all()
    )
    if not lines:
        # A set with no lines is a half-finished configuration, not a school
        # saying "we ask for nothing". Fall back rather than admit on no papers.
        return default_profile(level)
    return RequirementProfile(level=level, lines=lines, is_configured=True)


def profile_for_applicant(applicant) -> RequirementProfile:
    """Convenience: the profile governing one applicant."""
    return profile_for(
        applicant.level, school=applicant.school_id, branch=applicant.branch_id
    )
