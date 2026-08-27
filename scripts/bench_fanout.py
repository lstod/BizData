#!/usr/bin/env python3
"""Step 13's Done-when conditions: a recorded baseline, and the proof it is the same pack.

    scripts/bench_fanout.py --seed 42                      local Postgres, 1 against 8
    scripts/bench_fanout.py --seed 42 --repeat 3 -v        three of each, median reported
    scripts/bench_fanout.py --backend aws --no-reseed --seed 42 --repeat 3

The step asks for three things and this asserts all three: that run time drops against a
recorded single-threaded baseline, that every portfolio-level number still comes from the
assembly step, and that the workbook is identical to the sequential version.

The first two are easy to claim and easy to get wrong in opposite directions, so both are
measured rather than argued.

**That the work is the same.** A faster run that made fewer calls is not a faster run, it
is a smaller one. So the tool-call multiset is compared between the two modes — every tool
with every argument, counted — out of the tool-call log rather than out of the harness's
own intentions. Same calls, same arguments, same number of them.

**That the calls actually overlapped.** A wall clock that drops is weak evidence, because a
warm cache drops it too. The stronger statement is arithmetic: latency_ms is measured inside
each handler, so a run whose wall clock is *below the sum of its own handler times* must
have had handlers running at the same time. There is no other way to get there. The
sequential run is the control and its wall clock sits above its own sum.

**That the pack is the same.** Not a checksum. build_workbook.py opens a bare Workbook() and
openpyxl stamps dcterms:created and current zip mtimes, so two byte-different files are the
normal result of one unchanged pack. check_pack.compare_workbooks reads the cells instead,
formulas as text, and this asserts it finds nothing.

One thing this script must never do is publish. The whole comparison rests on both runs
seeing the same run-ledger decision, and a publish between them would move the period from
first_run or changed to unchanged, which changes the Data Quality tab (build_workbook.py's
arrival_notes) and would read as a fan-out defect. gather() does not publish; the assertion
that both runs saw the same decision is here so that stays true rather than being assumed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from check_pack import BUILDER, compare_workbooks, gather  # noqa: E402
from check_tools import PERIOD, Checks, anchors_for, report, reseed  # noqa: E402
from mcp import Client  # noqa: E402

from server import db, toollog  # noqa: E402
from server.app import mcp  # noqa: E402

# Eight legs against a portfolio that examines ten engagements, so the tail is one short
# wave rather than a long one. Higher is not obviously better: Aurora Serverless v2 at low
# ACU will scale under a wide enough fan-out, and a run that makes the cluster grow is
# measuring the cluster.
DEFAULT_CONCURRENCY = 8

# Summed handler time is allowed to rise as far as it likes under fan-out and is not allowed
# to fall. Contention adds; it cannot subtract. A parallel run whose handlers took materially
# *less* time in total than the sequential one did less work — a call skipped, a result
# reused — and that is the failure worth catching. The upper side is deliberately unbounded:
# it is a property of the database's spare capacity rather than of this code, it moved from
# 4.2x to 1.7x on one Terraform variable, and a threshold on it would assert the size of the
# cluster. What "the same work" actually rests on is the call multiset, which is exact.
LESS_WORK_FLOOR = 0.95


def strip_run_ids(value: Any) -> Any:
    """The pack with every run_id removed, at every depth.

    Top-level is not enough. Every tool response carries its own run_id from the base
    Response model in server/tools/common.py, so a pack from a run called `seq-1` differs
    from one called `par-1` in a dozen nested places for no reason worth reporting.
    """
    if isinstance(value, dict):
        return {k: strip_run_ids(v) for k, v in value.items() if k != "run_id"}
    if isinstance(value, list):
        return [strip_run_ids(v) for v in value]
    return value


def call_multiset(lines: list[dict[str, Any]]) -> Counter[str]:
    """What was called, with what, how many times — order deliberately discarded.

    Order is the one thing fan-out is *supposed* to change, so comparing on it would fail
    the very run it is meant to validate. Everything else must match exactly.
    """
    return Counter(
        f"{line['tool']}({json.dumps(strip_run_ids(line.get('arguments') or {}), sort_keys=True)})"
        for line in lines
    )


class Run:
    """One pass through the skill's order of operations, timed."""

    def __init__(self, label: str, wall: float, pack: dict[str, Any], lines: list[dict[str, Any]]):
        self.label = label
        self.wall = wall
        self.pack = pack
        self.lines = lines

    @property
    def handler_seconds(self) -> float:
        return sum(line.get("latency_ms") or 0.0 for line in self.lines) / 1000

    @property
    def decision(self) -> str:
        return str((self.pack.get("run_ledger") or {}).get("decision"))


