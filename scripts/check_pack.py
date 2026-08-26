#!/usr/bin/env python3
"""Step 6's Done-when conditions, as assertions.

    scripts/check_pack.py                  every fixture seed, reseeding each
    scripts/check_pack.py --seed 42 -v     one seed, against the database as it stands
    scripts/check_pack.py --keep /tmp/out  leave the workbooks behind to open

check_tools.py asserts things about the tools. This asserts things about the *pack*: that
the order of operations in assemble-delivery-pack/SKILL.md gathers what it claims to, that
the engagements it chooses to examine include the three mess cases a burn threshold cannot
reach, and that the workbook that comes out says the right thing about the week nobody
filed in.

It drives the same in-memory Client(mcp) that check_tools.py uses, then shells out to
build_workbook.py exactly as the skill instructs — same command line, same JSON contract —
and reads the result back with openpyxl. Nothing about the workbook is asserted from the
data that produced it; every check re-reads the file on disk.

The two assertions that carry the step:

    the examine set reaches mess cases 6, 7 and 8, none of which clears a burn threshold
    no cell in the workbook calls the low-coverage week a slowdown

The second one is a wording check, which is an unusual thing to automate and is here because
the failure it catches is the one the whole coverage rule exists to prevent, and because
prose is exactly the part of a pack no other check looks at.
"""

from __future__ import annotations

import argparse
import asyncio
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

from check_tools import PERIOD, Checks, anchors_for, report, reseed  # noqa: E402
from mcp import Client  # noqa: E402
from openpyxl import load_workbook  # noqa: E402

from server import db, toollog  # noqa: E402
from server.app import mcp  # noqa: E402

FIXTURE_SEEDS = (42, 43, *range(9001, 9016))

BUILDER = REPO_ROOT / "plugin" / "skills" / "assemble-delivery-pack" / "scripts" / "build_workbook.py"
SKILL = REPO_ROOT / "plugin" / "skills" / "assemble-delivery-pack" / "SKILL.md"

TABS = ["Summary", "Engagements", "Time Detail", "Exceptions", "Data Quality"]

# The examine triggers from the skill, as the pack applies them. Kept here as one function
# so that the check and the prose cannot drift without somebody noticing.
CONCENTRATION_TRIGGER = 70
BURN_TRIGGER = 70
SILENT_DAYS_TRIGGER = 14
MARGIN_TRIGGER = 15

# Words that turn a filing gap into a delivery finding. Matched on word boundaries, so
# "install" does not trip "stall" and a client called Fernhollow does not trip "fell".
BANNED = (
    "slowdown", "slow down", "slow-down", "slump", "stall", "stalled", "downturn",
    "dip", "dipped", "decline", "declined", "collapse", "collapsed", "fell", "drop",
    "dropped", "dropoff", "drop-off", "slowed", "slowing", "underperformed",
)
BANNED_RE = re.compile(r"\b(" + "|".join(re.escape(w) for w in BANNED) + r")\b", re.IGNORECASE)


def examine(engagement: dict[str, Any], period_end: str) -> bool:
    """The six triggers. Any one of them is enough.

    C and D are step 6's addition and the reason it exists: an engagement can be inside its
    ceiling and in the green band while one person is 85% of its hours, or while its contract
    has already ended.

    E and F are step 8's, for the same reason one step further on. scope-escalation flags a
    silent engagement and a fixed-fee engagement under water, and on six of the seventeen
    fixture seeds neither was examined — so neither could be flagged, because the field that
    proves it comes back from the call this filter declined to make.
    """
    concentration = engagement.get("person_concentration_pct")
    silent_days = engagement.get("days_since_last_entry")
    margin = engagement.get("margin_pct")
    return (
        engagement["burn_pct"] > BURN_TRIGGER
        or engagement.get("health_band") != "green"
        or (concentration is not None and concentration > CONCENTRATION_TRIGGER)
        or str(engagement["end_date"]) <= period_end
        or (silent_days is not None and silent_days >= SILENT_DAYS_TRIGGER)
        or (margin is not None and margin < MARGIN_TRIGGER)
    )


