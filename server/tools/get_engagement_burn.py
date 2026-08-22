"""get_engagement_burn — one engagement's hours against its ceiling, and the projection.

The important thing in this file is what it does not do. It does not decide whether the
projection is worth acting on, and it does not soften one. It reports the projection and,
beside it, whether the data underneath the projection can carry it — projection_confidence,
with the reason attached, computed in SQL.

That is the single best design decision in the spec: a projection the agent should not
trust is labelled by the tool rather than inferred by the model. An unlabelled
low-confidence projection is a defect; a labelled one is a finding.
"""

from __future__ import annotations

import datetime as dt

from pydantic import ConfigDict, Field

from server import db
from server.toollog import logged, new_run_id
from server.tools.common import Response, as_date, one_row


class EngagementBurn(Response):
    model_config = ConfigDict(extra="forbid")

    engagement_id: int
    name: str
    client_name: str
    sow_ref: str
    fee_type: str
    status: str

    period_start: dt.date
    period_end: dt.date
    as_of: dt.date = Field(
        description="Where the measurement stands: the period end, or the contract end if that came first."
    )

    hours_to_date: float
    ceiling_hours: float
    burn_pct: float
    hours_remaining: float = Field(
        description="Ceiling hours less hours to date. Negative once the ceiling is passed, and left negative."
    )
    days_remaining: int

    weekly_run_rate_4wk: float | None = Field(
        default=None,
        description="Mean hours over the trailing four readable, complete, in-contract weeks.",
    )
    weeks_counted: int | None = Field(
        default=None, description="How many weeks that mean rests on. Fewer than three is not a run rate."
    )
    weeks_with_no_hours: int | None = Field(
        default=None, description="Of those weeks, how many had no time logged against this engagement."
    )
    projected_total_hours: float | None = None
    projected_overrun_pct: float | None = Field(
        default=None, description="Projected hours above the ceiling, as a percentage of it. Never re-baselined."
    )

    projection_confidence: str = Field(description="high or low.")
    confidence_reason: str = Field(description="Why, in words, whichever way it went.")

    person_concentration_pct: float | None = Field(
        default=None, description="Share of the period's hours from the single largest contributor."
    )
    people_count: int | None = None
    run_rate_vs_baseline_pct: float | None = Field(
        default=None,
        description="This period's run rate against this engagement's own trailing baseline, not a portfolio average.",
    )
    baseline_weekly_run_rate: float | None = None
    baseline_weeks_counted: int | None = None

    last_entry_date: dt.date | None = None
    days_since_last_entry: int
    period_entry_count: int
    late_entry_count: int
    late_entry_pct: float | None = None


@logged
def get_engagement_burn(
    engagement_id: int,
    as_of_date: str,
    run_id: str | None = None,
) -> EngagementBurn:
    """Hours against the SOW ceiling for one engagement, with a labelled projection.

    Returns hours to date, burn percentage, the trailing four-week run rate, the projected
    total and overrun, key-person concentration, and the run rate against this engagement's
    own baseline. Crucially it also returns projection_confidence with a reason: low when
    the trailing weeks contain a gap, when fewer than three readable weeks are available, or
    when more than 10% of the period's entries were filed after the period closed. Where
    confidence is low, report the projection with its reason attached rather than acting on
    it.

    Call this for an engagement over 70% burn, outside the green band, above 70% person
    concentration, or whose contract ended inside the period, rather than for every
    engagement. All four are readable from list_engagements. Burn and band alone are not
    enough: an engagement can sit inside its ceiling in the green band while one person is
    most of its delivery.

    Args:
        engagement_id: From list_engagements.
        as_of_date: ISO date. The month containing it is the period measured.
        run_id: Groups this call with the rest of one run in the tool-call log.
    """
    as_of = as_date(as_of_date, "as_of_date")
    rows = db.query("get_engagement_burn", {"engagement_id": engagement_id, "as_of_date": as_of})
    row = one_row(rows, f"burn for engagement {engagement_id} in the month of {as_of}")

    return EngagementBurn(
        run_id=run_id or new_run_id(),
        total_count=1,
        returned_count=1,
        **{k: v for k, v in row.items() if k in EngagementBurn.model_fields and k != "run_id"},
    )
