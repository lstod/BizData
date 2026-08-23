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

Three consequences worth knowing before editing:

  * Ratios arrive already scaled. person_concentration_pct, realisation_pct, late_entry_pct
    and the rest come back from the tools on the 0..100 scale, converted once in SQL. Do not
    rescale them in Python. They are displayed with a 0.0"%" format, which appends the sign
    without multiplying.
  * Three columns are the exception, and they are exceptions in the other direction. Since
    step 7, burn_pct, projected_overrun_pct and margin_pct are Excel formulas rather than
    copied values, so the cell holds a 0..1 ratio and a 0.0% format scales it for display.
    Excel computes them, not this script, and that distinction is the whole point: a
    reviewer clicking the cell sees where the number came from. See HOUSE FORMAT below.
  * The gap week is not filtered out of Time Detail. Those rows are a record of what was
    filed, and the week is marked rather than removed — removing it would hide the thing the
    Data Quality tab is there to report. What the week is excluded from is the run rates,
    and that exclusion already happened in SQL.

Runs in Cowork's code execution sandbox, so the dependencies are openpyxl and the standard
library and nothing else.

HOUSE FORMAT (step 7). The three live formulas, the number formats and the conditional
formatting on burn are defined in plugin/skills/house-format/SKILL.md and implemented in
FORMULAS and FORMATS below. Adding a formula here means adding it there.

The margin formula deviates from the spec and the deviation is deliberate.
spec-a-delivery-margin.md prescribes a single `=(Billable_Value - Cost) / Billable_Value`,
but engagement_burn_v1 branches on fee type — fixed fee takes the fee as revenue — and mess
case 4 is a fixed-fee engagement at negative margin beside a healthy burn. Writing the
spec's version would put a margin in the workbook that disagrees with the one behind the
health score, and would make case 4 stop being a contradiction. The Excel formula carries
the branch.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.formatting.rule import CellIsRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

TABS = ("Summary", "Engagements", "Time Detail", "Exceptions", "Data Quality")

COVERAGE_FLOOR = 60

# The house format's two burn thresholds, on the 0..1 scale the live formula produces.
BURN_AMBER = 0.70
BURN_RED = 0.90

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

# ---------------------------------------------------------------- the house format

# Live formulas on the Engagements tab, step 7. Each entry is a function of a row number and
# a name-to-letter resolver, returning the formula for that cell.
#
# Every one of them is guarded, and the guard is not defensive coding. Unexamined
# engagements carry blank detail cells on purpose — a blank is honest about not having been
# looked at, a zero is not — and an unguarded formula over a blank turns that considered
# blank into 0.0%, which reads as a measured figure. The guard keeps the blank a blank.
FORMULAS = {
    # Burn works on every row: both operands are triage columns, present whether or not the
    # engagement was examined. That matters for the conditional formatting, which then
    # covers the whole column rather than the examined subset.
    "burn_pct": lambda r, at: (
        f'=IF(N({at("ceiling_hours")}{r})>0,'
        f'{at("hours_to_date")}{r}/{at("ceiling_hours")}{r},"")'
    ),
    "projected_overrun_pct": lambda r, at: (
        f'=IF(AND(N({at("ceiling_hours")}{r})>0,{at("projected_total_hours")}{r}<>""),'
        f'MAX(0,{at("projected_total_hours")}{r}-{at("ceiling_hours")}{r})'
        f'/{at("ceiling_hours")}{r},"")'
    ),
    # The fee-type branch, inherited from engagement_burn_v1.margin_ratio rather than
    # re-decided. See the module docstring for why the spec's single formula is not used.
    "margin_pct": lambda r, at: (
        f'=IF({at("cost_to_date")}{r}="","",'
        f'IF({at("fee_type")}{r}="fixed",'
        f'IF(N({at("ceiling_amount")}{r})>0,'
        f'({at("ceiling_amount")}{r}-{at("cost_to_date")}{r})/{at("ceiling_amount")}{r},""),'
        f'IF(N({at("billable_value_to_date")}{r})>0,'
        f'({at("billable_value_to_date")}{r}-{at("cost_to_date")}{r})'
        f'/{at("billable_value_to_date")}{r},"")))'
    ),
}