async def gather(client: Client, run_id: str, period_end: str) -> dict[str, Any]:
    """The skill's order of operations, followed exactly."""

    async def call(tool: str, **arguments: Any) -> dict[str, Any]:
        result = await client.call_tool(tool, {"run_id": run_id, **arguments})
        if getattr(result, "is_error", False):
            text = "; ".join(getattr(c, "text", "") for c in (result.content or []))
            raise AssertionError(f"{tool} returned an error: {text}")
        return result.structured_content

    # 0. The run ledger, before anything else. Added at step 12, and put here rather than
    #    into a fixture on purpose: check_format.py, check_publish.py and check_escalation.py
    #    all build their packs through this function, so the shape they assert against is the
    #    shape the SKILL's own order of operations produces. Step 11's defect survived 427
    #    assertions a seed precisely because the fixture and the documented process had
    #    drifted apart, and the way not to repeat that is for the harness to follow the
    #    document rather than to describe it.
    ledger = await call("get_run_ledger", period=PERIOD)

    # 1. The portfolio, paged to the end. limit=5 rather than 100 so the paging loop is
    #    genuinely exercised on eighteen engagements rather than being one page.
    #    include_portfolio on the first page only: the block is identical on every page, and
    #    asking four times would be four identical answers paid for four times.
    first = await call(
        "list_engagements", as_of_date=period_end, status="active", limit=5,
        include_portfolio=True,
    )
    engagements = list(first["engagements"])
    cursor, pages = first["next_cursor"], 1
    while cursor and pages <= 50:
        page = await call("list_engagements", as_of_date=period_end, status="active", limit=5, cursor=cursor)
        engagements += page["engagements"]
        cursor, pages = page["next_cursor"], pages + 1

    # 2. One portfolio-wide time summary, no engagement_ids, before anything is analysed.
    summary = await call(
        "get_time_summary",
        period_start=f"{PERIOD}-01",
        period_end=period_end,
        group_by="engagement,week",
    )

    # 3 and 4. Detail only for the engagements that met a trigger.
    chosen = [e for e in engagements if examine(e, period_end)]
    burn: dict[str, Any] = {}
    financials: dict[str, Any] = {}
    for engagement in chosen:
        eid = engagement["engagement_id"]
        burn[str(eid)] = await call("get_engagement_burn", engagement_id=eid, as_of_date=period_end)
        financials[str(eid)] = await call("get_financials", engagement_id=eid, period=PERIOD)

    return {
        "period": PERIOD,
        "run_id": run_id,
        "run_ledger": ledger,
        "scoring_model_version": first.get("scoring_model_version"),
        "total_count": first["total_count"],
        "pages": pages,
        "engagements": engagements,
        "portfolio": first.get("portfolio"),
        "time_summary": summary,
        "burn": burn,
        "financials": financials,
        # scope-escalation is step 8. An empty list is a valid pack and the tab says so.
        "exceptions": [],
    }


def sheet_rows(ws: Any) -> tuple[dict[str, int], list[dict[str, Any]]]:
    """A header-keyed view of a tab, so assertions name columns rather than positions."""
    header = {
        str(cell.value): i
        for i, cell in enumerate(next(ws.iter_rows(min_row=1, max_row=1)))
        if cell.value is not None
    }
    rows = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        if all(v is None for v in row):
            continue
        rows.append({name: row[i] if i < len(row) else None for name, i in header.items()})
    return header, rows


def all_text(wb: Any) -> list[tuple[str, str, str]]:
    """Every string in the workbook, with the tab and cell it came from."""
    found = []
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if isinstance(cell.value, str):
                    found.append((ws.title, cell.coordinate, cell.value))
    return found


