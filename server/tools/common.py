"""Shared shapes and argument handling for the four read tools.

Two things live here rather than being repeated four times: the base response, which
carries the fields every tool response has to have, and the small amount of argument
normalising that has to happen before a value reaches SQL.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


class Response(BaseModel):
    """What every tool response carries, whatever it is a response to.

    total_count and returned_count are on single-row responses too, where they are both 1.
    Making them optional would mean the tool-call log has a field that is sometimes there,
    and a log line with a conditional shape is one project #2 has to write a branch for.

    scoring_model_version is on every response because a figure is only comparable against
    the version of the model that produced it. It comes out of the database on every call
    rather than being cached in the process: the weights are data precisely so they can
    change without a redeploy, and a cached version string would quietly outlive the
    change.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(description="Groups every tool call belonging to one run.")
    total_count: int = Field(description="Rows matching the request, before any paging.")
    returned_count: int = Field(description="Rows in this response.")
    scoring_model_version: str | None = Field(
        default=None,
        description="The scoring model in force when this answer was computed.",
    )


def as_date(value: str | dt.date, field: str) -> dt.date:
    """Accept an ISO date or a date, reject anything else with the field named."""
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError:
        raise ValueError(f"{field} must be an ISO date such as 2026-08-31, got {value!r}") from None


def as_period_start(value: str, field: str = "period") -> dt.date:
    """Accept YYYY-MM or a full ISO date, return the first day of that month."""
    text = str(value)
    if PERIOD_RE.match(text):
        return dt.date.fromisoformat(f"{text}-01")
    return as_date(text, field).replace(day=1)


def month_end(first_of_month: dt.date) -> dt.date:
    nxt = (first_of_month.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    return nxt - dt.timedelta(days=1)


def one_row(rows: list[dict[str, Any]], what: str) -> dict[str, Any]:
    """Unwrap a single-row query, turning an empty result into a readable error.

    The empty case is nearly always the same mistake — an engagement that was not
    contractually live in the period asked about — so the message says that rather than
    leaving a caller to wonder whether the id was wrong.
    """
    if not rows:
        raise ValueError(
            f"No {what}. The engagement may not have been contractually live in that "
            f"period, or the period may be outside the seeded window."
        )
    return rows[0]
