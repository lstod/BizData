#!/usr/bin/env python3
"""Step 7's Done-when conditions, as assertions.

    scripts/check_format.py                  every fixture seed, reseeding each
    scripts/check_format.py --seed 42 -v     one seed, against the database as it stands
    scripts/check_format.py --keep /tmp/out  leave the workbook and deck behind to open

check_tools.py asserts things about the tools and check_pack.py asserts things about what the
pack contains. This asserts things about its *shape*: that the three live columns really are
formulas rather than numbers, that the formats and the conditional formatting are on the
cells, that the deck's slides are in the house order, and that no figure reached a slide
without also being in the workbook.

Everything is re-read from the two files on disk. Nothing is asserted from the data that
produced them, which is the only way to catch a builder that computed the right answer and
then wrote it into the wrong cell.

Two assertions carry the step:

    every row of burn_pct, projected_overrun_pct and margin_pct holds a formula string
    every number on a slide is in the workbook or in the pack it was built from

The first is worth stating because check_pack.py's existing formula assertion — nine formulas
somewhere on Summary referencing Engagements — passes with zero per-row formulas and proves
nothing about this. The second is the deck's whole claim to being trustworthy, and it is the
sort of thing that is easy to intend and hard to keep true by hand.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import logging
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from check_pack import BANNED_RE, BUILDER, gather, sheet_rows  # noqa: E402
from check_tools import PERIOD, Checks, anchors_for, report, reseed  # noqa: E402
from mcp import Client  # noqa: E402
from openpyxl import load_workbook  # noqa: E402
from pptx import Presentation  # noqa: E402

from server import db, toollog  # noqa: E402
from server.app import mcp  # noqa: E402

FIXTURE_SEEDS = (42, 43, *range(9001, 9016))

DECK_BUILDER = REPO_ROOT / "plugin" / "skills" / "house-format" / "scripts" / "build_deck.py"
HOUSE_SKILL = REPO_ROOT / "plugin" / "skills" / "house-format" / "SKILL.md"
SELF_CHECK = REPO_ROOT / "plugin" / "skills" / "house-format" / "assets" / "self-check.md"

# The house format, restated here as data rather than imported from the builder. That is the
# point: a harness that imports the builder's constants asserts that the builder agrees with
# itself. These are copied from house-format/SKILL.md, and a change there that is not made
# here fails.
LIVE_FORMULA_COLUMNS = ("burn_pct", "projected_overrun_pct", "margin_pct")

FORMULA_OPERANDS = {
    "burn_pct": ("hours_to_date", "ceiling_hours"),
    "projected_overrun_pct": ("projected_total_hours", "ceiling_hours"),
    "margin_pct": ("cost_to_date", "ceiling_amount", "billable_value_to_date", "fee_type"),
}

RATIO_PCT = "0.0%"
SCALED_PCT = '0.0"%"'

EXPECTED_FORMATS = {
    "burn_pct": RATIO_PCT,
    "projected_overrun_pct": RATIO_PCT,
    "margin_pct": RATIO_PCT,
    "person_concentration_pct": SCALED_PCT,
    "ceiling_amount": "#,##0",
    "cost_to_date": "#,##0",
    "ceiling_hours": "#,##0.0",
    "hours_to_date": "#,##0.0",
    "start_date": "yyyy-mm-dd",
    "end_date": "yyyy-mm-dd",
}

BURN_RULES = (0.9, 0.7)

# Slide order. Matched as a prefix of the slide's first text, because the RED slides carry the
# engagement name in their heading and the count of them varies with the period.
SLIDE_ORDER = (
    "Delivery and margin review",
    "Portfolio summary",
    "Margin by client",
    "Engagements at risk",
)
RED_SECTION = ("RED", "Engagements flagged RED")
LAST_SLIDE = "Data quality and caveats"

# Numbers that appear on a slide as part of the format's own prose rather than as a figure
# from the data: the coverage floor, the two burn thresholds, and the percent ceiling they are
# expressed against.
FORMAT_CONSTANTS = {60.0, 70.0, 90.0, 100.0}

NUMBER_RE = re.compile(r"[+-]?\d[\d,]*(?:\.\d+)?")
ISO_DATE_RE = re.compile(r"\d{4}-\d{2}(-\d{2})?")


# --------------------------------------------------------------------------- the workbook


def column_letters(ws: Any) -> dict[str, str]:
    return {str(c.value): c.column_letter for c in ws[1] if c.value is not None}


def check_live_formulas(ws: Any, rows: list[dict[str, Any]], checks: Checks) -> None:
    """Each of the three columns, named individually, on every row it applies to.

    Named individually rather than counted, because a count passes when one column is right
    three times. Each of these is a separate promise the format makes.
    """
    at = column_letters(ws)
    last = len(rows) + 1

    for name in LIVE_FORMULA_COLUMNS:
        letter = at.get(name)
        if letter is None:
            checks.add(f"workbook: {name} is a live formula on every row", False, "column absent")
            continue

        values = [ws[f"{letter}{r}"].value for r in range(2, last + 1)]
        formulas = [v for v in values if isinstance(v, str) and v.startswith("=")]
        checks.add(
            f"workbook: {name} is a live formula on every row, not a pasted value",
            len(formulas) == len(values) and bool(values),
            f"{len(formulas)} of {len(values)} row(s) hold a formula",
        )

        # The formula has to read the columns it claims to. A formula that is syntactically a
        # formula but points at the wrong letters computes something real and wrong, which is
        # worse than a pasted value because it looks live.
        wanted = {at[o] for o in FORMULA_OPERANDS[name] if o in at}
        first = formulas[0] if formulas else ""
        referenced = set(re.findall(r"\b([A-Z]{1,2})\d+", first))
        checks.add(
            f"workbook: {name} reads the columns it is defined over",
            wanted <= referenced,
            f"references {sorted(referenced)}, needs {sorted(wanted)} "
            f"({', '.join(FORMULA_OPERANDS[name])})",
        )

        # Guarded, so an unexamined row stays blank rather than reading 0.0%.
        checks.add(
            f"workbook: {name} is guarded so an unexamined row stays blank",
            '""' in first,
            first[:88],
        )


def check_number_formats(ws: Any, rows: list[dict[str, Any]], checks: Checks) -> None:
    at = column_letters(ws)
    wrong = []
    for name, fmt in EXPECTED_FORMATS.items():
        letter = at.get(name)
        if letter is None:
            wrong.append(f"{name}: column absent")
            continue
        got = {ws[f"{letter}{r}"].number_format for r in range(2, len(rows) + 2)}
        if got != {fmt}:
            wrong.append(f"{name}: {sorted(got)} not {fmt!r}")
    checks.add(
        "workbook: every formatted column carries its house number format on every row",
        not wrong,
        f"{len(EXPECTED_FORMATS)} column(s) checked" if not wrong else "; ".join(wrong[:3]),
    )

    # The two percent scales, asserted as being different. If somebody "tidies" the literal
    # 0.0"%" into 0.0% the tool-scaled columns silently become 5,410% instead of 54.1%.
    checks.add(
        "workbook: the tool-scaled percentages keep the literal sign, not the scaling one",
        ws[f"{at['person_concentration_pct']}2"].number_format == SCALED_PCT
        != ws[f"{at['burn_pct']}2"].number_format,
        f"concentration {SCALED_PCT}, burn {RATIO_PCT}",
    )


def check_conditional_formatting(ws: Any, rows: list[dict[str, Any]], checks: Checks) -> None:
    at = column_letters(ws)
    burn = at.get("burn_pct")

    found: list[tuple[str, float]] = []
    for rng in ws.conditional_formatting:
        if burn and burn not in str(rng.sqref):
            continue
        for rule in rng.rules:
            if rule.operator == "greaterThan" and rule.formula:
                found.append((str(rng.sqref), float(rule.formula[0])))

    thresholds = [t for _, t in found]
    checks.add(
        "workbook: burn carries the two conditional formatting rules, at 0.90 and 0.70",
        thresholds == list(BURN_RULES),
        f"{thresholds} on {found[0][0] if found else 'no range'}",
    )
    checks.add(
        "workbook: red is tested before amber, so a red cell is not painted amber",
        len(thresholds) == 2 and thresholds[0] > thresholds[1],
        f"first rule at {thresholds[0]}" if thresholds else "no rules",
    )
    covered = found and str(found[0][0]).endswith(str(len(rows) + 1))
    checks.add(
        "workbook: the rules cover every engagement row, not only the examined ones",
        bool(covered),
        f"{found[0][0] if found else 'none'} for {len(rows)} row(s)",
    )


def check_formulas_agree(rows: list[dict[str, Any]], pack: dict[str, Any], checks: Checks) -> None:
    """The formulas and the database compute the same thing.

    Excel is not run here, so what is compared is the formula's operands — copied values on
    the same row — against the ratio SQL returned for that engagement. Tolerance is half a
    decimal place, because the tool rounded to one and the operands did not.
    """
    burn_off, margin_off = [], []

    by_id = {e["engagement_id"]: e for e in pack["engagements"]}
    financials = {int(k): v for k, v in (pack.get("financials") or {}).items()}

    for row in rows:
        eid = row["engagement_id"]
        ceiling_hours = row.get("ceiling_hours")
        if ceiling_hours:
            live = 100 * row["hours_to_date"] / ceiling_hours
            sql = by_id.get(eid, {}).get("burn_pct")
            if sql is not None and abs(live - sql) > 0.05 + 1e-9:
                burn_off.append((eid, round(live, 3), sql))

        fin = financials.get(eid)
        cost = row.get("cost_to_date")
        if not fin or cost is None or fin.get("margin_pct") is None:
            continue
        if row.get("fee_type") == "fixed":
            denominator = row.get("ceiling_amount")
        else:
            denominator = row.get("billable_value_to_date")
        if not denominator:
            continue
        live = 100 * (denominator - cost) / denominator
        if abs(live - fin["margin_pct"]) > 0.05 + 1e-9:
            margin_off.append((eid, round(live, 3), fin["margin_pct"]))

    checks.add(
        "workbook: the live burn formula agrees with the database on every row",
        not burn_off,
        f"{len(rows)} row(s)" if not burn_off
        else f"engagement {burn_off[0][0]}: {burn_off[0][1]} vs {burn_off[0][2]}",
    )
    checks.add(
        "workbook: the fee-type margin branch agrees with the database on every examined row",
        not margin_off,
        f"{len(financials)} examined row(s)" if not margin_off
        else f"engagement {margin_off[0][0]}: {margin_off[0][1]} vs {margin_off[0][2]}",
    )


def check_summary_cross_check(wb: Any, pack: dict[str, Any], checks: Checks) -> None:
    """Summary holds the same totals twice, from two independent computations.

    The Excel SUM over the tab and the SQL rollup are worked out in different places by
    different code, so a disagreement means one of them is wrong. Excel has not run, so the
    SUM is done here over the same cells the formula points at.
    """
    _, rows = sheet_rows(wb["Engagements"])
    book = pack.get("portfolio") or {}

    mismatched = []
    for column, field in (("hours_to_date", "hours_to_date_total"), ("ceiling_hours", "ceiling_hours_total")):
        summed = round(sum(r[column] for r in rows if r.get(column) is not None), 2)
        reported = round(float(book.get(field, 0)), 2)
        if abs(summed - reported) > 0.05:
            mismatched.append(f"{field}: tab sums to {summed}, SQL says {reported}")

    checks.add(
        "workbook: the Engagements tab and the SQL rollup agree on the portfolio totals",
        not mismatched,
        "hours and ceiling hours agree" if not mismatched else "; ".join(mismatched),
    )

    labels = {
        str(row[0].value): row[1].value
        for row in wb["Summary"].iter_rows(min_row=1, max_row=60)
        if row[0].value is not None
    }
    checks.add(
        "workbook: Summary carries the blended margin, which no formula over the tab can produce",
        labels.get("Blended margin") is not None
        and abs(float(labels["Blended margin"]) - float(book["blended_margin_pct"])) < 0.001,
        f"{labels.get('Blended margin')}% on the tab, {book.get('blended_margin_pct')}% from SQL",
    )


# ------------------------------------------------------------------------------- the deck


def slide_text(slide: Any) -> list[str]:
    """Every string on a slide: text frames, table cells and chart categories."""
    found: list[str] = []
    for shape in slide.shapes:
        if shape.has_text_frame and shape.text_frame.text.strip():
            found.append(shape.text_frame.text)
        if getattr(shape, "has_table", False) and shape.has_table:
            found += [cell.text for row in shape.table.rows for cell in row.cells]
        if getattr(shape, "has_chart", False) and shape.has_chart:
            found += [str(c) for c in shape.chart.plots[0].categories]
            found += [
                str(v) for series in shape.chart.plots[0].series for v in series.values
                if v is not None
            ]
    return found


def heading_of(slide: Any) -> str:
    for shape in slide.shapes:
        if shape.has_text_frame and shape.text_frame.text.strip():
            return shape.text_frame.text.strip().splitlines()[0]
    return ""


def known_numbers(pack: dict[str, Any], wb: Any) -> list[float]:
    """Every number in the pack and every numeric cell in the workbook.

    The pack is included alongside the workbook because a handful of deck figures are
    legitimately in the book as a formula rather than a value — the workbook holds
    `=SUM(Engagements!J2:J19)` where the deck prints 44,390.0, and openpyxl cannot evaluate
    it. The pack is the source both files were built from, so a number found there is a
    number that came from a tool, which is the property being asserted.
    """
    found: list[float] = []

    def walk(node: Any) -> None:
        if isinstance(node, bool):
            return
        if isinstance(node, (int, float)):
            found.append(float(node))
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(pack)

    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if isinstance(cell.value, (int, float)) and not isinstance(cell.value, bool):
                    found.append(float(cell.value))
    return found


def check_traceability(prs: Any, pack: dict[str, Any], wb: Any, checks: Checks) -> None:
    """No number on a slide that is not in the workbook or the pack behind it.

    Tolerance follows the decimals shown: a figure printed to one decimal place is matched to
    within 0.05, one printed to none to within 0.5. Anything looser would pass a genuinely
    different number that happens to round nearby.
    """
    known = known_numbers(pack, wb)
    orphans: list[tuple[int, str]] = []

    # Identifiers that contain digits and are not figures: the run id, which ends in the seed
    # number, and the scoring model version. Removed by exact text rather than by pattern, so
    # this cannot quietly swallow a real number that happens to look like a version.
    identifiers = [
        str(pack[key]) for key in ("run_id", "scoring_model_version") if pack.get(key)
    ]

    for i, slide in enumerate(prs.slides, start=1):
        for text in slide_text(slide):
            for line in text.splitlines():
                # Dates carry digits and are not figures. Stripped whole, so 2026-08-10 does
                # not leave 2026, 08 and 10 behind to be matched against nothing.
                stripped = ISO_DATE_RE.sub(" ", line)
                for identifier in identifiers:
                    stripped = stripped.replace(identifier, " ")
                for token in NUMBER_RE.findall(stripped):
                    raw = token.replace(",", "")
                    try:
                        value = float(raw)
                    except ValueError:
                        continue
                    if abs(value) in FORMAT_CONSTANTS:
                        continue
                    decimals = len(raw.split(".")[1]) if "." in raw else 0
                    tolerance = 0.5 * (10 ** -decimals) + 1e-9
                    if not any(abs(value - k) <= tolerance for k in known):
                        orphans.append((i, token))

    checks.add(
        "deck: every number on a slide is in the workbook or the pack it was built from",
        not orphans,
        f"{len(known)} known figure(s) checked" if not orphans
        else f"slide {orphans[0][0]} shows {orphans[0][1]!r}, and {len(orphans)} other(s)",
    )


def check_deck(path: Path, pack: dict[str, Any], wb: Any, checks: Checks) -> None:
    prs = Presentation(path)
    slides = list(prs.slides)
    headings = [heading_of(s) for s in slides]

    checks.add(
        "deck: the four fixed slides come first, in the house order",
        headings[:4] == list(SLIDE_ORDER),
        " | ".join(headings[:4]),
    )
    checks.add(
        "deck: data quality and caveats is the last slide, always",
        headings[-1] == LAST_SLIDE,
        f"slide {len(slides)} of {len(slides)}: {headings[-1]}",
    )
    checks.add(
        "deck: the RED section sits between engagements at risk and the caveats",
        all(h.startswith(RED_SECTION) for h in headings[4:-1]) and 1 <= len(headings[4:-1]) <= 3,
        f"{len(headings[4:-1])} slide(s): {' | '.join(headings[4:-1])}",
    )
    checks.add(
        "deck: six to eight slides, the format's full extent being eight",
        6 <= len(slides) <= 8,
        f"{len(slides)} slides",
    )

    # A native chart, not an image of one. The claim the format makes is that a reader can
    # click into the figures, and a picture cannot be clicked into.
    charts = [sh for s in slides for sh in s.shapes if getattr(sh, "has_chart", False) and sh.has_chart]
    clients = pack["portfolio"]["clients"]
    plotted = [c for c in clients if c.get("margin_pct") is not None]
    categories = list(charts[0].chart.plots[0].categories) if charts else []
    checks.add(
        "deck: margin by client is a chart object with one column per client",
        len(charts) == 1 and len(categories) == len(plotted),
        f"{len(charts)} chart(s), {len(categories)} categories for {len(plotted)} client(s)",
    )
    checks.add(
        "deck: the chart's categories are the client names from the portfolio block",
        {str(c) for c in categories} == {c["client_name"] for c in plotted},
        f"{len(categories)} name(s) matched",
    )

    # Prose is the part of a deck no other check looks at, and it is the part that turns a
    # filing gap into a delivery finding.
    offending = [
        (i, m.group(0))
        for i, slide in enumerate(prs.slides, start=1)
        for text in slide_text(slide)
        for m in [BANNED_RE.search(text)]
        if m
    ]
    checks.add(
        "deck: nothing on a slide calls the low-coverage week a slowdown",
        not offending,
        "no banned wording on any slide" if not offending
        else f"slide {offending[0][0]}: {offending[0][1]!r}",
    )

    low = {eid: b for eid, b in (pack.get("burn") or {}).items()
           if b.get("projection_confidence") == "low"}
    everything = " ".join(t for s in slides for t in slide_text(s))
    unlabelled = [eid for eid, b in low.items() if b.get("confidence_reason", "")[:40] not in everything]
    checks.add(
        "deck: every low-confidence projection carries its reason onto a slide",
        not unlabelled,
        f"{len(low)} low-confidence projection(s), all labelled" if not unlabelled
        else f"unlabelled: {unlabelled}",
    )

    check_traceability(prs, pack, wb, checks)


def clean_pack(pack: dict[str, Any]) -> dict[str, Any]:
    """The same pack with every finding removed, to prove slide 8 is not conditional.

    A caveats slide that appears only when there is something to caveat teaches its reader
    that its absence means "fine", which is exactly the inference the coverage rule exists to
    prevent. So the clean case is synthesised rather than waited for: no gap week, no late
    filing, no duplicates, no low-confidence projection, nothing flagged.
    """
    clean = copy.deepcopy(pack)
    summary = clean["time_summary"]

    for week in summary.get("data_completeness", {}).get("weeks", []):
        week["firm_wide_gap"] = False
        week["pct_active_reporting"] = 100.0
        week["engagements_reporting"] = week.get("engagements_active")
    summary.get("data_completeness", {})["weeks_with_gap"] = 0
    summary.get("data_completeness", {})["lowest_pct_active_reporting"] = 100.0

    for field in ("late_entries", "null_billable", "suspected_duplicates"):
        summary.get("data_quality", {})[field] = 0
    summary.get("data_quality", {})["late_entry_pct"] = 0.0

    for burn in (clean.get("burn") or {}).values():
        burn["projection_confidence"] = "high"
        burn["confidence_reason"] = "four readable weeks with time logged in every one"

    clean["exceptions"] = []
    return clean


def check_clean_period(pack: dict[str, Any], workdir: Path, checks: Checks) -> None:
    path = workdir / "pack-clean.json"
    path.write_text(json.dumps(clean_pack(pack), default=str, indent=2))
    out = workdir / "deck-clean.pptx"

    result = subprocess.run(
        [sys.executable, str(DECK_BUILDER), str(path), "--out", str(out)],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not out.exists():
        checks.add("deck: a period with no findings still produces a deck", False, result.stderr.strip()[:110])
        return

    prs = Presentation(out)
    headings = [heading_of(s) for s in prs.slides]
    last = " ".join(slide_text(list(prs.slides)[-1]))

    checks.add(
        "deck: a clean period still gets its caveats slide, last and in full",
        headings[-1] == LAST_SLIDE,
        f"{len(headings)} slides, last is {headings[-1]!r}",
    )
    checks.add(
        "deck: on a clean period the caveats slide states the absence rather than going blank",
        "met the 60% reporting coverage floor" in last
        and "came back high confidence" in last,
        "coverage and confidence both stated as clean",
    )
    checks.add(
        "deck: a clean period says no engagement was flagged rather than dropping the section",
        any(h.startswith(RED_SECTION) for h in headings),
        f"{[h for h in headings if h.startswith(RED_SECTION)]}",
    )


def check_deck_refusals(pack: dict[str, Any], workdir: Path, checks: Checks) -> None:
    """The deck refuses a pack it cannot build honestly, rather than filling the gap.

    Without the portfolio block the summary and margin slides have no source, and the only
    way to produce them would be for the builder to total the rows. That is the arithmetic
    the whole design exists to avoid, so the correct behaviour is to stop.
    """
    for label, mutate, expect in (
        ("no portfolio block", lambda p: p.pop("portfolio", None), "include_portfolio"),
        ("no engagements", lambda p: p.__setitem__("engagements", []), "failed run"),
    ):
        broken = copy.deepcopy(pack)
        mutate(broken)
        path = workdir / f"pack-broken-{label.replace(' ', '-')}.json"
        path.write_text(json.dumps(broken, default=str, indent=2))

        result = subprocess.run(
            [sys.executable, str(DECK_BUILDER), str(path), "--out", str(workdir / "never.pptx")],
            capture_output=True, text=True,
        )
        checks.add(
            f"build_deck refuses a pack with {label}",
            result.returncode != 0 and expect in result.stderr,
            result.stderr.strip().replace("\n", " ")[:104],
        )


# ------------------------------------------------------------------------------ the skill


def check_house_skill(checks: Checks) -> None:
    checks.add("skill: house-format/SKILL.md exists", HOUSE_SKILL.exists(), str(HOUSE_SKILL.relative_to(REPO_ROOT)))
    if not HOUSE_SKILL.exists():
        return
    text = HOUSE_SKILL.read_text()

    frontmatter = text.split("---")[1] if text.startswith("---") else ""
    checks.add(
        "skill: frontmatter says what the skill is for and when it fires",
        len(frontmatter) > 300 and "house-format" in frontmatter,
        f"{len(frontmatter)} chars of frontmatter",
    )

    # The prose hedge. If a run reaches Cowork without the bundled scripts, the SKILL.md is
    # all it has, so the format has to be stated in words as well as implemented in code.
    for label, needle in (
        ("the five tabs in order", "`Data Quality`"),
        ("the burn thresholds", "amber above 0.70"),
        ("the fee-type margin branch", "branches on fee type"),
        ("the two percent scales", '0.0"%"'),
        ("slide 8 never being dropped", "Never omitted"),
        ("the no-adjectives voice rule", "concerningly high"),
        ("stopping rather than substituting a builder", "Do not write a replacement"),
    ):
        checks.add(f"skill: states {label}", needle in text, "present" if needle in text else f"missing {needle!r}")

    checks.add(
        "skill: the self-check asset exists and is pointed at from the body",
        SELF_CHECK.exists() and "self-check.md" in text,
        f"{len(SELF_CHECK.read_text().splitlines()) if SELF_CHECK.exists() else 0} lines",
    )


# ------------------------------------------------------------------------------ the runner


async def run_seed(seed: int, anchors: dict[str, Any], checks: Checks, workdir: Path) -> None:
    toollog.logger.handlers.clear()
    toollog.logger.addHandler(logging.NullHandler())
    toollog.logger.propagate = False

    period_end = anchors.get("period_end") or "2026-08-31"
    check_house_skill(checks)

    async with Client(mcp) as client:
        pack = await gather(client, f"format-check-seed-{seed}", period_end)

    pack_path = workdir / f"pack-{seed}.json"
    pack_path.write_text(json.dumps(pack, default=str, indent=2))
    pack = json.loads(pack_path.read_text())

    book = workdir / f"engagement-book-{PERIOD}-seed-{seed}.xlsx"
    deck = workdir / f"delivery-review-{PERIOD}-seed-{seed}.pptx"

    for label, script, out in (("build_workbook", BUILDER, book), ("build_deck", DECK_BUILDER, deck)):
        result = subprocess.run(
            [sys.executable, str(script), str(pack_path), "--out", str(out)],
            capture_output=True, text=True,
        )
        checks.add(
            f"{label} ran and wrote its artifact",
            result.returncode == 0 and out.exists(),
            result.stdout.strip()[:104] if result.returncode == 0 else result.stderr.strip()[:104],
        )
    if not (book.exists() and deck.exists()):
        return

    wb = load_workbook(book)
    ws = wb["Engagements"]
    _, rows = sheet_rows(ws)

    check_live_formulas(ws, rows, checks)
    check_number_formats(ws, rows, checks)
    check_conditional_formatting(ws, rows, checks)
    check_formulas_agree(rows, pack, checks)
    check_summary_cross_check(wb, pack, checks)

    check_deck(deck, pack, wb, checks)
    check_clean_period(pack, workdir, checks)
    check_deck_refusals(pack, workdir, checks)


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, action="append", help="Repeatable. Every fixture seed when omitted.")
    ap.add_argument("--dsn", default=db.DEFAULT_DSN)
    ap.add_argument("--no-reseed", action="store_true", help="Use the database as it stands.")
    ap.add_argument("--verbose", "-v", action="store_true", help="Print passing assertions too.")
    ap.add_argument("--keep", type=Path, help="Write the packs, workbooks and decks here and leave them.")
    ap.add_argument(
        "--backend", choices=("local", "aws"), default="local",
        help="'aws' drives the same sequence over the RDS Data API. Implies --no-reseed.",
    )
    args = ap.parse_args()

    seeds = args.seed or list(FIXTURE_SEEDS)
    if args.no_reseed and len(seeds) > 1:
        ap.error("--no-reseed needs a single --seed, since it cannot change the loaded data")
    if args.backend == "aws" and not args.no_reseed:
        ap.error("--backend aws expects --no-reseed and a single --seed matching what Aurora holds")

    db._backend = db.get_backend(args.backend, args.dsn)

    workdir = args.keep or Path(tempfile.mkdtemp(prefix="bizdata-format-"))
    workdir.mkdir(parents=True, exist_ok=True)

    ok = True
    try:
        for seed in seeds:
            if not args.no_reseed:
                reseed(seed, args.dsn)
            checks = Checks(seed)
            try:
                await run_seed(seed, anchors_for(seed), checks, workdir)
            except Exception as exc:  # noqa: BLE001
                while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
                    exc = exc.exceptions[0]
                checks.add("harness ran to completion", False, f"{type(exc).__name__}: {exc}"[:160])
            ok &= report(seed, checks, args.verbose)
    finally:
        if args.keep:
            print(f"\nworkbooks and decks left in {workdir}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)

    print()
    print("all seeds pass" if ok else "FAILURES above")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
