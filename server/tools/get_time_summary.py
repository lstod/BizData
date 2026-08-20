"""get_time_summary — forty thousand entries in, tens of rows out.

Two blocks ride alongside the aggregated rows, and they are two rather than one on purpose:

    data_quality       individual bad records — filed late, no billable flag, duplicated
    data_completeness  whether each week can be read at all, firm-wide

Those are different questions. An engagement can have flawless records for a week nobody
filed against. Collapsing them into one block is how a firm-wide filing gap gets reported
as a delivery slowdown, which is the exact mistake mess case 7 exists to catch.
"""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, ConfigDict, Field

from server import db
from server.toollog import logged, new_run_id
from server.tools.common import Response, as_date

MAX_ROWS = 2000

WEEK = "date_trunc('week', t.entry_date)::date"

# The five documented group_by modes, each as the pair of SQL fragments
# db/sql/get_time_summary.sql substitutes. This dictionary is the whitelist: a group_by
# argument selects a key and nothing from the argument reaches the SQL, so the one place
# text is interpolated into a query in this repository cannot be reached from the wire.
#
# Every mode selects the same six dimension columns, nulling the ones it is not grouped by,
# so all five share one response shape and a caller never branches on what it asked for.
GROUPINGS: dict[str, tuple[str, str]] = {
    "engagement": (
        """t.engagement_id,
            e.name                as engagement_name,
            null::int             as person_id,
            null::text            as person_name,
            null::text            as role,
            null::date            as week_start""",
        "t.engagement_id, e.name",
    ),
    "person": (
        """null::int             as engagement_id,
            null::text            as engagement_name,
            t.person_id,
            pe.name               as person_name,
            pe.role,
            null::date            as week_start""",
        "t.person_id, pe.name, pe.role",
    ),
    "week": (
        f"""null::int             as engagement_id,
            null::text            as engagement_name,
            null::int             as person_id,
            null::text            as person_name,
            null::text            as role,
            {WEEK}                as week_start""",
        WEEK,
    ),
    "role": (
        """null::int             as engagement_id,
            null::text            as engagement_name,
            null::int             as person_id,
            null::text            as person_name,
            pe.role,
            null::date            as week_start""",
        "pe.role",
    ),
    "engagement,week": (
        f"""t.engagement_id,
            e.name                as engagement_name,
            null::int             as person_id,
            null::text            as person_name,
            null::text            as role,
            {WEEK}                as week_start""",
        f"t.engagement_id, e.name, {WEEK}",
    ),
}


class TimeRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    engagement_id: int | None = None
    engagement_name: str | None = None
    person_id: int | None = None
    person_name: str | None = None
    role: str | None = None
    week_start: dt.date | None = None

    hours: float
    billable_hours: float
    cost: float
    billable_value: float
    entry_count: int


class DataQuality(BaseModel):
    """Individual records that are wrong. Mess cases 1, 5 and 2."""

    model_config = ConfigDict(extra="forbid")

    entry_count: int
    late_entries: int = Field(description="Filed after the period closed.")
    late_entry_pct: float | None = None
    null_billable: int = Field(description="Nobody set the billable flag.")
    suspected_duplicates: int = Field(
        description="Redundant rows: same person, engagement, day and hours, filed more than once."
    )
    duplicate_groups: int


class WeekCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    week_start: dt.date
    week_end: dt.date
    engagements_active: int
    engagements_reporting: int
    pct_active_reporting: float | None = None
    firm_wide_gap: bool = Field(
        description="True below 60% coverage. The week is a filing artifact, not a delivery signal."
    )


class DataCompleteness(BaseModel):
    """Whether the period can be read at all. Firm-wide, and deliberately not scoped to the
    engagements asked about: three of eighteen filing looks like full coverage to a caller
    who asked about exactly those three."""

    model_config = ConfigDict(extra="forbid")

    weeks: list[WeekCoverage]
    weeks_with_gap: int
    lowest_pct_active_reporting: float | None = None


class TimeSummary(Response):
    model_config = ConfigDict(extra="forbid")

    group_by: str
    period_start: dt.date
    period_end: dt.date
    engagement_ids: list[int] | None = None
    truncated: bool = Field(
        description="True when total_count exceeds the rows returned and the response was capped."
    )
    rows: list[TimeRow]
    data_quality: DataQuality
    data_completeness: DataCompleteness


@logged
def get_time_summary(
    period_start: str,
    period_end: str,
    group_by: str = "engagement",
    engagement_ids: list[int] | None = None,
    run_id: str | None = None,
) -> TimeSummary:
    """Aggregate time entries over a window, with data quality and coverage alongside.

    Returns hours, billable hours, cost, billable value and entry count per group, grouped
    by engagement, person, week, role, or engagement and week together. One call covers
    every engagement; do not call it once per engagement. Alongside the rows it returns a
    data_quality block of individually bad records and a data_completeness block of weekly
    reporting coverage across the whole firm. Check data_completeness before analysing
    anything: a week below 60% coverage is a filing artifact and must be excluded from every
    run rate and projection, reported as a data note, and never described as a delivery
    slowdown.

    Args:
        period_start: ISO date, first day of the window.
        period_end: ISO date, last day of the window.
        group_by: engagement, person, week, role, or "engagement,week".
        engagement_ids: Limit to these engagements. Every engagement when omitted.
        run_id: Groups this call with the rest of one run in the tool-call log.
    """
    start = as_date(period_start, "period_start")
    end = as_date(period_end, "period_end")
    if end < start:
        raise ValueError(f"period_end {end} is before period_start {start}")

    key = group_by.replace(" ", "")
    if key not in GROUPINGS:
        raise ValueError(f"group_by must be one of {', '.join(GROUPINGS)}; got {group_by!r}")
    group_select, group_by_sql = GROUPINGS[key]

    ids = list(engagement_ids) if engagement_ids else None
    window = {"period_start": start, "period_end": end, "engagement_ids": ids}

    rows = db.query(
        "get_time_summary",
        {**window, "max_rows": MAX_ROWS},
        group_select=group_select,
        group_by=group_by_sql,
    )
    quality = db.query("get_time_summary_quality", window)[0]
    weeks = db.query("get_time_summary_completeness", {"period_start": start, "period_end": end})

    total = rows[0]["total_count"] if rows else 0
    coverage = [WeekCoverage(**w) for w in weeks]
    reporting = [w.pct_active_reporting for w in coverage if w.pct_active_reporting is not None]

    return TimeSummary(
        run_id=run_id or new_run_id(),
        total_count=total,
        returned_count=len(rows),
        scoring_model_version=quality["scoring_model_version"],
        group_by=key,
        period_start=start,
        period_end=end,
        engagement_ids=ids,
        truncated=total > len(rows),
        rows=[TimeRow(**{k: v for k, v in r.items() if k in TimeRow.model_fields}) for r in rows],
        data_quality=DataQuality(**{k: v for k, v in quality.items() if k in DataQuality.model_fields}),
        data_completeness=DataCompleteness(
            weeks=coverage,
            weeks_with_gap=sum(1 for w in coverage if w.firm_wide_gap),
            lowest_pct_active_reporting=min(reporting) if reporting else None,
        ),
    )
