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

from dataclasses import dataclass

from .models import Plan


@dataclass(frozen=True)
class PlanOffer:
    plan: str
    name: str
    #: Naira per term. ``None`` means quoted, not listed.
    price_naira: int | None
    #: The student range, worded the way the pricing page words it.
    students: str
    #: Whether the billing screen can take payment for it.
    orderable: bool

    @property
    def price_display(self) -> str:
        if self.price_naira is None:
            return "Quoted"
        if self.price_naira == 0:
            return "Free"
        return f"₦{self.price_naira:,}"


OFFERS: tuple[PlanOffer, ...] = (
    PlanOffer(Plan.FREE, "Free", 0, "Up to 100 students", orderable=False),
    PlanOffer(Plan.STARTER, "Starter", 45_000, "Up to 200 students", orderable=True),
    PlanOffer(Plan.GROWTH, "Growth", 90_000, "Up to 600 students", orderable=True),
    PlanOffer(
        Plan.PROFESSIONAL, "Professional", 180_000, "Up to 1,200 students", orderable=True
    ),
    PlanOffer(
        Plan.ENTERPRISE, "Enterprise", None, "More than 1,200 students", orderable=False
    ),
)


def offer_for(plan: str) -> PlanOffer | None:
    """The offer for a plan value, or ``None`` for anything not on the page."""
    return next((offer for offer in OFFERS if offer.plan == plan), None)
