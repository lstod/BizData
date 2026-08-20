"""list_engagements — the triage call.

The one design decision in this file worth explaining out loud, because it is the answer to
"how do you keep an agent from blowing the context window": burn and health are computed in
SQL and come back on the row. The first version of this tool returned engagement metadata
only, which meant deciding what to look at cost a fan-out of thirty get_engagement_burn
calls. Now it costs one.
"""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, ConfigDict, Field

from server import db
from server.toollog import logged, new_run_id
from server.tools.common import Response, as_date

MAX_LIMIT = 100


class Engagement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    engagement_id: int = Field(description="Record id. Every row carries one; every claim traces to it.")
    client_name: str
    name: str
    sow_ref: str
    fee_type: str = Field(description="fixed or time_and_materials.")
    status: str
    ceiling_hours: float
    ceiling_amount: float
    start_date: dt.date
    end_date: dt.date
    days_remaining: int = Field(description="Contract days left at the period end. Zero once ended.")
    hours_to_date: float
    burn_pct: float = Field(description="Hours to date against ceiling hours, 0 to 100 and beyond.")

    health_score: float | None = Field(default=None, description="100 minus weighted risk.")
    health_band: str | None = Field(default=None, description="The band the score falls in.")
    score_delta_vs_prior_period: float | None = Field(
        default=None, description="Movement against this engagement's own prior month."
    )
    top_risk_factor: str | None = Field(
        default=None, description="The component contributing most to the deduction, if any does."
    )


class ListEngagementsResult(Response):
    engagements: list[Engagement]
    next_cursor: str | None = Field(
        default=None,
        description="Pass back as cursor for the next page. Null when the last page is in hand.",
    )
    as_of_date: dt.date
    period_start: dt.date


@logged
def list_engagements(
    as_of_date: str,
    status: str | None = None,
    client_id: int | None = None,
    cursor: str | None = None,
    limit: int = 25,
    run_id: str | None = None,
) -> ListEngagementsResult:
    """List engagements live at a date with their burn and health, for triage in one call.

    Returns one row per engagement that was contractually live in the month containing
    as_of_date, with burn percentage, health score and band, movement against the
    engagement's own prior month, and its top risk factor. Use this first in any delivery
    review, and page with next_cursor until returned_count sums to total_count: a partial
    list silently produces a clean-looking pack, and the omitted engagement is the one that
    was in trouble.

    Args:
        as_of_date: ISO date. The month containing it is the period measured.
        status: Filter to active, completed or on_hold. All statuses when omitted.
        client_id: Filter to one client. All clients when omitted.
        cursor: next_cursor from a previous page. Start of the list when omitted.
        limit: Rows per page, 1 to 100.
        run_id: Groups this call with the rest of one run in the tool-call log.
    """
    as_of = as_date(as_of_date, "as_of_date")
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}, got {limit}")

    after = 0
    if cursor:
        try:
            after = int(cursor)
        except ValueError:
            raise ValueError(f"cursor must be a next_cursor from a previous page, got {cursor!r}") from None

    rows = db.query(
        "list_engagements",
        {
            "as_of_date": as_of,
            "status": status,
            "client_id": client_id,
            "cursor": after,
            "limit": limit,
        },
    )

    total = rows[0]["total_count"] if rows else 0
    remaining = rows[0]["remaining_count"] if rows else 0
    version = rows[0]["scoring_model_version"] if rows else None
    engagements = [Engagement(**{k: v for k, v in row.items() if k in Engagement.model_fields}) for row in rows]

    # Null on the last page rather than a cursor that returns nothing, so the paging loop
    # terminates on a value rather than on a comparison the caller has to get right.
    more = remaining > len(rows)

    return ListEngagementsResult(
        run_id=run_id or new_run_id(),
        total_count=total,
        returned_count=len(engagements),
        scoring_model_version=version,
        engagements=engagements,
        next_cursor=str(rows[-1]["engagement_id"]) if more else None,
        as_of_date=as_of,
        period_start=as_of.replace(day=1),
    )
