"""get_financials — what one engagement billed, collected and cost.

Every ratio in here is computed in SQL. That is not a style preference: no percentage in a
partner deck should ever have been calculated by a model, and the reliable way to ensure
that is never to hand the model the operands.

payment_behaviour_change_pct is the port from spec B. DSO against the client's own history
rather than a global threshold, because a client that has always paid on day 45 is not a
risk and one that used to pay on day 10 and now pays on day 40 is.
"""

from __future__ import annotations

import datetime as dt

from pydantic import ConfigDict, Field

from server import db
from server.toollog import logged, new_run_id
from server.tools.common import Response, as_period_start, one_row


class Financials(Response):
    model_config = ConfigDict(extra="forbid")

    engagement_id: int
    name: str
    client_name: str
    fee_type: str

    period_start: dt.date
    period_end: dt.date
    as_of: dt.date

    invoiced: float = Field(description="Issued and paid invoices to date, void excluded.")
    paid: float
    wip_unbilled: float = Field(
        description="Delivered but not yet invoiced. Negative where billing has run ahead of delivery."
    )
    cost_to_date: float
    billable_value_to_date: float
    ceiling_amount: float

    margin_pct: float | None = Field(
        default=None,
        description="Fixed fee takes the fee as revenue; time and materials takes billable value at rate card.",
    )
    realisation_pct: float | None = Field(
        default=None, description="Billable value as a share of every hour delivered at rate card."
    )
    dso_days: float | None = Field(default=None, description="This client's mean days to pay, last 90 days.")
    dso_baseline_days: float | None = Field(default=None, description="The nine months before that.")
    payment_behaviour_change_pct: float | None = Field(
        default=None,
        description="Current DSO against this client's own baseline. Null where there is no history to compare.",
    )


@logged
def get_financials(
    engagement_id: int,
    period: str,
    run_id: str | None = None,
) -> Financials:
    """Invoiced, paid, unbilled work in progress, margin and payment behaviour for one engagement.

    Returns cumulative billing and cost through the period with margin and realisation
    computed in SQL, plus days sales outstanding measured against this client's own payment
    history rather than a fixed threshold. Call it for engagements over 70% burn or not in
    the green band, alongside get_engagement_burn. Margin and burn are measured
    independently and are not reconciled against each other: an engagement can show healthy
    burn and negative margin at the same time, and both are true.

    Args:
        engagement_id: From list_engagements.
        period: YYYY-MM, or any ISO date inside the month wanted.
        run_id: Groups this call with the rest of one run in the tool-call log.
    """
    start = as_period_start(period)
    rows = db.query("get_financials", {"engagement_id": engagement_id, "period": start})
    row = one_row(rows, f"financials for engagement {engagement_id} in {start:%Y-%m}")

    return Financials(
        run_id=run_id or new_run_id(),
        total_count=1,
        returned_count=1,
        **{k: v for k, v in row.items() if k in Financials.model_fields and k != "run_id"},
    )
