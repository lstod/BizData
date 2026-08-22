#!/usr/bin/env python3
"""Step 3's Done-when conditions, as assertions.

    scripts/check_tools.py                  every fixture seed, reseeding each
    scripts/check_tools.py --seed 42        one seed, against the database as it stands
    scripts/check_tools.py --no-reseed      whatever is loaded now

The SQL checks in db/checks/ assert things about the data. This asserts things about the
tools: that all four answer, that their responses validate against the output schemas they
publish, that the mess cases surface through the tool surface rather than only in a query,
that nothing exceeds the Data API's 1 MiB response cap, and that every call leaves exactly
one complete line in the tool-call log.

It runs through ``Client(mcp)``, which connects to the server object in memory — no port,
no transport, no uvicorn. That is the same code path a real client takes from
``call_tool`` down, so it exercises argument parsing, the log wrapper, output-schema
validation and serialisation. What it does not exercise is HTTP, which is what the separate
uvicorn check in the README covers.

Anchors come from ``seed.py --mess-report`` rather than being re-derived here. The
generator already publishes which engagement is mess case 3 and which week is case 7, and a
second implementation of "find the silent engagement" in Python would be a second thing to
keep in step with the generator.

Every seed, not just 42, because that breadth found a real defect at step 1 and again at
step 2.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from generator import generate  # noqa: E402
from mcp import Client  # noqa: E402

from server import db, toollog  # noqa: E402
from server.app import mcp  # noqa: E402
from server.tools.get_time_summary import GROUPINGS  # noqa: E402

FIXTURE_SEEDS = (42, 43, *range(9001, 9016))
PERIOD = "2026-08"
MIB = 1024 * 1024

LOG_FIELDS = ("run_id", "tool", "arguments", "total_count", "returned_count", "latency_ms", "scoring_model_version")

# The parameter contract for every file in db/sql/, pinned here so that adding a
# placeholder without binding it is a failed check rather than a runtime error on whichever
# call happens to hit that branch first.
EXPECTED_PARAMS = {
    "list_engagements": {"as_of_date", "status", "client_id", "cursor", "limit"},
    "get_engagement_burn": {"engagement_id", "as_of_date"},
    "get_financials": {"engagement_id", "period"},
    "get_time_summary": {"engagement_ids", "period_start", "period_end", "max_rows"},
    "get_time_summary_quality": {"engagement_ids", "period_start", "period_end"},
    "get_time_summary_completeness": {"period_start", "period_end"},
}


class Checks:
    """Collects results so a failure reports every other assertion too, rather than the
    first one. A run that stops at the first failure hides whether one thing broke or ten."""

    def __init__(self, seed: int) -> None:
        self.seed = seed
        self.results: list[tuple[str, bool, str]] = []

    def add(self, name: str, ok: bool, detail: str = "") -> bool:
        self.results.append((name, bool(ok), detail))
        return bool(ok)

    @property
    def failed(self) -> list[tuple[str, bool, str]]:
        return [r for r in self.results if not r[1]]


class LogCapture(logging.Handler):
    """Everything the tool-call log emitted, parsed."""

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[dict[str, Any]] = []
        self.raw: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        text = record.getMessage()
        self.raw.append(text)
        try:
            self.lines.append(json.loads(text))
        except json.JSONDecodeError:
            self.lines.append({"__unparseable__": text})


def anchors_for(seed: int) -> dict[str, Any]:
    """The mess case anchors, straight from the generator.

    ``portfolio.mess`` is what ``seed.py --mess-report`` writes out, and it is built before
    anything touches a database, so the anchors are available whether or not this run
    reseeds. Same source either way, so the two paths cannot drift.
    """
    return json.loads(json.dumps(generate(seed, PERIOD).mess, default=str))


def reseed(seed: int, dsn: str) -> None:
    subprocess.run(
        [
            sys.executable, str(REPO_ROOT / "scripts" / "seed.py"),
            "--seed", str(seed), "--period", PERIOD, "--dsn", dsn,
        ],
        check=True, capture_output=True,
    )


class Harness:
    def __init__(self, client: Client, capture: LogCapture, checks: Checks) -> None:
        self.client = client
        self.capture = capture
        self.checks = checks
        self.run_id = f"check-seed-{checks.seed}"
        self.schemas: dict[str, dict[str, Any]] = {}

    async def call(self, tool: str, **arguments: Any) -> dict[str, Any]:
        """One tool call, with the log line and the response size asserted around it."""
        before = len(self.capture.lines)
        result = await self.client.call_tool(tool, {"run_id": self.run_id, **arguments})

        if getattr(result, "is_error", False):
            text = "; ".join(getattr(c, "text", "") for c in (result.content or []))
            raise AssertionError(f"{tool} returned an error: {text}")

        payload = result.structured_content
        emitted = self.capture.lines[before:]

        self.checks.add(
            f"{tool}: exactly one log line",
            len(emitted) == 1,
            f"{len(emitted)} line(s)",
        )
        if len(emitted) == 1:
            line = emitted[0]
            missing = [f for f in LOG_FIELDS if line.get(f) is None]
            self.checks.add(
                f"{tool}: log line has all seven fields",
                not missing,
                "all populated" if not missing else f"null or absent: {', '.join(missing)}",
            )
            self.checks.add(
                f"{tool}: log line carries the caller's run_id",
                line.get("run_id") == self.run_id,
                str(line.get("run_id")),
            )
            self.checks.add(
                f"{tool}: log line counts match the response",
                line.get("total_count") == payload.get("total_count")
                and line.get("returned_count") == payload.get("returned_count"),
                f"log {line.get('total_count')}/{line.get('returned_count')} "
                f"vs response {payload.get('total_count')}/{payload.get('returned_count')}",
            )

        size = len(json.dumps(payload, default=str).encode())
        self.checks.add(
            f"{tool}: response under 1 MiB",
            size < MIB,
            f"{size:,} bytes, {100 * size / MIB:.1f}% of the cap",
        )

        self.checks.add(
            f"{tool}: response validates against its published output schema",
            *validate(payload, self.schemas.get(tool)),
        )
        self.checks.add(
            f"{tool}: response carries scoring_model_version",
            bool(payload.get("scoring_model_version")),
            str(payload.get("scoring_model_version")),
        )
        return payload


def validate(payload: dict[str, Any], schema: dict[str, Any] | None) -> tuple[bool, str]:
    if schema is None:
        return False, "no output schema published"
    try:
        import jsonschema

        jsonschema.validate(json.loads(json.dumps(payload, default=str)), schema)
    except Exception as exc:  # noqa: BLE001 - the message is the detail
        return False, str(exc).splitlines()[0][:110]
    return True, "valid"


def check_sql_files(checks: Checks) -> None:
    """Every placeholder resolves, nothing is left behind, and the contract is what the
    tools bind. The hazard the rewrite has to survive is ``::date``, of which there are
    plenty in db/views/ and a few here."""
    names = set(db.all_query_names())
    checks.add(
        "db/sql: every query file is in the parameter contract",
        names == set(EXPECTED_PARAMS),
        f"{len(names)} file(s); unlisted: {sorted(names - set(EXPECTED_PARAMS)) or 'none'}",
    )

    for name in sorted(names & set(EXPECTED_PARAMS)):
        text = db.load_sql(name)
        found = db.placeholders(text)
        checks.add(
            f"db/sql/{name}.sql: placeholders are exactly the contract",
            found == EXPECTED_PARAMS[name],
            f"{sorted(found)}",
        )

        rewritten = db.to_pyformat(text.replace("{group_select}", "1").replace("{group_by}", "1"))
        stray = [seg for kind, seg in db._scan(rewritten) if kind == "param"]
        checks.add(
            f"db/sql/{name}.sql: no colon placeholder survives the rewrite",
            not stray,
            "clean" if not stray else f"left behind: {stray}",
        )

        casts = text.count("::")
        checks.add(
            f"db/sql/{name}.sql: {casts} cast(s) survive the rewrite intact",
            rewritten.count("::") == casts,
            f"{rewritten.count('::')} of {casts}",
        )


def check_published_schemas(tools: Any, checks: Checks) -> dict[str, dict[str, Any]]:
    """The log wrapper did not eat the signatures.

    functools.wraps is what keeps this true — MCPServer derives the input schema with
    inspect.signature, which follows __wrapped__. Without it every tool would publish
    (*args, **kwargs), which no client could call and which nothing else here would catch.
    """
    expected_inputs = {
        "list_engagements": {"as_of_date", "status", "client_id", "cursor", "limit", "run_id"},
        "get_engagement_burn": {"engagement_id", "as_of_date", "run_id"},
        "get_time_summary": {"period_start", "period_end", "group_by", "engagement_ids", "run_id"},
        "get_financials": {"engagement_id", "period", "run_id"},
    }
    published = {t.name: t for t in tools.tools}

    checks.add(
        "tools/list publishes exactly the four read tools",
        set(published) == set(expected_inputs),
        ", ".join(sorted(published)),
    )

    schemas: dict[str, dict[str, Any]] = {}
    for name, wanted in expected_inputs.items():
        tool = published.get(name)
        if tool is None:
            checks.add(f"{name}: published", False, "absent from tools/list")
            continue
        got = set(tool.input_schema.get("properties", {}))
        checks.add(
            f"{name}: input schema survived the log wrapper",
            got == wanted,
            f"{sorted(got)}",
        )
        checks.add(
            f"{name}: run_id is optional",
            "run_id" not in tool.input_schema.get("required", []),
            f"required: {sorted(tool.input_schema.get('required', []))}",
        )
        checks.add(
            f"{name}: publishes an output schema",
            bool(tool.output_schema),
            "present" if tool.output_schema else "absent",
        )
        checks.add(
            f"{name}: description carries the guidance a caller needs",
            bool(tool.description) and len(tool.description) > 200,
            f"{len(tool.description or '')} chars",
        )
        schemas[name] = tool.output_schema
    return schemas


async def run_seed(seed: int, anchors: dict[str, Any], checks: Checks) -> None:
    capture = LogCapture()
    toollog.logger.handlers.clear()
    toollog.logger.addHandler(capture)
    toollog.logger.setLevel(logging.INFO)
    toollog.logger.propagate = False

    check_sql_files(checks)

    period_start = f"{PERIOD}-01"
    week = anchors["case_7_week_start"]
    period_end = anchors.get("period_end") or "2026-08-31"

    async with Client(mcp) as client:
        tools = await client.list_tools()
        h = Harness(client, capture, checks)
        h.schemas = check_published_schemas(tools, checks)

        # ---- list_engagements, and the paging contract -------------------------------
        first = await h.call("list_engagements", as_of_date=period_end, status="active", limit=5)
        total = first["total_count"]
        active = anchors["active_engagement_ids"]

        checks.add(
            "list_engagements: total_count matches the seeded active engagements",
            total == len(active),
            f"{total} vs {len(active)} seeded",
        )

        seen: list[int] = [e["engagement_id"] for e in first["engagements"]]
        triage: dict[int, dict[str, Any]] = {e["engagement_id"]: e for e in first["engagements"]}
        cursor, pages = first["next_cursor"], 1
        while cursor:
            page = await h.call("list_engagements", as_of_date=period_end, status="active", limit=5, cursor=cursor)
            seen += [e["engagement_id"] for e in page["engagements"]]
            triage.update({e["engagement_id"]: e for e in page["engagements"]})
            cursor, pages = page["next_cursor"], pages + 1
            if pages > 50:
                break

        checks.add(
            "list_engagements: paging reaches total_count and terminates",
            len(seen) == total and cursor is None,
            f"{len(seen)} of {total} over {pages} page(s), next_cursor {cursor!r}",
        )
        checks.add(
            "list_engagements: no row repeated or skipped across pages",
            sorted(seen) == sorted(active),
            f"{len(set(seen))} distinct" + ("" if sorted(seen) == sorted(active) else f"; expected {active}"),
        )
        checks.add(
            "list_engagements: every row carries its record id",
            all(isinstance(e.get("engagement_id"), int) for e in first["engagements"]),
            "all ids present",
        )
        checks.add(
            "list_engagements: burn and health come back on the row, so triage is one call",
            all(e.get("burn_pct") is not None and e.get("health_band") for e in first["engagements"]),
            "burn_pct and health_band populated",
        )
        # Step 6. A triage filter can only fire on what the triage row carries, so
        # concentration has to be here and not only on the per-engagement call the filter is
        # deciding whether to make. Null is legitimate for an engagement that logged nothing
        # in the period, so the assertion is that the portfolio is readable on this axis
        # rather than that every single row is populated.
        with_conc = [e for e in triage.values() if e.get("person_concentration_pct") is not None]
        checks.add(
            "list_engagements: key person concentration comes back on the row too",
            len(with_conc) == len(triage) and all(e.get("people_count") for e in with_conc),
            f"{len(with_conc)} of {len(triage)} rows, people_count alongside",
        )

        # ---- mess case 3: silent engagement, low confidence, unasked -----------------
        silent = anchors["case_3_silent_engagement"]
        burn = await h.call("get_engagement_burn", engagement_id=silent, as_of_date=period_end)
        checks.add(
            "mess case 3: get_engagement_burn drops projection_confidence to low, unasked",
            burn["projection_confidence"] == "low",
            f"engagement {silent}, confidence {burn['projection_confidence']}",
        )
        checks.add(
            "mess case 3: the low confidence arrives with a reason attached",
            bool(burn.get("confidence_reason")) and len(burn["confidence_reason"]) > 20,
            burn.get("confidence_reason", "")[:96],
        )
        checks.add(
            "mess case 3: the reporting gap is visible as days since last entry",
            burn["days_since_last_entry"] >= 14,
            f"{burn['days_since_last_entry']} days",
        )

        # ---- mess case 1: late filing pushes confidence down on its own --------------
        for eid, rate in anchors.get("case_1_late_rates", {}).items():
            if rate["of"] and rate["late"] / rate["of"] > 0.10:
                late = await h.call("get_engagement_burn", engagement_id=int(eid), as_of_date=period_end)
                checks.add(
                    f"mess case 1: engagement {eid} over the 10% late threshold reads low confidence",
                    late["projection_confidence"] == "low"
                    and "filed after the period closed" in late["confidence_reason"],
                    f"{late['late_entry_pct']}% late, {late['confidence_reason'][:60]}",
                )
                break

        # ---- mess case 8: key person concentration -----------------------------------
        concentrated = anchors["case_8_concentration_engagement"]
        conc = await h.call("get_engagement_burn", engagement_id=concentrated, as_of_date=period_end)
        expected_share = round(100 * anchors["case_8_share"], 1)
        checks.add(
            "mess case 8: person_concentration_pct matches the seeded share",
            conc["person_concentration_pct"] is not None
            and abs(conc["person_concentration_pct"] - expected_share) <= 0.2,
            f"engagement {concentrated} at {conc['person_concentration_pct']}%, seeded {expected_share}%",
        )

        # Step 6, and the reason the column was added. The pack decides what to examine from
        # list_engagements, so a risk that is only expressible after get_engagement_burn is a
        # risk the pack never reaches. On seed 42 this engagement sits at 67.5% burn in the
        # green band and clears no burn-or-band filter that exists.
        triage_row = triage.get(concentrated, {})
        checks.add(
            "mess case 8: the concentrated engagement is findable from the triage call alone",
            triage_row.get("person_concentration_pct") is not None
            and abs(triage_row["person_concentration_pct"] - expected_share) <= 0.2,
            f"engagement {concentrated} at {triage_row.get('person_concentration_pct')}% on the "
            f"triage row, burn {triage_row.get('burn_pct')}%, band {triage_row.get('health_band')}",
        )
        checks.add(
            "mess case 8: triage and get_engagement_burn report one concentration, not two",
            triage_row.get("person_concentration_pct") == conc["person_concentration_pct"]
            and triage_row.get("people_count") == conc["people_count"],
            f"{triage_row.get('person_concentration_pct')}% over "
            f"{triage_row.get('people_count')} people from both tools",
        )

        # ---- mess case 6: engagement ending mid period -------------------------------
        ending = anchors["case_6_mid_period_end_engagement"]
        mid = await h.call("get_engagement_burn", engagement_id=ending, as_of_date=period_end)
        checks.add(
            "mess case 6: a mid-period end reads as_of at the end date, not the period end",
            str(mid["as_of"]) == anchors["case_6_end_date"] and mid["days_remaining"] == 0,
            f"engagement {ending}, as_of {mid['as_of']}, {mid['days_remaining']} days remaining",
        )

        # ---- mess case 4: healthy burn, negative margin, neither reconciled ----------
        fixed = anchors["case_4_fixed_fee_engagement"]
        fin = await h.call("get_financials", engagement_id=fixed, period=PERIOD)
        fixed_burn = await h.call("get_engagement_burn", engagement_id=fixed, as_of_date=period_end)
        seeded_margin = round(100 * float(anchors["case_4_margin_pct"]), 1)
        checks.add(
            "mess case 4: margin_pct is negative beside a burn that looks healthy",
            fin["margin_pct"] < 0 and 50 <= fixed_burn["burn_pct"] <= 80,
            f"engagement {fixed}, margin {fin['margin_pct']}%, burn {fixed_burn['burn_pct']}%",
        )
        checks.add(
            "mess case 4: margin_pct matches the seeded figure, computed in SQL",
            abs(fin["margin_pct"] - seeded_margin) <= 0.2,
            f"{fin['margin_pct']}% vs {seeded_margin}% seeded",
        )
        checks.add(
            "get_financials: payment behaviour is measured against the client's own baseline",
            "dso_days" in fin and "dso_baseline_days" in fin,
            f"dso {fin.get('dso_days')} against baseline {fin.get('dso_baseline_days')}",
        )

        # ---- get_time_summary, all five groupings ------------------------------------
        for mode in GROUPINGS:
            summary = await h.call(
                "get_time_summary", period_start=period_start, period_end=period_end, group_by=mode
            )
            checks.add(
                f"get_time_summary[{mode}]: aggregates rather than returning entries",
                0 < summary["returned_count"] <= 200 and not summary["truncated"],
                f"{summary['returned_count']} rows from {summary['data_quality']['entry_count']:,} entries",
            )

        summary = await h.call(
            "get_time_summary", period_start=period_start, period_end=period_end, group_by="engagement"
        )

        # ---- mess case 7: the unreadable week ----------------------------------------
        weeks = {str(w["week_start"]): w for w in summary["data_completeness"]["weeks"]}
        gap_week = weeks.get(week)
        expected_pct = round(100 * len(anchors["case_7_reporting_engagements"]) / len(active), 1)
        checks.add(
            "mess case 7: the low-coverage week is reported in data_completeness",
            gap_week is not None and gap_week["firm_wide_gap"],
            f"week of {week}, gap flagged {gap_week and gap_week['firm_wide_gap']}",
        )
        checks.add(
            "mess case 7: pct_active_reporting matches the seeded coverage",
            gap_week is not None and abs(gap_week["pct_active_reporting"] - expected_pct) <= 0.2,
            f"{gap_week and gap_week['pct_active_reporting']}% reporting, seeded {expected_pct}%",
        )
        checks.add(
            "mess case 7: exactly one week in the period is below the coverage floor",
            summary["data_completeness"]["weeks_with_gap"] == 1,
            f"{summary['data_completeness']['weeks_with_gap']} week(s) flagged",
        )

        # ---- mess cases 2 and 5: individually bad records ----------------------------
        quality = summary["data_quality"]
        checks.add(
            "mess case 2: the duplicated entry is reported in data_quality",
            quality["suspected_duplicates"] >= 1,
            f"{quality['suspected_duplicates']} redundant row(s)",
        )
        checks.add(
            "mess case 5: the unset billable flags are reported in data_quality",
            quality["null_billable"] == anchors["case_5_count"],
            f"{quality['null_billable']} of {anchors['case_5_count']} seeded",
        )
        checks.add(
            "mess case 1: late filings are reported in data_quality",
            quality["late_entries"] > 0 and quality["late_entry_pct"] is not None,
            f"{quality['late_entries']} entries, {quality['late_entry_pct']}%",
        )
        checks.add(
            "data_quality and data_completeness are separate blocks",
            set(summary) >= {"data_quality", "data_completeness"},
            "both present on one response",
        )

        # ---- run_id falls back when the caller does not set one ----------------------
        before = len(capture.lines)
        unset = await client.call_tool("get_financials", {"engagement_id": fixed, "period": PERIOD})
        minted = capture.lines[before:]
        checks.add(
            "run_id: the server mints one when the caller omits it",
            len(minted) == 1
            and str(minted[0].get("run_id", "")).startswith("req-")
            and minted[0]["run_id"] == unset.structured_content["run_id"],
            f"{minted[0].get('run_id') if minted else 'no line'}, echoed in the response",
        )

        # ---- a bad argument fails loudly rather than silently widening the query -----
        bad = await client.call_tool(
            "get_time_summary",
            {"period_start": period_start, "period_end": period_end, "group_by": "'; drop table time_entries; --"},
        )
        checks.add(
            "get_time_summary: an unknown group_by is rejected before reaching SQL",
            bool(getattr(bad, "is_error", False)),
            "rejected",
        )

    checks.add(
        "every log line emitted this seed was parseable JSON",
        all("__unparseable__" not in line for line in capture.lines),
        f"{len(capture.lines)} line(s)",
    )


def _first_cause(exc: BaseException) -> str:
    """The innermost message, since a nested ExceptionGroup buries it several layers down."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return f"{type(exc).__name__}: {exc}".replace("\n", " ")[:160]


