#!/usr/bin/env python3
"""Lay out delivery-review-YYYY-MM.pptx from the same pack.json the workbook was built from.

    python scripts/build_deck.py pack.json --out delivery-review-2026-08.pptx

Eight slides, the same eight every month, in the same order. Slide 8 exists whether or not
the period has findings, because "no issues this period" is a finding and a deck that drops
the caveats slide when it has nothing to say has trained its reader to skim it.

Same contract as build_workbook.py, and for the same reason: **it does not compute
anything.** Every number on a slide is a field copied out of a tool response. There are no
sums here, no ratios, no averages. Portfolio totals and margin by client come from the
portfolio block on list_engagements, which computes them in SQL, and they exist for exactly
this reason — a deck has no formula layer to hide behind, so the alternative to a SQL rollup
was this file quietly adding up eighteen rows and becoming a second place figures come from.

Why this reads pack.json rather than the workbook. openpyxl returns None for the computed
value of a formula in a file Excel has never opened, and the workbook is written by openpyxl
moments earlier, so it has no cached values to read. Reading it would mean re-deriving every
figure the formulas were introduced to stop being re-derived. The two artifacts are built
from the same JSON instead, and scripts/check_format.py holds both open at once to assert
that no number reaches a slide that is not in the book.

Voice: declarative, no adjectives on numbers. "Burn is 84%", not "burn is concerningly high
at 84%". The reader decides what is concerning. Wording that turns a filing gap into a
delivery finding is banned outright and checked for by the harness — see BANNED in
scripts/check_pack.py.

Runs in Cowork's code execution sandbox, so the dependencies are python-pptx and the
standard library and nothing else.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

try:
    from pptx import Presentation
    from pptx.chart.data import CategoryChartData
    from pptx.dml.color import RGBColor
    from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.enum.text import PP_ALIGN
    from pptx.util import Emu, Inches, Pt
except ModuleNotFoundError:  # pragma: no cover - the sandbox check, see the note below
    # Deliberately not a bare traceback. Step 6 shipped a Skill whose bundled script never
    # reached Cowork, and the model responded by writing its own substitute and carrying on,
    # so the omission only surfaced on inspection of the output. The failure mode to design
    # against is not this script erroring, it is a run routing around it. Say what is wrong
    # in one line, and say that stopping is the correct outcome.
    sys.stderr.write(
        "error: python-pptx is not available in this environment, so the deck cannot be "
        "built to the house format.\n"
        "Install it with `pip install python-pptx`, or report that the deck could not be "
        "produced. Do not write a substitute deck: the house format is the deliverable, and "
        "a deck laid out differently each month is the thing this skill exists to prevent.\n"
    )
    raise SystemExit(2) from None


# The house format's palette. Muted on purpose — a partner deck that colours its own numbers
# is making an argument, and the numbers are supposed to make it.
INK = RGBColor(0x1F, 0x25, 0x2B)
MUTED = RGBColor(0x5B, 0x66, 0x70)
RULE = RGBColor(0xD5, 0xDB, 0xE0)
AMBER = RGBColor(0x9A, 0x74, 0x0B)
RED = RGBColor(0x9C, 0x00, 0x06)

BURN_AMBER = 70.0
BURN_RED = 90.0

TITLE_SIZE = Pt(30)
HEADING_SIZE = Pt(22)
BODY_SIZE = Pt(13)
SMALL_SIZE = Pt(10.5)

MARGIN = Inches(0.62)
SLIDE_W = Inches(13.333)
SLIDE_H = Inches(7.5)
CONTENT_W = SLIDE_W - 2 * MARGIN

MAX_AT_RISK = 8
MAX_RED_SLIDES = 3

SLIDE_TITLES = (
    "Delivery and margin review",
    "Portfolio summary",
    "Margin by client",
    "Engagements at risk",
    "Data quality and caveats",
)


class PackError(Exception):
    """The input could not produce a defensible deck."""


# --------------------------------------------------------------------------- input


def load_pack(path: Path) -> dict[str, Any]:
    """Read pack.json and refuse anything that would produce a deck with holes in it.

    The portfolio block is required rather than optional, and that is the one refusal worth
    explaining. Slides 2 and 3 are portfolio totals and margin by client. Without the block
    there is no honest way to fill them — the numbers would have to be added up here — so a
    pack that lacks it is a pack this script must not silently work around. The fix is one
    argument: list_engagements(..., include_portfolio=True) on the first page.
    """
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
            "engagements is empty. A pack with no engagements is a failed run, not an empty "
            "month; stop and report rather than writing a deck."
        )

    if not pack.get("portfolio"):
        raise PackError(
            "pack has no portfolio block, so the summary and margin-by-client slides have no "
            "source. Call list_engagements with include_portfolio=true on the first page. Do "
            "not total the engagement rows to fill the gap: those figures are computed in SQL "
            "precisely so that nothing downstream has to compute them."
        )

    if not pack["portfolio"].get("clients"):
        raise PackError("the portfolio block carries no clients, so slide 3 has nothing to chart.")

    return pack


# ------------------------------------------------------------------------- helpers


def fnum(value: Any, dp: int = 0) -> str:
    """A number as the house format prints it: thousands separated, fixed decimals."""
    if value is None:
        return "n/a"
    return f"{float(value):,.{dp}f}"


def fpct(value: Any, dp: int = 1) -> str:
    """A percentage already on the 0..100 scale. Never rescaled here."""
    if value is None:
        return "n/a"
    return f"{float(value):,.{dp}f}%"


def fsigned(value: Any, dp: int = 1, suffix: str = "") -> str:
    """A movement, with its sign always shown.

    Declarative rather than interpreted: "-2.9pp" and not "down 2.9pp" or "a 2.9pp fall".
    The direction is in the sign and the reader can read.
    """
    if value is None:
        return "no prior period"
    return f"{float(value):+,.{dp}f}{suffix}"


def fmoney(value: Any) -> str:
    """Currency to no decimals, per the house format."""
    return "n/a" if value is None else f"{float(value):,.0f}"


def burn_colour(burn_pct: float | None) -> RGBColor:
    if burn_pct is None:
        return INK
    if burn_pct > BURN_RED:
        return RED
    if burn_pct > BURN_AMBER:
        return AMBER
    return INK


def textbox(slide: Any, left: Any, top: Any, width: Any, height: Any) -> Any:
    box = slide.shapes.add_textbox(left, top, width, height)
    frame = box.text_frame
    frame.word_wrap = True
    return frame


def write(
    frame: Any,
    text: str,
    *,
    size: Any = BODY_SIZE,
    bold: bool = False,
    colour: RGBColor = INK,
    first: bool = False,
    align: Any = None,
    space_after: Any = Pt(4),
) -> Any:
    paragraph = frame.paragraphs[0] if first else frame.add_paragraph()
    paragraph.space_after = space_after
    if align is not None:
        paragraph.alignment = align
    run = paragraph.add_run()
    run.text = text
    run.font.size = size
    run.font.bold = bold
    run.font.color.rgb = colour
    return paragraph


def blank_slide(prs: Any) -> Any:
    """A slide with no placeholders at all.

    Layout 6 is the blank one in python-pptx's default template. Placeholder layouts carry
    "Click to edit Master title style" prompts that survive into the file when unfilled, and
    an empty prompt box on slide 8 of a partner deck is exactly the sort of thing that gets
    noticed.
    """
    return prs.slides.add_slide(prs.slide_layouts[6])


def slide_frame(prs: Any, title: str, subtitle: str | None = None) -> Any:
    """A titled slide, with the house rule under the heading."""
    slide = blank_slide(prs)

    head = textbox(slide, MARGIN, Inches(0.45), CONTENT_W, Inches(0.8))
    write(head, title, size=HEADING_SIZE, bold=True, first=True)
    if subtitle:
        write(head, subtitle, size=SMALL_SIZE, colour=MUTED)

    line = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, MARGIN, Inches(1.42), CONTENT_W, Emu(9525))
    line.fill.solid()
    line.fill.fore_color.rgb = RULE
    line.line.fill.background()
    line.shadow.inherit = False

    return slide


def table_of(slide: Any, rows: list[list[str]], top: Any, widths: list[float]) -> Any:
    """A plain table. Header row bold, no fill, one rule under the header."""
    n_rows, n_cols = len(rows), len(rows[0])
    table = slide.shapes.add_table(
        n_rows, n_cols, MARGIN, top, CONTENT_W, Inches(0.32) * n_rows
    ).table

    for i, w in enumerate(widths):
        table.columns[i].width = Emu(int(CONTENT_W * w))

    for r, row in enumerate(rows):
        table.rows[r].height = Inches(0.32)
        for c, value in enumerate(row):
            cell = table.cell(r, c)
            cell.text = ""
            paragraph = cell.text_frame.paragraphs[0]
            paragraph.alignment = PP_ALIGN.RIGHT if c and r else PP_ALIGN.LEFT
            run = paragraph.add_run()
            run.text = value
            run.font.size = SMALL_SIZE
            run.font.bold = r == 0
            run.font.color.rgb = INK if r == 0 else MUTED
            cell.margin_left = cell.margin_right = Inches(0.06)
            cell.margin_top = cell.margin_bottom = Inches(0.02)
    return table


# ---------------------------------------------------------------------------- slides


def slide_1_title(prs: Any, pack: dict[str, Any]) -> None:
    """Period, portfolio size, date generated, and the scoring model version.

    The version is on this slide because the decision record puts it here: a health score is
    only comparable against the model that produced it, and a deck that shows bands without
    saying which calibration produced them cannot be compared to last month's.
    """
    book = pack["portfolio"]
    slide = blank_slide(prs)

    frame = textbox(slide, MARGIN, Inches(2.3), CONTENT_W, Inches(2.4))
    write(frame, SLIDE_TITLES[0], size=TITLE_SIZE, bold=True, first=True, space_after=Pt(10))
    write(
        frame,
        f"{pack['period']}  ·  {book['engagements_active']} active engagements  "
        f"·  {book['clients_active']} clients",
        size=Pt(15),
        colour=MUTED,
        space_after=Pt(26),
    )

    generated = pack.get("generated_at") or dt.date.today().isoformat()
    write(frame, f"Generated {generated}", size=SMALL_SIZE, colour=MUTED)
    write(frame, f"Scoring model {pack.get('scoring_model_version', 'n/a')}", size=SMALL_SIZE, colour=MUTED)
    if pack.get("run_id"):
        write(frame, f"Run {pack['run_id']}", size=SMALL_SIZE, colour=MUTED)


def slide_2_portfolio(prs: Any, pack: dict[str, Any]) -> None:
    """Active engagements, total hours, blended margin, movement against the prior month.

    Every figure on this slide is a field of the portfolio block. None of it is added up
    here, and the movement column is the whole reason the block carries deltas: a month of
    figures with nothing to compare them against is a table, not a review.
    """
    book = pack["portfolio"]
    slide = slide_frame(
        prs, SLIDE_TITLES[1], f"{pack['period']}, active engagements, measured across the whole book"
    )

    rows = [
        ["", "This period", "Movement vs prior month"],
        ["Active engagements", fnum(book["engagements_active"]), fsigned(book.get("engagements_active_delta"), 0)],
        ["Hours to date", fnum(book["hours_to_date_total"], 1), fsigned(book.get("hours_to_date_delta"), 1)],
        ["Ceiling hours", fnum(book["ceiling_hours_total"], 1), ""],
        ["Portfolio burn", fpct(book.get("portfolio_burn_pct")), ""],
        ["Revenue to date", fmoney(book.get("revenue_to_date_total")), ""],
        ["Cost to date", fmoney(book.get("cost_to_date_total")), ""],
        ["Blended margin", fpct(book.get("blended_margin_pct")), fsigned(book.get("blended_margin_delta_pct"), 1, "pp")],
        ["Mean health score", fnum(book.get("mean_health_score"), 1), fsigned(book.get("mean_health_score_delta"), 1)],
        ["In the green band", f"{book.get('engagements_green')} of {book['engagements_active']}", ""],
    ]
    table_of(slide, rows, Inches(1.75), [0.40, 0.30, 0.30])

    frame = textbox(slide, MARGIN, Inches(5.45), CONTENT_W, Inches(1.2))
    write(
        frame,
        "Blended margin is revenue less cost across the book, divided once. Fixed-price "
        "engagements take the fee as revenue; time and materials takes billable value at "
        "rate card.",
        size=SMALL_SIZE,
        colour=MUTED,
        first=True,
    )


def slide_3_margin_by_client(prs: Any, pack: dict[str, Any]) -> None:
    """A native chart, not a picture of one.

    A real chart object means the figures are in the file and a reader can click through to
    them, which is the same argument as the workbook's live formulas one artifact along.
    """
    clients = pack["portfolio"]["clients"]
    slide = slide_frame(
        prs, SLIDE_TITLES[2], "Blended across each client's active engagements, in percent"
    )

    plotted = [c for c in clients if c.get("margin_pct") is not None]

    if not plotted:
        frame = textbox(slide, MARGIN, Inches(2.0), CONTENT_W, Inches(1.0))
        write(frame, "No client has a measurable margin this period.", colour=MUTED, first=True)
        return

    data = CategoryChartData()
    data.categories = [c["client_name"] for c in plotted]
    data.add_series("Margin %", tuple(float(c["margin_pct"]) for c in plotted))

    graphic = slide.shapes.add_chart(
        XL_CHART_TYPE.COLUMN_CLUSTERED, MARGIN, Inches(1.7), CONTENT_W, Inches(4.3), data
    )
    chart = graphic.chart
    chart.has_legend = True
    chart.legend.position = XL_LEGEND_POSITION.BOTTOM
    chart.legend.include_in_layout = False
    chart.font.size = SMALL_SIZE

    frame = textbox(slide, MARGIN, Inches(6.15), CONTENT_W, Inches(0.9))
    lowest = min(plotted, key=lambda c: c["margin_pct"])
    write(
        frame,
        f"Lowest is {lowest['client_name']} at {fpct(lowest['margin_pct'])} across "
        f"{lowest['engagements_active']} engagement(s).",
        size=SMALL_SIZE,
        colour=MUTED,
        first=True,
    )


def at_risk(pack: dict[str, Any]) -> list[dict[str, Any]]:
    """The engagements the pack examined, worst health first.

    The order is health score ascending and then burn descending, both figures the tools
    returned. Nothing here decides what "at risk" means beyond the four triggers that
    already chose the examined set — that decision belongs to scope-escalation.
    """
    burn = pack.get("burn") or {}
    chosen = [e for e in pack["engagements"] if str(e["engagement_id"]) in burn]
    return sorted(
        chosen,
        key=lambda e: (
            e.get("health_score") if e.get("health_score") is not None else 999,
            -(e.get("burn_pct") or 0),
        ),
    )[:MAX_AT_RISK]


def slide_4_at_risk(prs: Any, pack: dict[str, Any]) -> None:
    """One line each, maximum eight."""
    rows_data = at_risk(pack)
    examined = len(pack.get("burn") or {})
    slide = slide_frame(
        prs,
        SLIDE_TITLES[3],
        f"{len(rows_data)} of {examined} engagements examined in detail, "
        f"of {pack['portfolio']['engagements_active']} active",
    )

    if not rows_data:
        frame = textbox(slide, MARGIN, Inches(2.0), CONTENT_W, Inches(1.0))
        write(frame, "No engagement met an examine trigger this period.", colour=MUTED, first=True)
        return

    rows = [["Engagement", "Client", "Burn", "Margin", "Health", "Concentration", "Ends"]]
    for e in rows_data:
        eid = str(e["engagement_id"])
        fin = (pack.get("financials") or {}).get(eid, {})
        rows.append([
            f"{e['engagement_id']}  {e['name']}",
            e["client_name"],
            fpct(e.get("burn_pct")),
            fpct(fin.get("margin_pct")),
            f"{fnum(e.get('health_score'), 1)}  {e.get('health_band') or ''}".strip(),
            fpct(e.get("person_concentration_pct")),
            str(e.get("end_date") or ""),
        ])

    table = table_of(slide, rows, Inches(1.75), [0.30, 0.18, 0.09, 0.09, 0.13, 0.13, 0.11])

    # Burn is the one column the house format colours, at the same thresholds as the
    # workbook's conditional formatting. Nothing else on the slide is coloured: an amber
    # cell is a threshold being crossed, not an opinion.
    for r, e in enumerate(rows_data, start=1):
        colour = burn_colour(e.get("burn_pct"))
        for run in table.cell(r, 2).text_frame.paragraphs[0].runs:
            run.font.color.rgb = colour

    frame = textbox(slide, MARGIN, Inches(5.9), CONTENT_W, Inches(1.1))
    write(
        frame,
        f"Burn above {fpct(BURN_AMBER, 0)} is amber and above {fpct(BURN_RED, 0)} is red, the same "
        "thresholds the workbook applies. Ceilings are never re-baselined, so burn above 100% "
        "stays above 100%.",
        size=SMALL_SIZE,
        colour=MUTED,
        first=True,
    )


def slide_5_to_7_red(prs: Any, pack: dict[str, Any]) -> int:
    """One slide per RED engagement, maximum three. Situation, the numbers, action.

    RED is scope-escalation's decision and that skill is step 8, so an empty exceptions list
    is the ordinary case today rather than an error. It produces one slide saying so, which
    keeps the deck at a fixed length and keeps the reader's expectation that slide 8 is the
    caveats slide.

    Nothing here recommends anything of its own. recommended_action and decision_owner are
    copied from the exception exactly as scope-escalation wrote them, and the owner is the
    engagement lead rather than the agent.
    """
    exceptions = [e for e in (pack.get("exceptions") or []) if str(e.get("flag", "")).upper() == "RED"]
    by_id = {e["engagement_id"]: e for e in pack["engagements"]}

    if not exceptions:
        slide = slide_frame(prs, "Engagements flagged RED", pack["period"])
        frame = textbox(slide, MARGIN, Inches(2.0), CONTENT_W, Inches(2.0))
        write(
            frame,
            "No engagement was flagged RED this period.",
            size=Pt(16),
            first=True,
            space_after=Pt(12),
        )
        write(
            frame,
            "Engagements meeting an examine trigger are on the previous slide with their "
            "figures. A trigger is a reason to look, not a finding.",
            size=SMALL_SIZE,
            colour=MUTED,
        )
        return 1

    for exception in exceptions[:MAX_RED_SLIDES]:
        eid = exception["engagement_id"]
        row = by_id.get(eid, {})
        fin = (pack.get("financials") or {}).get(str(eid), {})
        burn = (pack.get("burn") or {}).get(str(eid), {})

        slide = slide_frame(
            prs,
            f"RED · {row.get('name', 'engagement ' + str(eid))}",
            f"Engagement {eid}  ·  {row.get('client_name', '')}  ·  {row.get('fee_type', '')}",
        )

        frame = textbox(slide, MARGIN, Inches(1.75), CONTENT_W, Inches(1.5))
        write(frame, "Situation", size=SMALL_SIZE, bold=True, colour=MUTED, first=True)
        write(frame, exception.get("situation", ""), size=Pt(14))

        rows = [
            ["", "", "", ""],
            ["Burn", fpct(row.get("burn_pct")), "Margin", fpct(fin.get("margin_pct"))],
            ["Hours to date", fnum(row.get("hours_to_date"), 1), "Ceiling hours", fnum(row.get("ceiling_hours"), 1)],
            ["Hours remaining", fnum(burn.get("hours_remaining"), 1), "Projected overrun", fpct(burn.get("projected_overrun_pct"))],
            ["Health", fnum(row.get("health_score"), 1), "Band", str(row.get("health_band") or "")],
        ]
        table_of(slide, rows, Inches(3.05), [0.25, 0.25, 0.25, 0.25])

        frame = textbox(slide, MARGIN, Inches(4.9), CONTENT_W, Inches(2.0))
        write(frame, "Cause", size=SMALL_SIZE, bold=True, colour=MUTED, first=True)
        write(frame, exception.get("cause", "Not determinable from available data."), size=Pt(13))
        write(frame, "Recommended action", size=SMALL_SIZE, bold=True, colour=MUTED)
        write(frame, exception.get("recommended_action", ""), size=Pt(13))
        write(
            frame,
            f"Decision owner: {exception.get('decision_owner', 'engagement lead')}",
            size=SMALL_SIZE,
            colour=MUTED,
        )

        if burn.get("projection_confidence") == "low":
            write(
                frame,
                f"Projection confidence low — {burn.get('confidence_reason', '')}",
                size=SMALL_SIZE,
                colour=AMBER,
            )

    return len(exceptions[:MAX_RED_SLIDES])


def slide_8_data_quality(prs: Any, pack: dict[str, Any]) -> None:
    """Never omitted, even when clean. "No issues this period" is a finding.

    The coverage note is the one thing on this slide that has to be worded carefully. A week
    below the floor is a filing artifact and the deck says so in those terms — it is not a
    slowdown, a dip or a drop, and the harness checks the slide text for those words.
    """
    summary = pack["time_summary"]
    quality = summary.get("data_quality", {})
    completeness = summary.get("data_completeness", {})
    gaps = [w for w in completeness.get("weeks", []) if w.get("firm_wide_gap")]

    slide = slide_frame(prs, SLIDE_TITLES[4], f"{pack['period']}, across the whole firm")

    rows = [
        ["Record quality", "Count"],
        ["Time entries in the period", fnum(quality.get("entry_count"))],
        ["Filed after the period closed", f"{fnum(quality.get('late_entries'))}  ({fpct(quality.get('late_entry_pct'))})"],
        ["No billable flag set", fnum(quality.get("null_billable"))],
        ["Redundant rows: same person, engagement, day and hours", fnum(quality.get("suspected_duplicates"))],
    ]
    table_of(slide, rows, Inches(1.72), [0.70, 0.30])

    frame = textbox(slide, MARGIN, Inches(3.55), CONTENT_W, Inches(3.2))
    write(frame, "Reporting coverage", size=SMALL_SIZE, bold=True, colour=MUTED, first=True)

    if not gaps:
        write(
            frame,
            f"Every week in the period met the 60% reporting coverage floor. "
            f"Lowest weekly coverage was {fpct(completeness.get('lowest_pct_active_reporting'))}.",
        )
    for week in gaps:
        write(
            frame,
            f"Week of {week['week_start']}: {fpct(week.get('pct_active_reporting'))} of active "
            f"engagements filed time, below the 60% coverage floor. This week is a filing "
            f"artifact. It is excluded from every run rate and projection in this pack, and "
            f"delivery for the week cannot be measured from the data.",
        )

    low = [
        (eid, b) for eid, b in sorted((pack.get("burn") or {}).items())
        if b.get("projection_confidence") == "low"
    ]
    write(frame, "Projections reported with low confidence", size=SMALL_SIZE, bold=True, colour=MUTED)
    if not low:
        write(frame, "Every projection in this pack came back high confidence.")
    for eid, b in low:
        write(frame, f"Engagement {eid}: {b.get('confidence_reason', '')}", size=Pt(12))

    write(
        frame,
        f"Figures come from the BizData tools and are reproduced in "
        f"engagement-book-{pack['period']}.xlsx. Scoring model "
        f"{pack.get('scoring_model_version', 'n/a')}.",
        size=SMALL_SIZE,
        colour=MUTED,
    )


# --------------------------------------------------------------------------- entry


def build(pack: dict[str, Any], out: Path) -> tuple[Path, int, int]:
    """The eight-slide house format, in order.

    On the count, because the spec is in tension with itself and this is where it resolves.
    "Eight slides, same order every month" and "5-7: one slide per red engagement, maximum
    three" cannot both be literally true in a month with one RED engagement. Padding the
    section out to three would give a fixed count at the price of two slides that say
    nothing, which is a worse deck and a worse habit — a reader who learns that some slides
    are filler stops reading all of them.

    So eight is the format's full extent rather than its invariant length. What is fixed, and
    what scripts/check_format.py asserts, is the order: title, portfolio, margin by client,
    at risk, the RED section, then data quality and caveats **always last and always
    present**. The deck runs to six, seven or eight slides.
    """
    prs = Presentation()
    prs.slide_width, prs.slide_height = SLIDE_W, SLIDE_H

    slide_1_title(prs, pack)
    slide_2_portfolio(prs, pack)
    slide_3_margin_by_client(prs, pack)
    slide_4_at_risk(prs, pack)
    reds = slide_5_to_7_red(prs, pack)
    slide_8_data_quality(prs, pack)

    out.parent.mkdir(parents=True, exist_ok=True)
    prs.save(out)
    return out, len(prs.slides), reds


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("pack", type=Path, help="pack.json: the run's tool responses, unmodified.")
    ap.add_argument("--out", type=Path, help="Defaults to delivery-review-<period>.pptx here.")
    args = ap.parse_args()

    try:
        pack = load_pack(args.pack)
    except PackError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    out = args.out or Path(f"delivery-review-{pack['period']}.pptx")
    written, slides, reds = build(pack, out)

    flagged = len([e for e in (pack.get("exceptions") or []) if str(e.get("flag", "")).upper() == "RED"])
    print(
        f"{written}: {slides} slides, "
        f"{len(at_risk(pack))} engagement(s) at risk, "
        f"{len(pack['portfolio']['clients'])} client(s) charted, "
        f"{flagged} flagged RED over {reds} slide(s)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