def check_workbook(path: Path, pack: dict[str, Any], anchors: dict[str, Any], checks: Checks) -> None:
    wb = load_workbook(path)
    gap_week = anchors["case_7_week_start"]

    checks.add(
        "workbook: five tabs, in the house order",
        wb.sheetnames == TABS,
        ", ".join(wb.sheetnames),
    )
    if wb.sheetnames != TABS:
        return

    # ---- Engagements ---------------------------------------------------------------
    header, rows = sheet_rows(wb["Engagements"])
    checks.add(
        "workbook: every engagement in the portfolio reached the book",
        len(rows) == pack["total_count"],
        f"{len(rows)} rows for total_count {pack['total_count']}",
    )
    checks.add(
        "workbook: every row carries its engagement_id",
        bool(rows) and all(isinstance(r.get("engagement_id"), int) for r in rows),
        f"{sum(1 for r in rows if isinstance(r.get('engagement_id'), int))} of {len(rows)}",
    )

    by_id = {r["engagement_id"]: r for r in rows}
    examined = {eid for eid, r in by_id.items() if r.get("examined") == "yes"}
    checks.add(
        "workbook: the examined engagements are the ones the triggers chose",
        examined == {int(k) for k in pack["burn"]},
        f"{len(examined)} of {len(rows)} examined",
    )

    # ---- the three cases a burn threshold cannot reach -------------------------------
    concentrated = anchors["case_8_concentration_engagement"]
    ending = anchors["case_6_mid_period_end_engagement"]

    def burn_of(eid: int) -> str:
        """Burn for a detail string, from the operands rather than the cell.

        The cell holds a formula since step 7, and a failure message reading
        `burn =IF(N(J4)>0,K4/J4,"")%` helps nobody.
        """
        row = by_id.get(eid, {})
        ceiling, hours = row.get("ceiling_hours"), row.get("hours_to_date")
        return f"{round(100 * hours / ceiling, 1)}" if ceiling else "n/a"

    row = by_id.get(concentrated, {})
    checks.add(
        "mess case 8: the concentrated engagement was examined",
        concentrated in examined,
        f"engagement {concentrated} at {row.get('person_concentration_pct')}% concentration, "
        f"burn {burn_of(concentrated)}%, band {row.get('health_band')}",
    )
    checks.add(
        "mess case 6: the engagement that ended inside the period was examined",
        ending in examined,
        f"engagement {ending} ended {by_id.get(ending, {}).get('end_date')}, "
        f"burn {burn_of(ending)}%",
    )

    # ---- nothing was re-derived ------------------------------------------------------
    # Since step 7 burn_pct is a live formula, so the cell holds a formula string rather than
    # the tool's number and cannot be compared to it directly. What is compared instead is the
    # formula's own operands against the tool's answer: hours_to_date over ceiling_hours, both
    # copied values on the same row, against burn_pct as SQL computed it.
    #
    # That is a stronger check than the one it replaces. The old assertion proved the workbook
    # had not altered a number in transit. This one proves the live formula and the database
    # agree about what burn is, which is the claim a reviewer clicking the cell is testing.
    drift = [
        eid for eid in examined
        if by_id[eid].get("weekly_run_rate_4wk") != pack["burn"][str(eid)].get("weekly_run_rate_4wk")
    ]
    checks.add(
        "workbook: run rates are the tools' figures, not re-derived ones",
        not drift,
        "every examined row matches its tool response" if not drift else f"differs on {drift}",
    )

    # Compared unrounded against the tool's one-decimal figure, with half a decimal place of
    # tolerance, because that is the largest gap the two can honestly have: SQL rounded to
    # 1dp, so the true ratio is within 0.05 of what it reported.
    #
    # Rounding both sides and demanding equality is the obvious version and it is wrong.
    # Postgres rounds half away from zero and Python rounds half to even, so a burn landing
    # exactly on x.x5 comes out 0.1 apart with nothing whatsoever wrong. That fired on seed
    # 9002 and no other — engagement 30 at 138.25% — which is the fixture set earning its
    # keep for the fifth time. Excel rounds half up for display, so the cell shows Postgres's
    # answer anyway; the disagreement was only ever between the harness and the database.
    disagree = []
    for eid, row in by_id.items():
        ceiling, hours = row.get("ceiling_hours"), row.get("hours_to_date")
        if not ceiling:
            continue
        computed = 100 * hours / ceiling
        reported = next(
            (e["burn_pct"] for e in pack["engagements"] if e["engagement_id"] == eid), None
        )
        if reported is not None and abs(computed - reported) > 0.05 + 1e-9:
            disagree.append((eid, round(computed, 4), reported))
    checks.add(
        "workbook: the live burn formula and the database agree on every row",
        not disagree,
        f"{len(by_id)} row(s) within half a decimal place" if not disagree
        else f"engagement {disagree[0][0]}: formula {disagree[0][1]} vs SQL {disagree[0][2]}",
    )

    low = {eid for eid in examined if by_id[eid].get("projection_confidence") == "low"}
    unlabelled = [eid for eid in low if not str(by_id[eid].get("confidence_reason") or "").strip()]
    checks.add(
        "workbook: every low-confidence projection carries its reason",
        not unlabelled,
        f"{len(low)} low-confidence projection(s), all labelled" if not unlabelled
        else f"unlabelled on {unlabelled}",
    )

    # ---- Summary is formulas over Engagements, never pasted values -------------------
    summary_values = [c.value for row in wb["Summary"].iter_rows() for c in row if c.value is not None]
    formulas = [v for v in summary_values if isinstance(v, str) and v.startswith("=")]
    referencing = [f for f in formulas if "Engagements!" in f]
    checks.add(
        "workbook: portfolio totals on Summary are formulas over the Engagements tab",
        len(referencing) >= 9,
        f"{len(referencing)} formula(s) referencing Engagements, {len(formulas)} in total",
    )
    checks.add(
        "workbook: Summary carries the scoring model version",
        any(str(v) == pack["scoring_model_version"] for v in summary_values),
        str(pack["scoring_model_version"]),
    )

    # ---- the pack carries what step 0 decided on ------------------------------------
    ledger = pack.get("run_ledger") or {}
    checks.add(
        "pack: carries the run_ledger response the SKILL's step 0 produces",
        ledger.get("decision") in ("first_run", "unchanged", "changed"),
        str(ledger.get("decision")),
    )
    checks.add(
        "pack: and its digest, so the archived pack records what it was built from",
        bool(ledger.get("figures_digest")) and ledger.get("period") == PERIOD,
        str(ledger.get("figures_digest")),
    )

    # ---- Time Detail marks the gap week rather than dropping it ----------------------
    _, detail = sheet_rows(wb["Time Detail"])
    gap_rows = [r for r in detail if str(r.get("week_start")) == gap_week]
    checks.add(
        "workbook: the gap week's rows are present in Time Detail and flagged, not removed",
        bool(gap_rows) and all(r.get("firm_wide_gap") is True for r in gap_rows),
        f"{len(gap_rows)} row(s) for the week of {gap_week}, all flagged",
    )

    # ---- Data Quality carries the coverage note --------------------------------------
    quality_text = " ".join(
        str(c.value) for row in wb["Data Quality"].iter_rows() for c in row if c.value is not None
    )
    checks.add(
        "mess case 7: the low-coverage week appears on Data Quality as a data note",
        gap_week in quality_text and "filing artifact" in quality_text,
        f"week of {gap_week} noted" if gap_week in quality_text else "the week is not named",
    )
    checks.add(
        "mess case 7: the note says the week is excluded from run rates and projections",
        "excluded from every run rate and projection" in quality_text,
        "stated",
    )
    checks.add(
        "workbook: the two data blocks stay separate on the tab",
        "Record quality" in quality_text and "Reporting coverage by week" in quality_text,
        "both sections present",
    )

    # ---- and the wording check -------------------------------------------------------
    offences = [
        (tab, coord, BANNED_RE.search(text).group(0), text[:60])
        for tab, coord, text in all_text(wb)
        if BANNED_RE.search(text)
    ]
    checks.add(
        "mess case 7: nothing in the pack calls the week a delivery slowdown",
        not offences,
        "no banned wording anywhere in the workbook" if not offences
        else f"{offences[0][0]}!{offences[0][1]} says {offences[0][2]!r}: {offences[0][3]}",
    )

    # ---- the empty Exceptions tab is a statement, not a blank ------------------------
    exceptions_text = " ".join(
        str(c.value) for row in wb["Exceptions"].iter_rows() for c in row if c.value is not None
    )
    checks.add(
        "workbook: an Exceptions tab with nothing on it says so",
        "No engagement was flagged" in exceptions_text,
        "stated rather than left blank",
    )