# Percentages to one decimal, currency to none, dates ISO.
#
# Two percent formats, because there are two kinds of column here. The three formula columns
# hold a 0..1 ratio, so 0.0% scales them for display in the ordinary Excel way. Every other
# _pct column holds a value the tools already scaled to 0..100, so it gets 0.0"%" — a
# literal percent sign appended, no multiplication. Both read as "58.3%" on the tab, and
# neither one rescales anything in Python.
RATIO_PCT = "0.0%"
SCALED_PCT = '0.0"%"'
CURRENCY = "#,##0"
HOURS = "#,##0.0"
ISO_DATE = "yyyy-mm-dd"
ONE_DP = "0.0"

FORMATS = {
    **{name: RATIO_PCT for name in FORMULAS},
    **{
        name: SCALED_PCT
        for name in (
            "person_concentration_pct", "realisation_pct", "late_entry_pct",
            "run_rate_vs_baseline_pct", "payment_behaviour_change_pct",
        )
    },
    **{
        name: CURRENCY
        for name in (
            "ceiling_amount", "invoiced", "paid", "wip_unbilled",
            "cost_to_date", "billable_value_to_date",
        )
    },
    **{
        name: HOURS
        for name in (
            "ceiling_hours", "hours_to_date", "hours_remaining",
            "weekly_run_rate_4wk", "projected_total_hours",
        )
    },
    **{name: ISO_DATE for name in ("start_date", "end_date")},
    **{name: ONE_DP for name in ("health_score", "score_delta_vs_prior_period", "dso_days",
                                 "dso_baseline_days", "days_since_last_entry")},
}

TIME_DETAIL_FORMATS = {
    "week_start": ISO_DATE,
    "hours": HOURS,
    "billable_hours": HOURS,
    "cost": CURRENCY,
    "billable_value": CURRENCY,
    "pct_active_reporting": SCALED_PCT,
}