async def timed_run(label: str, run_id: str, period_end: str, concurrency: int) -> Run:
    async with Client(mcp) as client:
        started = time.perf_counter()
        pack = await gather(client, run_id, period_end, concurrency=concurrency)
        wall = time.perf_counter() - started
    return Run(label, wall, pack, toollog.recent(run_id))


def build(pack: dict[str, Any], workdir: Path, name: str) -> Path | None:
    pack_path = workdir / f"pack-{name}.json"
    pack_path.write_text(json.dumps(pack, default=str, indent=2))
    out = workdir / f"engagement-book-{PERIOD}-{name}.xlsx"
    result = subprocess.run(
        [sys.executable, str(BUILDER), str(pack_path), "--out", str(out)],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not out.exists():
        print(f"build_workbook failed for {name}: {result.stderr.strip()[:200]}", file=sys.stderr)
        return None
    return out


async def probe_transport(concurrency: int) -> None:
    """The control, and the reason the Aurora result is readable at all.

    A flat wall clock under fan-out has two completely different causes that look the same
    from outside: a path that cannot run two calls at once, and a database with no spare CPU
    to run them on. One is a bug in this design and the other is a line item.

    pg_sleep separates them, because it holds a connection without consuming any CPU. If the
    path overlaps at all, N concurrent sleeps finish in about the time of one. If they add
    up instead, something in the path is serialising and no amount of database capacity will
    help. Run against the deployed backend this answers for the whole chain — the shared
    boto3 client, the Data API, and anyio's worker threads — in one measurement.
    """
    sql = "select :n::int as n, pg_sleep(0.5) is null as slept"
    target = db.backend()

    def one(n: int) -> None:
        target.query(sql, {"n": n})

    one(0)  # discard the first, which pays for the client and the connection

    started = time.perf_counter()
    await asyncio.gather(*(asyncio.to_thread(one, n) for n in range(concurrency)))
    overlapped = time.perf_counter() - started

    started = time.perf_counter()
    for n in range(concurrency):
        one(n)
    serial = time.perf_counter() - started

    print()
    print(f"  transport control: {concurrency} x pg_sleep(0.5)")
    print(f"  {'concurrent':<14} {overlapped:>10.2f}s")
    print(f"  {'sequential':<14} {serial:>10.2f}s   {serial / overlapped:.1f}x")
    print(
        "  the path overlaps; anything flat above this line is capacity, not serialisation"
        if overlapped < serial / 2
        else "  the path does NOT overlap, and that is a defect rather than a capacity limit"
    )


def print_measurements(seed: int, backend: str, sequential: list[Run], parallel: list[Run],
                       concurrency: int) -> tuple[float, float]:
    """The table, and the two medians the assertions are made against."""
    examined = len(sequential[0].pack["burn"])
    calls = len(sequential[0].lines)

    print()
    print(
        f"seed {seed}, backend {backend}, {examined} of "
        f"{sequential[0].pack['total_count']} engagements examined, {calls} tool calls per run"
    )
    print()
    print(f"  {'':<14} {'wall clock':>11} {'handler time':>14} {'calls':>7}")
    for runs in (sequential, parallel):
        for i, run in enumerate(runs, 1):
            label = f"{run.label} #{i}" if len(runs) > 1 else run.label
            print(
                f"  {label:<14} {run.wall:>10.2f}s {run.handler_seconds:>13.2f}s "
                f"{len(run.lines):>7}"
            )

    seq_median = statistics.median(r.wall for r in sequential)
    par_median = statistics.median(r.wall for r in parallel)

    print()
    print(f"  {'median seq':<14} {seq_median:>10.2f}s")
    print(f"  {'median par':<14} {par_median:>10.2f}s   at concurrency {concurrency}")
    print(f"  {'speedup':<14} {seq_median / par_median:>10.2f}x")
    print()

    return seq_median, par_median


def assert_all(seed: int, sequential: list[Run], parallel: list[Run], seq_median: float,
               par_median: float, workdir: Path, checks: Checks) -> None:
    seq, par = sequential[-1], parallel[-1]

    checks.add(
        "the detail leg is wide enough for fan-out to mean anything",
        len(seq.pack["burn"]) > 1,
        f"{len(seq.pack['burn'])} engagements examined",
    )

    # ---- same work ------------------------------------------------------------------
    checks.add(
        "both modes made the same number of tool calls",
        len(seq.lines) == len(par.lines),
        f"{len(seq.lines)} sequential, {len(par.lines)} parallel",
    )
    seq_calls, par_calls = call_multiset(seq.lines), call_multiset(par.lines)
    extra = (seq_calls - par_calls) + (par_calls - seq_calls)
    checks.add(
        "the tool-call multiset is identical: same tools, same arguments, same counts",
        not extra,
        f"{len(seq_calls)} distinct calls, all matched" if not extra
        else f"{len(extra)} unmatched, first {next(iter(extra))[:80]}",
    )
    inflation = par.handler_seconds / max(seq.handler_seconds, 1e-9)
    checks.add(
        "the fan-out's handlers did at least as much work, so nothing was skipped",
        inflation >= LESS_WORK_FLOOR,
        f"{seq.handler_seconds:.2f}s sequential, {par.handler_seconds:.2f}s parallel, "
        f"{inflation:.2f}x — contention cost, and it is smaller than the saving",
    )

    # ---- the ledger saw the same thing both times -----------------------------------
    decisions = {r.decision for r in sequential + parallel}
    checks.add(
        "every run saw the same run-ledger decision, so nothing published between them",
        len(decisions) == 1,
        f"decision {seq.decision} throughout" if len(decisions) == 1 else f"decisions differ: {decisions}",
    )

    # ---- the calls actually overlapped ----------------------------------------------
    checks.add(
        "sequential wall clock is at or above its own summed handler time, as a control",
        seq.wall >= seq.handler_seconds * 0.98,
        f"{seq.wall:.2f}s wall against {seq.handler_seconds:.2f}s of handlers",
    )
    checks.add(
        "parallel wall clock is below its own summed handler time, which needs overlap",
        par.wall < par.handler_seconds,
        f"{par.wall:.2f}s wall against {par.handler_seconds:.2f}s of handlers",
    )

    # ---- and it is faster -------------------------------------------------------------
    checks.add(
        "run time drops against the recorded single-threaded baseline",
        par_median < seq_median,
        f"{seq_median:.2f}s to {par_median:.2f}s, {seq_median / par_median:.2f}x",
    )

    # ---- and it is the same pack ------------------------------------------------------
    checks.add(
        "the packs are equal once run ids are stripped at every depth",
        strip_run_ids(json.loads(json.dumps(seq.pack, default=str)))
        == strip_run_ids(json.loads(json.dumps(par.pack, default=str))),
        "identical",
    )

    sequential_book = build(seq.pack, workdir, "sequential")
    parallel_book = build(par.pack, workdir, "parallel")
    if not sequential_book or not parallel_book:
        checks.add("both workbooks built", False, "see stderr")
        return
    checks.add("both workbooks built", True, f"{sequential_book.name}, {parallel_book.name}")

    # The run id is the one cell that must differ, because the two runs are two runs and
    # step 12 requires a regenerated pack to take a new id. Asserted in both directions:
    # that the raw comparison finds exactly that cell and nothing else, and that renaming
    # it leaves the two workbooks with nothing between them at all.
    raw = compare_workbooks(sequential_book, parallel_book)
    checks.add(
        "the run id is the only cell that differs before it is accounted for",
        len(raw) == 1 and repr(seq.pack["run_id"]) in raw[0] and repr(par.pack["run_id"]) in raw[0],
        raw[0][:96] if len(raw) == 1 else f"{len(raw)} difference(s)",
    )

    differences = compare_workbooks(
        sequential_book, parallel_book, aliases={par.pack["run_id"]: seq.pack["run_id"]}
    )
    checks.add(
        "the workbook is identical to the sequential version, cell by cell",
        not differences,
        "no differences across five tabs, formulas compared as text" if not differences
        else f"{len(differences)} difference(s), first: {differences[0][:96]}",
    )


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--dsn", default=db.DEFAULT_DSN)
    ap.add_argument("--no-reseed", action="store_true", help="Use the database as it stands.")
    ap.add_argument("--verbose", "-v", action="store_true", help="Print passing assertions too.")
    ap.add_argument("--keep", type=Path, help="Write the packs and workbooks here and leave them.")
    ap.add_argument(
        "--backend", choices=("local", "aws"), default="local",
        help="'aws' drives the same sequence over the RDS Data API. Implies --no-reseed.",
    )
    ap.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    ap.add_argument("--repeat", type=int, default=1, help="Runs of each mode; the median is reported.")
    ap.add_argument(
        "--probe", action="store_true",
        help="Also run the pg_sleep control, which separates serialisation from capacity.",
    )
    args = ap.parse_args()

    if args.backend == "aws" and not args.no_reseed:
        ap.error("--backend aws expects --no-reseed and a --seed matching what Aurora holds")
    if args.concurrency < 2:
        ap.error("--concurrency below 2 is not a fan-out")

    db._backend = db.get_backend(args.backend, args.dsn)

    if args.backend == "aws":
        from warm import warm_database

        print(f"database  awake in {warm_database():.1f}s")

    if not args.no_reseed:
        reseed(args.seed, args.dsn)

    period_end = anchors_for(args.seed).get("period_end") or "2026-08-31"
    workdir = args.keep or Path(tempfile.mkdtemp(prefix="bizdata-fanout-"))
    workdir.mkdir(parents=True, exist_ok=True)

    checks = Checks(args.seed)
    try:
        # A discarded run first, so neither measured mode pays for the cold cluster, the
        # first Data API client, or the SQL files being read off disk into load_sql's cache.
        # Whichever mode went first would otherwise carry all of it, and sequential goes
        # first — which would flatter the result rather than test it.
        await timed_run("warmup", "fanout-warmup", period_end, 1)

        sequential, parallel = [], []
        for i in range(1, args.repeat + 1):
            sequential.append(await timed_run("sequential", f"fanout-seq-{i}", period_end, 1))
            parallel.append(
                await timed_run("fan-out", f"fanout-par-{i}", period_end, args.concurrency)
            )

        seq_median, par_median = print_measurements(
            args.seed, args.backend, sequential, parallel, args.concurrency
        )
        if args.probe:
            await probe_transport(args.concurrency)
        assert_all(args.seed, sequential, parallel, seq_median, par_median, workdir, checks)
    except Exception as exc:  # noqa: BLE001
        while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
            exc = exc.exceptions[0]
        checks.add("harness ran to completion", False, f"{type(exc).__name__}: {exc}"[:160])
    finally:
        if args.keep:
            print(f"workbooks left in {workdir}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)

    ok = report(args.seed, checks, args.verbose)
    print()
    print("fan-out proven" if ok else "FAILURES above")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