def check_builder_refusals(pack: dict[str, Any], workdir: Path, checks: Checks) -> None:
    """The stop conditions, at the last place they can still be enforced.

    A scoped time summary is the interesting one. It looks like a thrifty call and it
    quietly changes what data_completeness means, so the coverage rule silently measures
    the wrong population. That failure is invisible in the output, which is why it is
    refused at the door rather than checked afterwards.
    """
    scoped = json.loads(json.dumps(pack))
    scoped["time_summary"]["engagement_ids"] = [pack["engagements"][0]["engagement_id"]]

    empty = json.loads(json.dumps(pack))
    empty["engagements"] = []

    for name, bad, expected in (
        ("a time summary scoped to some engagements", scoped, "whole portfolio"),
        ("an empty engagement list", empty, "failed run"),
    ):
        path = workdir / "bad.json"
        path.write_text(json.dumps(bad, default=str))
        result = subprocess.run(
            [sys.executable, str(BUILDER), str(path), "--out", str(workdir / "bad.xlsx")],
            capture_output=True, text=True,
        )
        checks.add(
            f"build_workbook refuses {name}",
            result.returncode != 0 and expected in result.stderr,
            result.stderr.strip().splitlines()[-1][:96] if result.stderr.strip() else "no message",
        )


def check_exceptions_populate(pack: dict[str, Any], workdir: Path, checks: Checks) -> None:
    """The populated Exceptions path, which the run itself cannot exercise until step 8."""
    with_exception = json.loads(json.dumps(pack, default=str))
    eid = sorted(int(k) for k in pack["burn"])[0]
    with_exception["exceptions"] = [{
        "engagement_id": eid,
        "flag": "RED",
        "triggers": "placeholder trigger",
        "situation": "placeholder from scripts/check_pack.py",
        "cause": "not determinable from available data",
        "recommended_action": "none, this is a harness fixture",
        "decision_owner": "engagement lead",
    }]

    path = workdir / "with-exception.json"
    path.write_text(json.dumps(with_exception, default=str))
    out = workdir / "with-exception.xlsx"
    result = subprocess.run(
        [sys.executable, str(BUILDER), str(path), "--out", str(out)], capture_output=True, text=True
    )
    if result.returncode != 0:
        checks.add("workbook: a flagged engagement reaches the Exceptions tab", False, result.stderr[:96])
        return

    _, rows = sheet_rows(load_workbook(out)["Exceptions"])
    checks.add(
        "workbook: a flagged engagement reaches the Exceptions tab with its decision owner",
        len(rows) == 1
        and rows[0]["engagement_id"] == eid
        and rows[0]["decision_owner"] == "engagement lead",
        f"{len(rows)} row(s), owner {rows[0]['decision_owner'] if rows else 'none'}",
    )


