"""list_engagements — the triage call.

The one design decision in this file worth explaining out loud, because it is the answer to
"how do you keep an agent from blowing the context window": burn and health are computed in
SQL and come back on the row. The first version of this tool returned engagement metadata
only, which meant deciding what to look at cost a fan-out of thirty get_engagement_burn
calls. Now it costs one.

Step 6 added person_concentration_pct and people_count for the same reason, and the reason
is worth keeping because it was found rather than designed. A connector review filtered on
burn or band examined 8 of 18 engagements and missed mess case 8 entirely — 85% of an
engagement's hours from one person, at 67% burn, in the green band. The filter was right and
the row was too thin to express what it was filtering on.

Step 8 added margin_pct and days_since_last_entry, and found it the same way. scope-escalation
flags an active engagement silent for fourteen days and a fixed-fee engagement under water
beside a healthy burn, and on six of the seventeen fixture seeds neither engagement was ever
examined: both sat in the green band under 70% burn with a live contract and one team, so the
detail call carrying the proof was the call triage had declined to make. Both columns come off
engagement_burn_v1, which the query already reads, and margin_ratio there is the column
engagement_financials_v1 projects — so the triage margin and get_financials' margin are the
same number by construction rather than by agreement.
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
    margin_pct: float | None = Field(
        default=None,
        description=(
            "Fee less cost over fee for fixed price, billable value less cost over billable "
            "value for time and materials. The same figure get_financials returns, from the "
            "same column. Null when a time and materials engagement has billed nothing."
        ),
    )
    days_since_last_entry: int | None = Field(
        default=None,
        description=(
            "Days between the last time entry and the period end. Counts from the start date "
            "when nothing has ever been logged, so it is never null on a live engagement."
        ),
    )
    person_concentration_pct: float | None = Field(
        default=None,
        description=(
            "Share of the period's hours from the single largest contributor. Null when the "
            "engagement logged no hours in the period."
        ),
    )
    people_count: int | None = Field(
        default=None, description="How many people logged time against it in the period."
    )

    health_score: float | None = Field(default=None, description="100 minus weighted risk.")
    health_band: str | None = Field(default=None, description="The band the score falls in.")
    score_delta_vs_prior_period: float | None = Field(
        default=None, description="Movement against this engagement's own prior month."
    )
    top_risk_factor: str | None = Field(
        default=None, description="The component contributing most to the deduction, if any does."
    )


class ClientSummary(BaseModel):
    """One client's share of the book, for the deck's margin-by-client slide."""

    model_config = ConfigDict(extra="forbid")

    client_id: int
    client_name: str
    engagements_active: int
    ceiling_hours: float
    hours_to_date: float
    ceiling_amount: float
    revenue_to_date: float = Field(
        description="Fee for fixed-price engagements, billable value at rate card for time and materials."
    )
    cost_to_date: float
    burn_pct: float | None = None
    margin_pct: float | None = Field(
        default=None,
        description="Blended across this client's active engagements: a ratio of sums, not a mean of ratios.",
    )


class PortfolioSummary(BaseModel):
    """The whole active book as one row, plus one row per client.

    Deliberately not narrowed to the filters applied to the engagement rows it travels with,
    the same way get_time_summary's data_completeness is measured firm-wide rather than over
    the engagements asked about. A portfolio summary computed over a subset is a different
    quantity with the same name.
    """

    model_config = ConfigDict(extra="forbid")

    period_start: dt.date
    period_end: dt.date

    engagements_active: int
    clients_active: int
    engagements_green: int | None = None

    ceiling_hours_total: float
    hours_to_date_total: float
    ceiling_amount_total: float
    revenue_to_date_total: float
    cost_to_date_total: float

    portfolio_burn_pct: float | None = Field(
        default=None, description="Hours to date against ceiling hours across the whole active book."
    )
    blended_margin_pct: float | None = Field(
        default=None,
        description="Revenue less cost over revenue, summed across the book before dividing.",
    )
    mean_health_score: float | None = None

    blended_margin_delta_pct: float | None = Field(
        default=None, description="Movement against the portfolio's own prior month. Null in the first period."
    )
    mean_health_score_delta: float | None = None
    hours_to_date_delta: float | None = None
    engagements_active_delta: int | None = None

    clients: list[ClientSummary]


class ListEngagementsResult(Response):
    engagements: list[Engagement]
    next_cursor: str | None = Field(
        default=None,
        description="Pass back as cursor for the next page. Null when the last page is in hand.",
    )
    as_of_date: dt.date
    period_start: dt.date
    portfolio: PortfolioSummary | None = Field(
        default=None, description="Present only when include_portfolio was set. Identical on every page."
    )


def _portfolio(as_of: dt.date) -> tuple[PortfolioSummary | None, str | None]:
    """The portfolio block, in two queries at two grains.

    Two round trips rather than one, because the alternative is a grouping set returning both
    grains in one result with a nullable client column, and every consumer would then have to
    branch on which shape a row is. get_time_summary already pays for three queries per call
    for the same reason.
    """
    head = db.query("list_engagements_portfolio", {"as_of_date": as_of})
    if not head:
        return None, None

    row = head[0]
    clients = db.query("list_engagements_portfolio_clients", {"as_of_date": as_of})

    return (
        PortfolioSummary(
            **{k: v for k, v in row.items() if k in PortfolioSummary.model_fields},
            clients=[
                ClientSummary(**{k: v for k, v in c.items() if k in ClientSummary.model_fields})
                for c in clients
            ],
        ),
        row.get("scoring_model_version"),
    )


@logged
def list_engagements(
    as_of_date: str,
    status: str | None = None,
    client_id: int | None = None,
    cursor: str | None = None,
    limit: int = 25,
    include_portfolio: bool = False,
    run_id: str | None = None,
) -> ListEngagementsResult:
    """List engagements live at a date with their burn and health, for triage in one call.

    Returns one row per engagement that was contractually live in the month containing
    as_of_date, with burn percentage, health score and band, movement against the
    engagement's own prior month, its top risk factor, and key-person concentration. Use
    this first in any delivery review, and page with next_cursor until returned_count sums
    to total_count: a partial list silently produces a clean-looking pack, and the omitted
    engagement is the one that was in trouble.

    Triage from these rows rather than from burn alone. An engagement can be inside its
    ceiling and in the green band while one person is 85% of its hours, while its contract
    ended part way through the period, while nobody has logged time against it for a month,
    or while it loses money on every hour. None of those four is visible in burn_pct, and
    each has its own column here so that the filter can fire without a detail call.

    Set include_portfolio on the first page to get portfolio totals and margin by client
    alongside the rows: blended margin, total hours, and movement against the prior month,
    all computed in SQL. Those figures are for the review's summary and its margin-by-client
    chart, and they are the only source for them — do not add up the rows to get them.

    Args:
        as_of_date: ISO date. The month containing it is the period measured.
        status: Filter to active, completed or on_hold. All statuses when omitted.
        client_id: Filter to one client. All clients when omitted.
        cursor: next_cursor from a previous page. Start of the list when omitted.
        limit: Rows per page, 1 to 100.
        include_portfolio: Attach the portfolio block. Measured across the whole active
            book regardless of the filters above, and identical on every page, so one page
            needs it.
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

    portfolio, portfolio_version = _portfolio(as_of) if include_portfolio else (None, None)
    version = version or portfolio_version

    return ListEngagementsResult(
        run_id=run_id or new_run_id(),
        total_count=total,
        returned_count=len(engagements),
        scoring_model_version=version,
        engagements=engagements,
        next_cursor=str(rows[-1]["engagement_id"]) if more else None,
        as_of_date=as_of,
        period_start=as_of.replace(day=1),
        portfolio=portfolio,
    )
