#!/usr/bin/env python3
"""Lay out engagement-book-YYYY-MM.xlsx from the tool responses that produced it.

    python scripts/build_workbook.py pack.json --out engagement-book-2026-08.xlsx

The whole design of this script is one constraint: **it does not compute anything.** Every
value it writes is a field copied out of a tool response, and every portfolio total on the
Summary tab is an Excel formula over the Engagements tab rather than a number worked out
here. There is no arithmetic in this file — no sums, no ratios, no averages.

That is not fastidiousness. The pack's central claim is that no figure in front of a partner
was calculated by a model, and the claim has to survive the last step as well as the first
ones. A script that quietly totals a column is a second place figures come from, and the
first time it disagrees with the database nobody will know which one is wrong.

Two consequences worth knowing before editing:

  * Ratios arrive already scaled. burn_pct, margin_pct and person_concentration_pct come
    back from the tools on the 0..100 scale, converted once in SQL. Do not rescale them.
  * The gap week is not filtered out of Time Detail. Those rows are a record of what was
    filed, and the week is marked rather than removed — removing it would hide the thing the
    Data Quality tab is there to report. What the week is excluded from is the run rates,
    and that exclusion already happened in SQL.

Runs in Cowork's code execution sandbox, so the dependencies are openpyxl and the standard
library and nothing else.

Step 7 adds the house format on top of this: live per-row formulas for burn, projected
overrun and margin, conditional formatting, number formats, and the deck. The tab names,
their order and the column layout are settled here so that step 7 is formatting rather than
restructuring.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

TABS = ("Summary", "Engagements", "Time Detail", "Exceptions", "Data Quality")

COVERAGE_FLOOR = 60

# Columns come straight off the tool responses and keep the tools' own field names, so any
# cell in the book can be traced to the call that produced it without a mapping table.
TRIAGE_COLUMNS = (
    "engagement_id", "client_name", "name", "sow_ref", "fee_type", "status",
    "start_date", "end_date", "days_remaining",
    "ceiling_hours", "hours_to_date", "burn_pct", "ceiling_amount",
    "person_concentration_pct", "people_count",
    "health_score", "health_band", "score_delta_vs_prior_period", "top_risk_factor",
)

# Blank on an engagement that did not meet an examine trigger. A blank cell is honest about
# not having been looked at; a zero is not.
BURN_COLUMNS = (
    "hours_remaining", "weekly_run_rate_4wk", "weeks_counted", "weeks_with_no_hours",
    "projected_total_hours", "projected_overrun_pct",
    "projection_confidence", "confidence_reason",
    "run_rate_vs_baseline_pct", "days_since_last_entry", "late_entry_pct",
)

FINANCIAL_COLUMNS = (
    "invoiced", "paid", "wip_unbilled", "cost_to_date", "billable_value_to_date",
    "margin_pct", "realisation_pct",
    "dso_days", "dso_baseline_days", "payment_behaviour_change_pct",
)

ENGAGEMENT_COLUMNS = (*TRIAGE_COLUMNS, "examined", *BURN_COLUMNS, *FINANCIAL_COLUMNS)

TIME_DETAIL_COLUMNS = (
    "engagement_id", "engagement_name", "week_start",
    "hours", "billable_hours", "cost", "billable_value", "entry_count",
    "pct_active_reporting", "firm_wide_gap",
)

EXCEPTION_COLUMNS = (
    "engagement_id", "flag", "situation", "cause", "recommended_action", "decision_owner",
)

QUALITY_METRICS = (
    ("entry_count", "Time entries in the period"),
    ("late_entries", "Filed after the period closed"),
    ("late_entry_pct", "Filed late, as a percentage"),
    ("null_billable", "No billable flag set"),
    ("suspected_duplicates", "Redundant rows: same person, engagement, day and hours"),
    ("duplicate_groups", "Distinct duplicate groups"),
)

HEADER = Font(bold=True)
SECTION = Font(bold=True, size=12)


class PackError(Exception):
    """The input could not produce a defensible workbook."""


# --------------------------------------------------------------------------- input


def load_pack(path: Path) -> dict[str, Any]:
    """Read pack.json and refuse anything that would produce a book with holes in it."""
    try:
        pack = json.loads(path.read_text())
    except FileNotFoundError:
        raise PackError(f"No pack file at {path}") from None
    except json.JSONDecodeError as exc:
        raise PackError(f"{path} is not valid JSON: {exc}") from None

    for key in ("period", "engagements", "time_summary"):
        if key not in pack:
            raise PackError(f"{path} has no {key!r}. Required keys: period, engagements, time_summary")

    if not pack["engagements"]:
        raise PackError(
            "engagements is empty. A pack with no engagements is a failed run, not an empty month; "
            "stop and report rather than writing a workbook."
        )

    summary = pack["time_summary"]
    if summary.get("group_by") != "engagement,week":
        raise PackError(
            f"time_summary was grouped by {summary.get('group_by')!r}. The Time Detail tab needs "
            "'engagement,week'."
        )
    if summary.get("engagement_ids"):
        raise PackError(
            "time_summary was scoped to specific engagement_ids, so its data_completeness block "
            "measures coverage across those engagements rather than across the firm. Call it for "
            "the whole portfolio."
        )
    if summary.get("truncated"):
        raise PackError("time_summary came back truncated, so Time Detail would be a partial record.")

    known = {e["engagement_id"] for e in pack["engagements"]}
    for block in ("burn", "financials"):
        stray = sorted(int(k) for k in pack.get(block, {}) if int(k) not in known)
        if stray:
            raise PackError(
                f"{block} holds engagement(s) {stray} that are not in the engagement list. The list "
                "was paged short, or the two calls disagree about the period."
            )
    return pack


# ------------------------------------------------------------------------- helpers


def write_header(ws: Worksheet, columns: tuple[str, ...], row: int = 1) -> None:
    for col, name in enumerate(columns, start=1):
        cell = ws.cell(row=row, column=col, value=name)
        cell.font = HEADER


def write_row(ws: Worksheet, row: int, values: list[Any]) -> None:
    for col, value in enumerate(values, start=1):
        ws.cell(row=row, column=col, value=value)


def pick(source: dict[str, Any] | None, columns: tuple[str, ...]) -> list[Any]:
    """Field copies, in column order. Absent and null both become an empty cell."""
    if source is None:
        return [None] * len(columns)
    return [source.get(name) for name in columns]


def widths(ws: Worksheet, columns: tuple[str, ...], wide: frozenset[str] | set[str] = frozenset()) -> None:
    for col, name in enumerate(columns, start=1):
        letter = get_column_letter(col)
        ws.column_dimensions[letter].width = 52 if name in wide else max(11, min(len(name) + 3, 26))


def column_letter(columns: tuple[str, ...], name: str) -> str:
    return get_column_letter(columns.index(name) + 1)


def gap_weeks(pack: dict[str, Any]) -> list[dict[str, Any]]:
    weeks = pack["time_summary"].get("data_completeness", {}).get("weeks", [])
    return [w for w in weeks if w.get("firm_wide_gap")]


# ---------------------------------------------------------------------------- tabs


def build_engagements(ws: Worksheet, pack: dict[str, Any]) -> None:
    """One row per engagement, detail attached where the engagement was examined."""
    burn = {int(k): v for k, v in pack.get("burn", {}).items()}
    financials = {int(k): v for k, v in pack.get("financials", {}).items()}

    write_header(ws, ENGAGEMENT_COLUMNS)
    for row, engagement in enumerate(pack["engagements"], start=2):
        eid = engagement["engagement_id"]
        examined = "yes" if eid in burn or eid in financials else "no"
        write_row(ws, row, [
            *pick(engagement, TRIAGE_COLUMNS),
            examined,
            *pick(burn.get(eid), BURN_COLUMNS),
            *pick(financials.get(eid), FINANCIAL_COLUMNS),
        ])

    ws.freeze_panes = "B2"
    widths(ws, ENGAGEMENT_COLUMNS, wide={"confidence_reason", "name"})


def build_summary(ws: Worksheet, pack: dict[str, Any]) -> None:
    """Portfolio totals, every one of them a formula over another tab.

    Nothing on this tab is a pasted value, which is what makes it safe to open in six months
    and trust: change a row on Engagements and these move with it.
    """
    completeness = pack["time_summary"].get("data_completeness", {})
    engagement_rows = len(pack["engagements"])
    last = engagement_rows + 1

    def over(name: str) -> str:
        letter = column_letter(ENGAGEMENT_COLUMNS, name)
        return f"Engagements!{letter}2:{letter}{last}"

    facts: list[tuple[str, Any]] = [
        ("Period", pack["period"]),
        ("Run id", pack.get("run_id")),
        ("Scoring model version", pack.get("scoring_model_version")
            or pack["time_summary"].get("scoring_model_version")),
    ]
    if pack.get("generated_at"):
        facts.append(("Generated", pack["generated_at"]))

    portfolio: list[tuple[str, Any]] = [
        # COUNT rather than COUNTA throughout, because it counts numbers and ignores text.
        # The Exceptions tab carries a sentence in A2 when nothing was flagged, and COUNTA
        # would report that sentence as one exception.
        ("Engagements in the book", f"=COUNT({over('engagement_id')})"),
        ("Examined in detail", f'=COUNTIF({over("examined")},"yes")'),
        ("Ceiling hours", f"=SUM({over('ceiling_hours')})"),
        ("Hours to date", f"=SUM({over('hours_to_date')})"),
        ("Ceiling amount", f"=SUM({over('ceiling_amount')})"),
        ("Over their ceiling", f'=COUNTIF({over("burn_pct")},">100")'),
        ("Above 70% burn", f'=COUNTIF({over("burn_pct")},">70")'),
        ("Not in the green band", f'=COUNTIF({over("health_band")},"<>green")'),
        ("Above 70% person concentration", f'=COUNTIF({over("person_concentration_pct")},">70")'),
        ("Projections reported low confidence", f'=COUNTIF({over("projection_confidence")},"low")'),
        ("Exceptions raised", "=COUNT(Exceptions!A2:A200)"),
    ]

    reporting: list[tuple[str, Any]] = [
        ("Weeks below the coverage floor", completeness.get("weeks_with_gap")),
        ("Lowest weekly reporting coverage", completeness.get("lowest_pct_active_reporting")),
    ]

    row = 1
    for title, block in (
        ("Delivery and margin review", facts),
        ("Portfolio", portfolio),
        ("Reporting coverage", reporting),
    ):
        ws.cell(row=row, column=1, value=title).font = SECTION
        row += 1
        for label, value in block:
            ws.cell(row=row, column=1, value=label)
            ws.cell(row=row, column=2, value=value)
            row += 1
        row += 1

    ws.column_dimensions["A"].width = 38
    ws.column_dimensions["B"].width = 30


def build_time_detail(ws: Worksheet, pack: dict[str, Any]) -> None:
    """get_time_summary grouped by engagement and week, with each week's coverage beside it.

    The coverage columns are the reason this tab is not just a dump. A reader who totals
    hours by week needs to know which weeks the firm was filing in, and carrying that on the
    row is what stops the gap week being read as a delivery figure.
    """
    weeks = {
        str(w["week_start"]): w
        for w in pack["time_summary"].get("data_completeness", {}).get("weeks", [])
    }

    write_header(ws, TIME_DETAIL_COLUMNS)
    for row, entry in enumerate(pack["time_summary"].get("rows", []), start=2):
        week = weeks.get(str(entry.get("week_start")), {})
        write_row(ws, row, [
            *pick(entry, TIME_DETAIL_COLUMNS[:8]),
            week.get("pct_active_reporting"),
            week.get("firm_wide_gap"),
        ])

    ws.freeze_panes = "A2"
    widths(ws, TIME_DETAIL_COLUMNS, wide={"engagement_name"})


def build_exceptions(ws: Worksheet, pack: dict[str, Any]) -> None:
    """Only the engagements scope-escalation flagged. An empty tab is a finding, not a gap."""
    exceptions = pack.get("exceptions") or []

    write_header(ws, EXCEPTION_COLUMNS)
    for row, exception in enumerate(exceptions, start=2):
        write_row(ws, row, pick(exception, EXCEPTION_COLUMNS))

    if not exceptions:
        ws.cell(row=2, column=1, value="No engagement was flagged by scope-escalation this period.")

    ws.freeze_panes = "A2"
    widths(ws, EXCEPTION_COLUMNS, wide={"situation", "cause", "recommended_action"})


def build_data_quality(ws: Worksheet, pack: dict[str, Any]) -> None:
    """The two blocks the tools keep separate, kept separate here too.

    data_quality is individual bad records. data_completeness is whether the period can be
    read at all. Collapsing them is how a firm-wide filing gap gets reported as a delivery
    problem, so the tab has them as two sections with the data notes in between.
    """
    quality = pack["time_summary"].get("data_quality", {})
    completeness = pack["time_summary"].get("data_completeness", {})
    burn = {int(k): v for k, v in pack.get("burn", {}).items()}
    row = 1

    def section(title: str) -> None:
        nonlocal row
        ws.cell(row=row, column=1, value=title).font = SECTION
        row += 1

    section("Record quality — entries in the period")
    for field, label in QUALITY_METRICS:
        ws.cell(row=row, column=1, value=label)
        ws.cell(row=row, column=2, value=quality.get(field))
        ws.cell(row=row, column=3, value=field)
        row += 1
    row += 1

    section("Reporting coverage by week")
    coverage_columns = (
        "week_start", "week_end", "engagements_active", "engagements_reporting",
        "pct_active_reporting", "firm_wide_gap",
    )
    write_header(ws, coverage_columns, row=row)
    row += 1
    for week in completeness.get("weeks", []):
        write_row(ws, row, pick(week, coverage_columns))
        row += 1
    row += 1

    section("Data notes")
    gaps = gap_weeks(pack)
    if not gaps:
        ws.cell(
            row=row, column=1,
            value=f"Every week in the period met the {COVERAGE_FLOOR}% reporting coverage floor.",
        )
        row += 1
    for week in gaps:
        note = (
            f"Week of {week['week_start']}: {week['pct_active_reporting']}% of active engagements "
            f"filed time, below the {COVERAGE_FLOOR}% coverage floor. This week is a filing "
            f"artifact. It is excluded from every run rate and projection in this workbook, and "
            f"delivery for the week cannot be measured from the data."
        )
        cell = ws.cell(row=row, column=1, value=note)
        cell.alignment = Alignment(wrap_text=True, vertical="top")
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=6)
        ws.row_dimensions[row].height = 46
        row += 1
    row += 1

    section("Projections reported with low confidence")
    low = [(eid, b) for eid, b in sorted(burn.items()) if b.get("projection_confidence") == "low"]
    if not low:
        ws.cell(row=row, column=1, value="Every projection in this pack came back high confidence.")
        row += 1
    else:
        write_header(ws, ("engagement_id", "projection_confidence", "confidence_reason"), row=row)
        row += 1
        for eid, b in low:
            write_row(ws, row, [eid, b.get("projection_confidence"), b.get("confidence_reason")])
            row += 1

    ws.column_dimensions["A"].width = 48
    for letter in ("B", "C", "D", "E", "F"):
        ws.column_dimensions[letter].width = 22


# --------------------------------------------------------------------------- entry


def build(pack: dict[str, Any], out: Path) -> Path:
    wb = Workbook()
    wb.remove(wb.active)
    sheets = {name: wb.create_sheet(name) for name in TABS}

    # Engagements first: Summary's formulas reference its rows, so its shape has to be
    # settled before they are written.
    build_engagements(sheets["Engagements"], pack)
    build_summary(sheets["Summary"], pack)
    build_time_detail(sheets["Time Detail"], pack)
    build_exceptions(sheets["Exceptions"], pack)
    build_data_quality(sheets["Data Quality"], pack)

    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("pack", type=Path, help="pack.json: the run's tool responses, unmodified.")
    ap.add_argument("--out", type=Path, help="Defaults to engagement-book-<period>.xlsx here.")
    args = ap.parse_args()

    try:
        pack = load_pack(args.pack)
    except PackError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    out = args.out or Path(f"engagement-book-{pack['period']}.xlsx")
    written = build(pack, out)

    gaps = len(gap_weeks(pack))
    print(
        f"{written}: {len(pack['engagements'])} engagements, "
        f"{len(pack['time_summary'].get('rows', []))} time detail rows, "
        f"{len(pack.get('exceptions') or [])} exception(s), "
        f"{gaps} week(s) below the coverage floor"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