def check_skill_document(checks: Checks) -> None:
    """The skill is a deliverable too, and its frontmatter is what decides whether it fires."""
    text = SKILL.read_text() if SKILL.exists() else ""
    checks.add("skill: SKILL.md exists", bool(text), str(SKILL.relative_to(REPO_ROOT)))
    if not text:
        return

    front = text.split("---")[1] if text.startswith("---") else ""
    checks.add(
        "skill: frontmatter names the skill and describes when to use it",
        "name: assemble-delivery-pack" in front and "description:" in front and len(front) > 300,
        f"{len(front)} chars of frontmatter",
    )
    for phrase, what in (
        ("person_concentration_pct > 70", "the concentration trigger"),
        ("end_date on or before period_end", "the ended-inside-the-period trigger"),
        ("no `engagement_ids` argument", "the portfolio-wide coverage call"),
        ("Never compute a percentage", "the no-arithmetic rule"),
        ("Never re-baseline a ceiling", "the ceiling rule"),
        # Step 12. The stop condition it replaced named a ledger no tool could read, so
        # these check the instruction is now expressed in something callable.
        ('get_run_ledger(period="<YYYY-MM>")', "the run-ledger call, in callable form"),
        ("`first_run`", "what a period with no prior pack returns"),
        ("`unchanged`", "what an untouched period returns"),
        ("`arrivals_complete: false`", "the limit of what the watermark can enumerate"),
        ("use a new run id", "that a regenerated pack does not reuse the run id"),
    ):
        checks.add(f"skill: states {what}", phrase in text, "present" if phrase in text else "absent")

    prohibition = "never describe that week as a delivery slowdown"
    checks.add(
        "skill: forbids calling the low-coverage week a delivery slowdown, in those words",
        prohibition in text.lower(),
        "stated as a prohibition" if prohibition in text.lower() else "absent",
    )


