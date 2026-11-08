"""What each plan costs, in one place.

``School.plan`` says which plan a school is on. This module says what that plan
costs per term and whether a school can pay for it from the billing screens.
The landing page's pricing section is rendered from this list too, so the price a
school sees advertised is the price it is charged. Enterprise is quoted, so it has
no price here.

Trial is not offered. A school on trial is moved onto a paid plan by the
platform owner, not bought from the billing screen.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .models import Plan


@dataclass(frozen=True)
class PlanOffer:
    plan: str
    name: str
    #: Naira per term. ``None`` means quoted, not listed.
    price_naira: int | None
    #: The student range, worded the way the pricing page words it.
    students: str
    #: The most active students the plan is for. ``None`` means no ceiling.
    max_students: int | None
    #: Whether the billing screen can take payment for it.
    orderable: bool

    @property
    def price_display(self) -> str:
        if self.price_naira is None:
            return "Quoted"
        if self.price_naira == 0:
            return "Free"
        return f"₦{self.price_naira:,}"


#: What each plan is called, in order, and what it cost when prices first moved into the
#: database. The live prices are the PlanPrice rows; these stand in only for a
#: plan that has no row.
DEFAULT_OFFERS: tuple[PlanOffer, ...] = (
    PlanOffer(Plan.FREE, "Free", 0, "Up to 100 students", 100, orderable=False),
    PlanOffer(Plan.STARTER, "Starter", 45_000, "Up to 200 students", 200, orderable=True),
    PlanOffer(Plan.GROWTH, "Growth", 90_000, "Up to 600 students", 600, orderable=True),
    PlanOffer(
        Plan.PROFESSIONAL, "Professional", 180_000, "Up to 1,200 students", 1_200,
        orderable=True,
    ),
    PlanOffer(
        Plan.ENTERPRISE, "Enterprise", None, "More than 1,200 students", None,
        orderable=False,
    ),
)


def offers() -> tuple[PlanOffer, ...]:
    """The plans as they are sold today: the names and order above, with the prices
    and student limits the platform owner set (``PlanPrice``).

    Read fresh each time; it is one small query, and a cached copy could show a price
    after it changed.
    """
    from django.db import DatabaseError

    from .models import PlanPrice

    try:
        stored = {row.plan: row for row in PlanPrice.objects.all()}
    except DatabaseError:
        # Before the table exists (a migration in flight): the defaults, not an error page.
        return DEFAULT_OFFERS
    result = []
    for offer in DEFAULT_OFFERS:
        row = stored.get(offer.plan)
        if row is None:
            result.append(offer)
            continue
        result.append(
            replace(
                offer,
                price_naira=row.price_naira,
                students=row.students_label,
                max_students=row.max_students,
                # Only a positive price can be paid for at the checkout.
                orderable=bool(row.price_naira),
            )
        )
    return tuple(result)


def offer_for(plan: str) -> PlanOffer | None:
    """The offer for a plan value, or ``None`` for anything not on the page."""
    return next((offer for offer in offers() if offer.plan == plan), None)