def report(seed: int, checks: Checks, verbose: bool) -> bool:
    failed = checks.failed
    if verbose or failed:
        print(f"\nseed {seed}")
        print(f"{'':>3}  {'ok':<3} {'assertion':<74} detail")
        for i, (name, ok, detail) in enumerate(checks.results, 1):
            if verbose or not ok:
                print(f"{i:>3}  {'t' if ok else 'F':<3} {name[:74]:<74} {detail}")
    print(
        f"seed {seed}: {len(checks.results) - len(failed)} of {len(checks.results)} assertions passed"
        + ("" if not failed else f" -- {len(failed)} FAILED")
    )
    return not failed


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, action="append", help="Repeatable. Every fixture seed when omitted.")
    ap.add_argument("--dsn", default=db.DEFAULT_DSN)
    ap.add_argument("--no-reseed", action="store_true", help="Use the database as it stands.")
    ap.add_argument("--verbose", "-v", action="store_true", help="Print passing assertions too.")
    ap.add_argument(
        "--backend",
        choices=("local", "aws"),
        default="local",
        help=(
            "Which database to drive the tools against. 'aws' is the RDS Data API and is "
            "how step 5 proves the two backends agree: the same assertions, the same seed, "
            "one swapped adapter. It implies --no-reseed unless you really do mean to "
            "reload Aurora."
        ),
    )
    args = ap.parse_args()

    seeds = args.seed or list(FIXTURE_SEEDS)
    if args.no_reseed and len(seeds) > 1:
        ap.error("--no-reseed needs a single --seed, since it cannot change the loaded data")
    if args.backend == "aws" and not args.no_reseed:
        ap.error(
            "--backend aws expects --no-reseed. Reseeding Aurora takes half a minute per "
            "seed over the Data API and is a separate deliberate act: "
            "`python scripts/seed.py --seed N --period 2026-08`."
        )

    # Anchors come from the generator rather than the database, so the harness does not
    # care which backend is underneath. That is exactly what makes this a parity check:
    # every assertion below was written for local Postgres and none of them was touched.
    db._backend = db.get_backend(args.backend, args.dsn)

    ok = True
    for seed in seeds:
        if not args.no_reseed:
            reseed(seed, args.dsn)
        checks = Checks(seed)
        try:
            await run_seed(seed, anchors_for(seed), checks)
        except Exception as exc:  # noqa: BLE001
            # Including ExceptionGroup, which is what a failure inside the client's task
            # group arrives as. One seed aborting should still report the assertions it
            # got through and still let the remaining seeds run.
            checks.add("harness ran to completion", False, _first_cause(exc))
        ok &= report(seed, checks, args.verbose)

    print()
    print("all seeds pass" if ok else "FAILURES above")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