async def run_seed(seed: int, anchors: dict[str, Any], checks: Checks, workdir: Path) -> None:
    toollog.logger.handlers.clear()
    toollog.logger.addHandler(logging.NullHandler())
    toollog.logger.propagate = False

    period_end = anchors.get("period_end") or "2026-08-31"
    check_skill_document(checks)

    async with Client(mcp) as client:
        pack = await gather(client, f"pack-check-seed-{seed}", period_end)

    checks.add(
        "pack: paging reached total_count before any analysis began",
        len(pack["engagements"]) == pack["total_count"] and pack["pages"] > 1,
        f"{len(pack['engagements'])} of {pack['total_count']} over {pack['pages']} pages",
    )
    checks.add(
        "pack: one portfolio-wide time summary, not one per engagement",
        pack["time_summary"].get("engagement_ids") is None
        and pack["time_summary"].get("group_by") == "engagement,week",
        "unscoped, grouped by engagement,week",
    )
    checks.add(
        "pack: detail was pulled for a subset rather than the whole portfolio",
        0 < len(pack["burn"]) < pack["total_count"],
        f"{len(pack['burn'])} of {pack['total_count']} engagements examined",
    )

    pack_path = workdir / f"pack-{seed}.json"
    pack_path.write_text(json.dumps(pack, default=str, indent=2))
    out = workdir / f"engagement-book-{PERIOD}-seed-{seed}.xlsx"

    result = subprocess.run(
        [sys.executable, str(BUILDER), str(pack_path), "--out", str(out)],
        capture_output=True, text=True,
    )
    checks.add(
        "build_workbook ran and wrote the workbook",
        result.returncode == 0 and out.exists(),
        result.stdout.strip()[:96] if result.returncode == 0 else result.stderr.strip()[:96],
    )
    if not out.exists():
        return

    check_workbook(out, json.loads(pack_path.read_text()), anchors, checks)
    check_exceptions_populate(json.loads(pack_path.read_text()), workdir, checks)
    check_builder_refusals(json.loads(pack_path.read_text()), workdir, checks)


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, action="append", help="Repeatable. Every fixture seed when omitted.")
    ap.add_argument("--dsn", default=db.DEFAULT_DSN)
    ap.add_argument("--no-reseed", action="store_true", help="Use the database as it stands.")
    ap.add_argument("--verbose", "-v", action="store_true", help="Print passing assertions too.")
    ap.add_argument("--keep", type=Path, help="Write the packs and workbooks here and leave them.")
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

    workdir = args.keep or Path(tempfile.mkdtemp(prefix="bizdata-pack-"))
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
            print(f"\nworkbooks left in {workdir}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)

    print()
    print("all seeds pass" if ok else "FAILURES above")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
