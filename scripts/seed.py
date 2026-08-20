#!/usr/bin/env python3
"""Seed a BizData database.

    python scripts/seed.py --seed 42 --period 2026-08

Deterministic: the same seed and period produce byte-identical tables, which is what
db/checks/checksums.sql exists to prove. A different seed produces a different but
equally reproducible consultancy, which is what makes the reserved fixture seeds in the
README usable as a comparable scorecard for project #2.

The schema is dropped and recreated by default. Every row in this database is synthetic
and regenerating it costs seconds, so a reset is the sane default; pass --no-schema to
load on top of an existing schema instead.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from generator import COLUMNS, generate  # noqa: E402
from writers import TABLE_ORDER, get_writer  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "db" / "schema.sql"

PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate and load a synthetic professional services portfolio.",
    )
    parser.add_argument("--seed", type=int, required=True, help="integer seed; determines the portfolio")
    parser.add_argument("--period", required=True, help="demo period as YYYY-MM, e.g. 2026-08")
    parser.add_argument("--dsn", help="Postgres DSN; defaults to BIZDATA_DSN or the compose database")
    parser.add_argument(
        "--backend",
        choices=("local", "aws"),
        help="overrides BIZDATA_DB_BACKEND. aws lands at step 5",
    )
    parser.add_argument(
        "--no-schema",
        action="store_true",
        help="skip applying db/schema.sql, which otherwise drops and recreates every table",
    )
    parser.add_argument(
        "--mess-report",
        type=Path,
        help="write the mess case anchors to this path as JSON",
    )
    args = parser.parse_args(argv)
    if not PERIOD_RE.match(args.period):
        parser.error(f"--period must look like YYYY-MM, got {args.period!r}")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    started = time.monotonic()
    portfolio = generate(args.seed, args.period)
    generated = time.monotonic()

    try:
        writer = get_writer(args.backend, args.dsn)
    except NotImplementedError as exc:
        # The aws backend is a deliberate stub until step 5. A traceback would suggest
        # something broke rather than that something has not been built yet.
        raise SystemExit(str(exc)) from None

    try:
        if not args.no_schema:
            writer.apply_schema(SCHEMA_PATH.read_text())

        counts = {}
        for table in TABLE_ORDER:
            rows = getattr(portfolio, table)
            counts[table] = writer.load(table, COLUMNS[table], rows)
    finally:
        writer.close()

    loaded = time.monotonic()

    print(f"seed {args.seed}, period {args.period}")
    print(f"window {portfolio.window_start} to {portfolio.window_end}")
    for table in TABLE_ORDER:
        print(f"  {table:<16} {counts[table]:>7,}")
    print(f"generated in {generated - started:.1f}s, loaded in {loaded - generated:.1f}s")

    if args.mess_report:
        args.mess_report.write_text(json.dumps(portfolio.mess, indent=2, default=str) + "\n")
        print(f"mess case anchors written to {args.mess_report}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
