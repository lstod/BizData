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

After the rows land, the scoring model in db/seeds/ and the health views in db/views/ are
applied on every run. They are seed-independent reference objects, but the schema reset
drops the views along with the tables they read, so reapplying is not optional.
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

# Applied after the generated rows are in, in this order. The scoring model has to exist
# before the views that read it, each view has to exist before the one that reads it, and
# all of them have to be reapplied on every run because db/schema.sql drops the six tables
# with CASCADE, which takes the views with them.
#
# These are not generated data and do not depend on --seed. They are here rather than in
# the README as five more psql lines because a database seeded without them is a database
# where every step-3 tool fails on a missing relation.
POST_LOAD_PATHS = (
    REPO_ROOT / "db" / "seeds" / "scoring_weights.sql",
    REPO_ROOT / "db" / "views" / "portfolio_coverage_v1.sql",
    REPO_ROOT / "db" / "views" / "engagement_burn_v1.sql",
    REPO_ROOT / "db" / "views" / "engagement_financials_v1.sql",
    REPO_ROOT / "db" / "views" / "engagement_health_v1.sql",
    REPO_ROOT / "db" / "views" / "portfolio_summary_v1.sql",
)

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
        "--views-only",
        action="store_true",
        help="apply db/views/ and the scoring weights, load no rows; for adding a view to a "
             "database that already holds the right data",
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
        # --views-only exists for one situation, and step 7 is it: a new view has to reach a
        # database that already holds the right rows. Against Aurora the alternative is
        # pushing forty thousand time entries back over the Data API to change nothing, which
        # is slow enough that it gets skipped, and a view that gets skipped is the deployment
        # gap step 6 already paid for once. `create or replace view` makes this idempotent.
        if not args.views_only:
            if not args.no_schema:
                writer.apply_sql(SCHEMA_PATH.read_text())

            counts = {}
            for table in TABLE_ORDER:
                rows = getattr(portfolio, table)
                counts[table] = writer.load(table, COLUMNS[table], rows)

        for path in POST_LOAD_PATHS:
            writer.apply_sql(path.read_text())
    finally:
        writer.close()

    loaded = time.monotonic()

    print(f"seed {args.seed}, period {args.period}")
    if args.views_only:
        print("views only, no rows loaded")
    else:
        print(f"window {portfolio.window_start} to {portfolio.window_end}")
        for table in TABLE_ORDER:
            print(f"  {table:<16} {counts[table]:>7,}")
    for path in POST_LOAD_PATHS:
        print(f"  applied          {path.relative_to(REPO_ROOT)}")
    print(f"generated in {generated - started:.1f}s, loaded in {loaded - generated:.1f}s")

    if args.mess_report:
        args.mess_report.write_text(json.dumps(portfolio.mess, indent=2, default=str) + "\n")
        print(f"mess case anchors written to {args.mess_report}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
