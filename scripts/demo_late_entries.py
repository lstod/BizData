#!/usr/bin/env python3
"""File three timesheet entries late, so a live run has something to report.

    scripts/demo_late_entries.py --period 2026-07 --show
    scripts/demo_late_entries.py --period 2026-07 --insert
    scripts/demo_late_entries.py --period 2026-07 --remove

`get_run_ledger` returning `changed` is the one branch that cannot be demonstrated from
seeded data, because the seed is generated all at once: every entry in it was "filed" before
any pack existed. Something has to arrive *after* a run for there to be a late arrival, and
this is the smallest thing that makes that true.

**This writes to whichever database it is pointed at, and by default that is Aurora.** It is
the only script here other than seed.py that does. Three guards, because the failure mode is
a demo where every figure is quietly slightly wrong:

  - every row carries NOTE in its note column, and --remove deletes on exactly that string,
    so cleanup cannot take a real row with it
  - --insert refuses if marked rows are already present, so running it twice does not leave
    six behind and make the count in the transcript wrong
  - --show prints what is there and changes nothing, and is worth running before and after

Cleanup is not optional and not deferred to reseeding. Aurora is not reseeded between demos
the way local Postgres is, so a row left here outlives the recording it was for.

The entries are dated inside the period and filed after it closed, which is mess case 1
happening for real rather than being seeded: work done in the period, submitted once the
review had already been run.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

NOTE = "demo: filed after the period closed"
COUNT = 3
PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def period_bounds(period: str) -> tuple[dt.date, dt.date]:
    if not PERIOD_RE.match(period):
        raise SystemExit(f"--period must be YYYY-MM, got {period!r}")
    year, month = (int(p) for p in period.split("-"))
    start = dt.date(year, month, 1)
    end = dt.date(year + (month == 12), (month % 12) + 1, 1) - dt.timedelta(days=1)
    return start, end


def connect(backend: str):
    """Aurora through the master secret, or local Postgres. Never inferred.

    ``--backend`` has no default that reads the environment, deliberately. seed.py's does,
    and check_tools.py:132 records what that cost: in any shell where `terraform output
    shell_exports` has been evaluated, BIZDATA_DB_BACKEND says aws, and a script that trusts
    it writes to the deployment while the operator believes it is writing locally.
    """
    if backend == "aws":
        from writers import AwsWriter

        return AwsWriter()

    from writers import LocalWriter

    from server import db

    return LocalWriter(db.DEFAULT_DSN)


def query(writer, sql: str) -> list[dict]:
    api = getattr(writer, "api", None)
    if api is not None:
        return api.query(sql)
    with writer.conn.cursor() as cur:
        cur.execute(sql)
        columns = [d[0] for d in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def show(writer, period: str) -> int:
    rows = query(
        writer,
        "select t.id, p.name as person, e.name as engagement, t.entry_date, t.hours, t.submitted_at "
        "from time_entries t "
        "join people p on p.id = t.person_id "
        "join engagements e on e.id = t.engagement_id "
        f"where t.note = '{NOTE}' order by t.id",
    )
    if not rows:
        print(f"no demo rows present for {period}")
        return 0
    print(f"{len(rows)} demo row(s) present:")
    for r in rows:
        print(f"  {r['id']}  {r['person']:<22} {r['engagement'][:34]:<34} "
              f"{r['entry_date']}  {r['hours']}h  filed {r['submitted_at']}")
    return len(rows)


def insert(writer, period: str) -> None:
    start, end = period_bounds(period)
    if show(writer, period):
        raise SystemExit(
            "Demo rows are already present. Run --remove first, or the next run will report "
            "six arrivals and the number in your transcript will not match what you said."
        )

    # After the period closed, and after any watermark a run over this period could hold:
    # the seed's own latest filing for a period lands within days of its end.
    filed_at = f"{end + dt.timedelta(days=20)} 10:15:00+00"

    base = int(query(writer, "select coalesce(max(id), 0) as m from time_entries")[0]["m"])
    # One per engagement, and the latest entry on each. Taking the first three rows by id
    # gives three copies of the same person on the same engagement on the same day, which is
    # a worse demo and also indistinguishable from the duplicate-detection mess case. Spread
    # across engagements, "three entries arrived late" reads as three separate people
    # forgetting, which is what actually happens.
    sources = query(
        writer,
        "select max(id) as id from time_entries "
        f"where entry_date between '{start}' and '{end}' "
        f"group by engagement_id order by max(entry_date) desc, max(id) desc limit {COUNT}",
    )
    if len(sources) < COUNT:
        raise SystemExit(f"{period} has fewer than {COUNT} entries to copy from; pick another period")

    # Copied from real rows so the person and engagement are real and the foreign keys hold.
    # Only the hours, the note and the filing time are invented.
    for offset, source in enumerate(sources, start=1):
        writer.apply_sql(
            "insert into time_entries "
            "(id, person_id, engagement_id, entry_date, hours, billable, note, submitted_at) "
            f"select {base + offset}, person_id, engagement_id, entry_date, 1.25, true, "
            f"'{NOTE}', '{filed_at}'::timestamptz from time_entries where id = {source['id']}"
        )
    print(f"inserted {COUNT} entries dated inside {period}, filed {filed_at}\n")
    show(writer, period)


def remove(writer, period: str) -> None:
    before = show(writer, period)
    if not before:
        return
    writer.apply_sql(f"delete from time_entries where note = '{NOTE}'")
    print()
    remaining = show(writer, period)
    print("removed" if not remaining else f"WARNING: {remaining} row(s) still present")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--period", required=True, help="YYYY-MM. Entries land inside it and are filed after it.")
    ap.add_argument(
        "--backend", choices=("aws", "local"), default="aws",
        help="Which database to write to. Never read from the environment.",
    )
    action = ap.add_mutually_exclusive_group(required=True)
    action.add_argument("--show", action="store_true", help="Print the demo rows. Changes nothing.")
    action.add_argument("--insert", action="store_true")
    action.add_argument("--remove", action="store_true")
    args = ap.parse_args()

    period_bounds(args.period)
    writer = connect(args.backend)
    print(f"-- {args.backend} --")
    try:
        if args.show:
            show(writer, args.period)
        elif args.insert:
            insert(writer, args.period)
        else:
            remove(writer, args.period)
    finally:
        writer.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