AMBER_FILL = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")
RED_FILL = PatternFill(start_color="F8CBAD", end_color="F8CBAD", fill_type="solid")
AMBER_FONT = Font(color="7F6000")
RED_FONT = Font(color="9C0006")


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
    """One row per engagement, detail attached where the engagement was examined.

    Three of the columns are Excel formulas rather than copied values — see FORMULAS. They
    are written last, over the top of the copied value, so that the column order stays the
    tools' own and the formula lands in the cell its field name claims.
    """
    burn = {int(k): v for k, v in pack.get("burn", {}).items()}
    financials = {int(k): v for k, v in pack.get("financials", {}).items()}

    def at(name: str) -> str:
        return column_letter(ENGAGEMENT_COLUMNS, name)

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

        for name, formula in FORMULAS.items():
            ws[f"{at(name)}{row}"] = formula(row, at)

    last = len(pack["engagements"]) + 1
    for name, fmt in FORMATS.items():
        letter = at(name)
        for row in range(2, last + 1):
            ws[f"{letter}{row}"].number_format = fmt

    # Amber above 0.70, red above 0.90, in that order — openpyxl writes rules in the order
    # added and Excel stops at the first match, so red has to be tested first or every red
    # cell reads amber.
    burn_range = f"{at('burn_pct')}2:{at('burn_pct')}{last}"
    ws.conditional_formatting.add(
        burn_range,
        CellIsRule(operator="greaterThan", formula=[str(BURN_RED)], fill=RED_FILL, font=RED_FONT),
    )
    ws.conditional_formatting.add(
        burn_range,
        CellIsRule(operator="greaterThan", formula=[str(BURN_AMBER)], fill=AMBER_FILL, font=AMBER_FONT),
    )

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
        # Against 1 and 0.7 rather than 100 and 70: since step 7 the burn column is a live
        # formula holding a 0..1 ratio, displayed as a percentage by its number format. The
        # concentration count below it is still a tool value on the 0..100 scale, so the two
        # thresholds on this tab are deliberately written on different scales.
        ("Over their ceiling", f'=COUNTIF({over("burn_pct")},">1")'),
        ("Above 70% burn", f'=COUNTIF({over("burn_pct")},">{BURN_AMBER}")'),
        ("Not in the green band", f'=COUNTIF({over("health_band")},"<>green")'),
        ("Above 70% person concentration", f'=COUNTIF({over("person_concentration_pct")},">70")'),
        ("Projections reported low confidence", f'=COUNTIF({over("projection_confidence")},"low")'),
        ("Exceptions raised", "=COUNT(Exceptions!A2:A200)"),
    ]

    reporting: list[tuple[str, Any]] = [
        ("Weeks below the coverage floor", completeness.get("weeks_with_gap")),
        ("Lowest weekly reporting coverage", completeness.get("lowest_pct_active_reporting")),
    ]

    # Step 7. The portfolio block from list_engagements, copied straight across. These are
    # the figures the deck's summary slide reads, and they are here because the deck may not
    # contain a number the workbook does not — a claim that has to be checkable against two
    # files rather than asserted in prose.
    #
    # Blended margin is the reason this block cannot be an Excel formula over Engagements.
    # It is a ratio of sums whose numerator switches on fee type, so the sum it needs is of
    # a quantity that is not a column on the tab. It comes from SQL, like every other ratio
    # in the pack.
    #
    # The totals that are on both — hours, ceiling hours — are deliberately left duplicated.
    # The Excel SUM above and the tool figure here are computed independently and have to
    # agree, and scripts/check_format.py asserts that they do.
    book = pack.get("portfolio") or {}
    measured: list[tuple[str, Any]] = []
    if book:
        measured = [
            ("Active engagements", book.get("engagements_active")),
            ("Clients", book.get("clients_active")),
            ("In the green band", book.get("engagements_green")),
            ("Ceiling hours", book.get("ceiling_hours_total")),
            ("Hours to date", book.get("hours_to_date_total")),
            ("Portfolio burn", book.get("portfolio_burn_pct")),
            ("Revenue to date", book.get("revenue_to_date_total")),
            ("Cost to date", book.get("cost_to_date_total")),
            ("Blended margin", book.get("blended_margin_pct")),
            ("Mean health score", book.get("mean_health_score")),
            ("Blended margin, movement vs prior month", book.get("blended_margin_delta_pct")),
            ("Mean health score, movement vs prior month", book.get("mean_health_score_delta")),
            ("Hours to date, movement vs prior month", book.get("hours_to_date_delta")),
        ]

    # Which of those labels take which format. Keyed by label because this tab is label and
    # value pairs rather than columns.
    summary_formats = {
        "Ceiling hours": HOURS,
        "Hours to date": HOURS,
        "Hours to date, movement vs prior month": HOURS,
        "Ceiling amount": CURRENCY,
        "Revenue to date": CURRENCY,
        "Cost to date": CURRENCY,
        "Portfolio burn": SCALED_PCT,
        "Blended margin": SCALED_PCT,
        "Blended margin, movement vs prior month": SCALED_PCT,
        "Mean health score": ONE_DP,
        "Mean health score, movement vs prior month": ONE_DP,
        "Lowest weekly reporting coverage": SCALED_PCT,
    }

    row = 1
    blocks = [
        ("Delivery and margin review", facts),
        ("Portfolio", portfolio),
    ]
    if measured:
        blocks.append(("Portfolio, as the tools measured it", measured))
    blocks.append(("Reporting coverage", reporting))

    for title, block in blocks:
        ws.cell(row=row, column=1, value=title).font = SECTION
        row += 1
        for label, value in block:
            ws.cell(row=row, column=1, value=label)
            cell = ws.cell(row=row, column=2, value=value)
            if fmt := summary_formats.get(label):
                cell.number_format = fmt
            row += 1
        row += 1

    ws.column_dimensions["A"].width = 42
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
    rows = pack["time_summary"].get("rows", [])
    for row, entry in enumerate(rows, start=2):
        week = weeks.get(str(entry.get("week_start")), {})
        write_row(ws, row, [
            *pick(entry, TIME_DETAIL_COLUMNS[:8]),
            week.get("pct_active_reporting"),
            week.get("firm_wide_gap"),
        ])

    for name, fmt in TIME_DETAIL_FORMATS.items():
        letter = column_letter(TIME_DETAIL_COLUMNS, name)
        for row in range(2, len(rows) + 2):
            ws[f"{letter}{row}"].number_format = fmt

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
        cell = ws.cell(row=row, column=2, value=quality.get(field))
        if field.endswith("_pct"):
            cell.number_format = SCALED_PCT
        ws.cell(row=row, column=3, value=field)
        row += 1
    row += 1

    section("Reporting coverage by week")
    coverage_columns = (
        "week_start", "week_end", "engagements_active", "engagements_reporting",
        "pct_active_reporting", "firm_wide_gap",
    )
    coverage_formats = {"week_start": ISO_DATE, "week_end": ISO_DATE, "pct_active_reporting": SCALED_PCT}
    write_header(ws, coverage_columns, row=row)
    row += 1
    for week in completeness.get("weeks", []):
        write_row(ws, row, pick(week, coverage_columns))
        for name, fmt in coverage_formats.items():
            ws.cell(row=row, column=coverage_columns.index(name) + 1).number_format = fmt
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
