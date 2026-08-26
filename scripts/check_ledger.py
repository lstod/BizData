#!/usr/bin/env python3
"""Step 12's Done-when conditions, as assertions.

    scripts/check_ledger.py                  every fixture seed, reseeding each
    scripts/check_ledger.py --seed 42 -v     one seed, against the database as it stands
    scripts/check_ledger.py --keep /tmp/out  leave each seed's archive behind

check_publish.py asserts what happens to a pack when it is archived. This asserts what the
*next* run makes of it: that a period nobody has published reads as a first run, that
republishing an unchanged period is refused work rather than a second pack, and that three
entries filed since the last run come back named.

**This is the only harness in the repository that writes to the database.** Every other one
reseeds and reads. Inserting rows is the whole point here — late data cannot be simulated
from a fixture, because the fixture is generated all at once — and it makes this the one
script that could do real damage if it reached the wrong database. Every write goes through
``_late_entries``, which is hardcoded to psycopg against the local DSN and never consults
``BIZDATA_DB_BACKEND``. See the comment there: this is the same trap that reseeded Aurora
sixteen times at step 9.

Three assertions here are not Done-when conditions and are the ones most likely to catch a
regression:

    the digest is identical under five timezone and DateStyle combinations
    a digest change the watermark cannot enumerate is described rather than denied
    restoring an amended value returns the period to unchanged

The first is the cross-backend property the whole design rests on. The digest is text built
by string_agg, and Postgres renders dates and timestamps through session settings that local
psycopg and the Data API arrive at by different routes — per-connection ``set time zone`` in
server/db.py against a role-level GUC in db/seeds/mcp_readonly.sql, which scripts/writers.py
does not have because it connects as a different role. A digest that moved with the session
would report every period as changed the first time it was read through Aurora.

The third is what makes "idempotent" mean something. A change counter would call a period
changed forever once anything touched it; a fingerprint says the figures are back where they
were, and that is the difference between a stop condition and a nuisance.

What this harness cannot prove is S3's behaviour. LocalArchive returns None for a missing
file; S3 returns **403** rather than 404 for a key that is not there when the caller has no
ListBucket, which this role deliberately does not. So every first-run assertion below passes
here whether or not server/archive.py handles that, and scripts/check_archive.py is where it
is real. Same division as check_tools.py and check_auth.py.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from check_tools import PERIOD, Checks, report, reseed  # noqa: E402
from mcp import Client  # noqa: E402

from server import archive as archive_mod  # noqa: E402
from server import db, toollog  # noqa: E402
from server.app import mcp  # noqa: E402
from server.tools.get_run_ledger import CHANGED, FIRST_RUN, UNCHANGED  # noqa: E402

FIXTURE_SEEDS = (42, 43, *range(9001, 9016))

WORKBOOK = f"engagement-book-{PERIOD}.xlsx"
DECK = f"delivery-review-{PERIOD}.pptx"

# The marker on every row this harness inserts, so cleanup can be exact rather than
# by-id-range. Reseeding drops the table anyway; this is what makes --no-reseed survivable.
PROBE_NOTE = "check_ledger probe"

# Five session configurations the digest has to be blind to. Two ISO variants because those
# are what a real deployment might differ by, and three exotic ones because the assertion is
# about the query never asking the session anything, and a wider net makes that stronger.
SESSIONS = (
    ("UTC", "ISO, MDY"),
    ("America/Vancouver", "ISO, DMY"),
    ("Asia/Tokyo", "SQL, DMY"),
    ("Europe/Berlin", "German, DMY"),
    ("Pacific/Auckland", "Postgres, DMY"),
)


async def call(client: Client, tool: str, **arguments: Any) -> dict[str, Any]:
    result = await client.call_tool(tool, arguments)
    if getattr(result, "is_error", False):
        text = "; ".join(getattr(c, "text", "") for c in (result.content or []))
        raise AssertionError(f"{tool} returned an error: {text}")
    return result.structured_content


async def refused(client: Client, tool: str, **arguments: Any) -> tuple[bool, str]:
    result = await client.call_tool(tool, arguments)
    text = "; ".join(getattr(c, "text", "") for c in (result.content or []))
    return bool(getattr(result, "is_error", False)), text


async def publish(client: Client, store: Any, run_id: str) -> dict[str, Any]:
    """A full two-phase publish. The artifacts are filler and that is fine here.

    check_publish.py builds a real workbook and a real deck because it is asserting what
    reaches the archive. This asserts what the *ledger* says about a run, and the ledger
    does not read the artifacts — it records their digests, which check_publish.py already
    proves are the bytes on disk. Building two real files seventeen times to arrive at the
    same ledger would be twenty minutes spent re-proving someone else's assertion.
    """
    prepared = await call(client, "publish_pack", run_id=run_id, period=PERIOD, artifacts=[WORKBOOK, DECK])
    for upload in prepared["uploads"]:
        store.accept_put(upload["url"], f"filler for {upload['filename']}".encode())
    return await call(
        client, "publish_pack", run_id=run_id, period=PERIOD, artifacts=[WORKBOOK, DECK], finalize=True
    )


# ------------------------------------------------------------------ the only write path


def _late_entries(dsn: str, count: int, submitted_at: str) -> list[int]:
    """Insert rows that look like time filed after the fact. Local Postgres, always.

    **Pinned to psycopg and to the DSN it is handed, with no environment fallback.** This is
    the trap scripts/check_tools.py:132 documents: seed.py's --backend falls through to
    BIZDATA_DB_BACKEND, and in any shell where `terraform output shell_exports` has been
    evaluated that says ``aws``. Step 9 lost an afternoon to a harness that quietly rewrote
    the deployed database sixteen times while reading local Postgres. Three stray rows in
    Aurora would be worse than that one was, because they would not fail anything — they
    would just make every figure in a demo slightly wrong.

    ids are allocated as max(id)+1, because time_entries.id is a plain integer with no
    sequence. db/schema.sql:7 says why: a sequence-assigned id is a sequence-dependent
    checksum, and the seed has to be reproducible down to the byte.
    """
    import psycopg

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("select coalesce(max(id), 0) from time_entries")
        base = int(cur.fetchone()[0])
        # Copied from real rows so the person and engagement are real and the entry lands
        # inside the period. A synthetic row with an invented engagement_id would fail a
        # foreign key, and one with an out-of-period entry_date would test nothing.
        cur.execute(
            "select id from time_entries where entry_date between %s and %s order by id limit %s",
            (f"{PERIOD}-01", _period_end(), count),
        )
        sources = [int(r[0]) for r in cur.fetchall()]
        inserted: list[int] = []
        for offset, source in enumerate(sources, start=1):
            new_id = base + offset
            cur.execute(
                "insert into time_entries"
                " (id, person_id, engagement_id, entry_date, hours, billable, note, submitted_at)"
                " select %s, person_id, engagement_id, entry_date, 1.25, true, %s, %s::timestamptz"
                " from time_entries where id = %s",
                (new_id, PROBE_NOTE, submitted_at, source),
            )
            inserted.append(new_id)
        conn.commit()
    return inserted


def _remove_probes(dsn: str) -> int:
    import psycopg

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("delete from time_entries where note = %s", (PROBE_NOTE,))
        removed = cur.rowcount
        conn.commit()
    return removed


def _instant(value: Any) -> dt.datetime | None:
    """An ISO timestamp from either side of the wire, as the moment it names.

    The two sides spell it differently and both are correct. A tool response is serialised
    by Pydantic, which writes ``2026-09-12T16:04:21Z``; the pointer is written with
    ``datetime.isoformat()``, which writes ``...+00:00``. Comparing the strings fails on a
    pair of timestamps that are the same instant, which is a harness bug that reads exactly
    like a real one — so every comparison across that boundary goes through here.

    ``fromisoformat`` accepts both from Python 3.11, which is also why server/tools/
    get_run_ledger.py can read a pointer whichever way it was written.
    """
    return None if value is None else dt.datetime.fromisoformat(str(value))


def _period_end() -> str:
    year, month = (int(p) for p in PERIOD.split("-"))
    first_next = dt.date(year + (month == 12), (month % 12) + 1, 1)
    return (first_next - dt.timedelta(days=1)).isoformat()


# ------------------------------------------------------------------------- assertions


def check_digest_is_session_blind(dsn: str, checks: Checks) -> None:
    """The digest is the same string whatever the session thinks the date format is.

    This is the cross-backend assertion, run entirely on local Postgres, and that is not a
    contradiction: what it proves is that the *query* never consults a session setting. A
    query with that property cannot disagree between two backends over formatting, which is
    the only way local psycopg and the Data API could differ here — both are Postgres 16,
    and the digest is computed in the database, not in Python.

    Timestamps are compared as epochs because psycopg refuses to parse a timestamptz under
    a non-ISO DateStyle at all, which is a fact about the driver rather than the query.
    """
    import psycopg

    from server.db import load_sql, to_pyformat

    inner = to_pyformat(load_sql("period_watermark")).rstrip().rstrip(";")
    digests: set[str] = set()
    marks: set[float] = set()
    for timezone, datestyle in SESSIONS:
        with psycopg.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute(f"set time zone '{timezone}'")
            cur.execute(f"set datestyle to '{datestyle}'")
            cur.execute(
                f"select figures_digest, extract(epoch from watermark) as wm from ({inner}) q",
                {"period_end": _period_end()},
            )
            row = cur.fetchone()
            digests.add(str(row[0]))
            marks.add(float(row[1]) if row[1] is not None else -1.0)

    checks.add(
        f"the digest is identical across {len(SESSIONS)} timezone and DateStyle combinations",
        len(digests) == 1,
        f"{len(digests)} distinct: {sorted(d[:8] for d in digests)}",
    )
    checks.add(
        "and the watermark is the same instant under all of them",
        len(marks) == 1,
        f"{len(marks)} distinct",
    )


async def check_first_run(client: Client, checks: Checks) -> dict[str, Any]:
    """A period nobody has published. The missing key is a state, not a failure."""
    ledger = await call(client, "get_run_ledger", period=PERIOD, run_id="ledger-first")

    checks.add("a period with no prior run reads as a first run", ledger["decision"] == FIRST_RUN, ledger["decision"])
    checks.add("and reports no prior run rather than an empty one", ledger["prior_run"] is None, "null")
    checks.add(
        "and does not claim the figures changed, having nothing to change from",
        ledger["figures_changed"] is False and ledger["arrivals_complete"] is True,
        f"figures_changed={ledger['figures_changed']}",
    )
    checks.add(
        "it still fingerprints the data, so the first pack has a baseline",
        bool(ledger["figures_digest"]) and len(ledger["figures_digest"]) == 32,
        ledger["figures_digest"],
    )
    checks.add(
        "and reads a watermark from real rows",
        ledger["current_watermark"] is not None and ledger["entries_scanned"] > 0,
        f"{ledger['entries_scanned']} entries, watermark {ledger['current_watermark']}",
    )
    checks.add(
        "the next step tells the caller to assemble rather than to stop",
        "assemble" in ledger["next_step"].lower(),
        ledger["next_step"][:70],
    )
    return ledger


async def check_unchanged(client: Client, first: dict[str, Any], published: dict[str, Any], checks: Checks) -> None:
    """The same period, run twice, with nothing touched in between."""
    ledger = await call(client, "get_run_ledger", period=PERIOD, run_id="ledger-second")

    checks.add(
        "re-running an untouched period is a no-op, not a second pack",
        ledger["decision"] == UNCHANGED,
        ledger["decision"],
    )
    checks.add(
        "the digest is unchanged from before the pack was published",
        ledger["figures_digest"] == first["figures_digest"] == published["figures_digest"],
        ledger["figures_digest"],
    )
    checks.add(
        "the watermark has not moved either",
        ledger["current_watermark"] == first["current_watermark"],
        str(ledger["current_watermark"]),
    )
    checks.add(
        "nothing is reported as having arrived",
        ledger["late_arrivals"] == [] and ledger["total_count"] == 0,
        f"{ledger['total_count']} arrival(s)",
    )
    checks.add(
        "the prior run is named, with its archive prefix",
        ledger["prior_run"]["run_id"] == published["run_id"]
        and ledger["prior_run"]["prefix"] == published["prefix"],
        f"{ledger['prior_run']['run_id']} at {ledger['prior_run']['prefix']}",
    )
    checks.add(
        "and the caller is told not to build a second one",
        "do not build" in ledger["next_step"].lower(),
        ledger["next_step"][:70],
    )


async def check_three_late_entries(
    client: Client, dsn: str, prior: dict[str, Any], checks: Checks
) -> None:
    """Three entries filed after the run, named in the output. Step 12's headline condition."""
    watermark = dt.datetime.fromisoformat(prior["current_watermark"])
    filed_at = (watermark + dt.timedelta(hours=1)).isoformat()
    ids = _late_entries(dsn, 3, filed_at)
    checks.add("three late entries were inserted", len(ids) == 3, f"ids {ids}")

    ledger = await call(client, "get_run_ledger", period=PERIOD, run_id="ledger-late")

    checks.add("the period now reads as changed", ledger["decision"] == CHANGED, ledger["decision"])
    checks.add(
        "and names exactly those three entries, not two and not four",
        sorted(a["id"] for a in ledger["late_arrivals"]) == sorted(ids),
        f"{[a['id'] for a in ledger['late_arrivals']]} against {ids}",
    )
    checks.add(
        "the count agrees with the list",
        ledger["total_count"] == 3 and ledger["returned_count"] == 3,
        f"total {ledger['total_count']}, returned {ledger['returned_count']}",
    )
    checks.add(
        "each one carries who filed it and against what, not just an id",
        all(a["person_name"] and a["engagement_name"] for a in ledger["late_arrivals"]),
        ", ".join(sorted({a["person_name"] for a in ledger["late_arrivals"]}))[:60],
    )
    checks.add(
        "each is flagged as filed after the period closed",
        all(a["filed_after_period_close"] for a in ledger["late_arrivals"]),
        "all three",
    )
    checks.add(
        "the arrivals account for the whole change",
        ledger["arrivals_complete"] is True and ledger["entries_added"] == 3,
        f"added {ledger['entries_added']}, complete {ledger['arrivals_complete']}",
    )
    checks.add(
        "the watermark advanced past the previous run's",
        dt.datetime.fromisoformat(ledger["current_watermark"]) > watermark,
        f"{prior['current_watermark']} -> {ledger['current_watermark']}",
    )
    checks.add(
        "and the caller is told to ask before regenerating",
        "ask before regenerating" in ledger["next_step"].lower(),
        ledger["next_step"][-60:],
    )


async def check_change_the_watermark_cannot_see(
    client: Client, dsn: str, prior: dict[str, Any], checks: Checks
) -> None:
    """Entries filed *below* the previous watermark. The case the first build got wrong.

    submitted_at is not monotonic — nothing in db/schema.sql makes it so — and id is not a
    fallback, because ids are assigned by sorting on entry_date. So a row can be inserted
    with an earlier timestamp, move the digest, and be invisible to a ``since`` scan.

    The first version of get_run_ledger reported this as "no new entry has been filed, so an
    existing record was amended or removed", which was false: three had been. The assertion
    that matters is not that the tool finds them — it cannot — but that it does not claim
    they do not exist.
    """
    watermark = dt.datetime.fromisoformat(prior["current_watermark"])
    filed_at = (watermark - dt.timedelta(days=2)).isoformat()
    ids = _late_entries(dsn, 3, filed_at)

    ledger = await call(client, "get_run_ledger", period=PERIOD, run_id="ledger-invisible")

    checks.add(
        "a row filed below the watermark still moves the digest",
        ledger["decision"] == CHANGED,
        ledger["decision"],
    )
    checks.add(
        "the watermark cannot enumerate it, and the tool says so rather than denying it",
        ledger["arrivals_complete"] is False and ledger["late_arrivals"] == [],
        f"complete={ledger['arrivals_complete']}, listed={len(ledger['late_arrivals'])}",
    )
    checks.add(
        "the row count is what catches it",
        ledger["entries_added"] == len(ids),
        f"entries_added {ledger['entries_added']}",
    )
    checks.add(
        "and the summary says the rest were filed with an earlier timestamp",
        "earlier timestamp" in (ledger["change_summary"] or ""),
        (ledger["change_summary"] or "")[:80],
    )
    checks.add(
        "the watermark did not move, because nothing arrived after it",
        ledger["current_watermark"] == prior["current_watermark"],
        str(ledger["current_watermark"]),
    )


async def check_amendment_is_reversible(client: Client, dsn: str, checks: Checks) -> None:
    """Amend a value, see changed. Put it back, see unchanged.

    This is what separates a fingerprint from a change counter, and it is the assertion that
    makes "regenerates identically" checkable. A period that stayed changed forever once
    anything touched it would produce a stop condition nobody could clear, and the Skill
    would learn to ignore it.
    """
    import psycopg

    before = await call(client, "get_run_ledger", period=PERIOD, run_id="ledger-amend-before")
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "select id, hours from time_entries where entry_date between %s and %s order by id limit 1",
            (f"{PERIOD}-01", _period_end()),
        )
        row = cur.fetchone()
        target, original = int(row[0]), row[1]
        cur.execute("update time_entries set hours = hours + 0.25 where id = %s", (target,))
        conn.commit()

    amended = await call(client, "get_run_ledger", period=PERIOD, run_id="ledger-amend")
    checks.add(
        "amending one value in place is detected",
        amended["decision"] == CHANGED and amended["figures_digest"] != before["figures_digest"],
        f"entry {target}",
    )
    checks.add(
        "and is described as an amendment rather than as an arrival",
        amended["entries_added"] == 0 and "amended in place" in (amended["change_summary"] or ""),
        (amended["change_summary"] or "")[:70],
    )

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("update time_entries set hours = %s where id = %s", (original, target))
        conn.commit()

    restored = await call(client, "get_run_ledger", period=PERIOD, run_id="ledger-restore")
    checks.add(
        "putting the value back returns the period to unchanged",
        restored["decision"] == UNCHANGED and restored["figures_digest"] == before["figures_digest"],
        restored["figures_digest"],
    )


async def check_pointer_supersedes(client: Client, store: Any, checks: Checks) -> None:
    """A second run over the same period, and what the index remembers of the first."""
    first = json.loads(store.get_bytes(archive_mod.period_key(PERIOD)))
    second_run = "ledger-check-rerun"
    published = await publish(client, store, second_run)
    pointer = json.loads(store.get_bytes(archive_mod.period_key(PERIOD)))

    checks.add(
        "the period index now points at the newer run",
        pointer["run_id"] == second_run and published["run_id"] == second_run,
        f"{first['run_id']} -> {pointer['run_id']}",
    )
    checks.add(
        "and names the run it superseded",
        pointer["supersedes"] == first["run_id"],
        str(pointer["supersedes"]),
    )
    checks.add(
        "run_count advanced rather than resetting",
        pointer["run_count"] == first["run_count"] + 1,
        f"{first['run_count']} -> {pointer['run_count']}",
    )
    checks.add(
        "the superseded run's own ledger is untouched and still readable",
        store.get_bytes(first["ledger_key"]) is not None,
        first["ledger_key"],
    )
    checks.add(
        "the newer pointer carries the newer watermark",
        _instant(pointer["watermark"]) == _instant(published["watermark"]),
        f"{pointer['watermark']} against {published['watermark']}",
    )


async def check_refusals(client: Client, checks: Checks) -> None:
    """A period argument that is not one. The tool builds an archive key from it."""
    for label, period in (
        ("a month that does not exist", "2026-13"),
        ("a year on its own", "2026"),
        ("something that is not a date at all", "last month"),
        ("a path segment", "../2026-08"),
    ):
        was_refused, message = await refused(client, "get_run_ledger", period=period)
        checks.add(f"get_run_ledger refuses {label}", was_refused, message[:70] or "accepted it")


async def check_corrupt_pointer_is_not_swallowed(client: Client, store: Any, checks: Checks) -> None:
    """A pointer that is present but unreadable must not read as a first run.

    Treating it as absent is the dangerous repair: the period *has* been published, and a
    run that decides otherwise publishes a second pack over the first without asking. So the
    tool refuses, and the refusal names the period.
    """
    key = archive_mod.period_key(PERIOD)
    good = store.get_bytes(key)
    store.put_bytes(key, b"{ this is not json", "application/json")

    was_refused, message = await refused(client, "get_run_ledger", period=PERIOD)
    checks.add(
        "a corrupt period pointer is refused rather than read as a first run",
        was_refused and PERIOD in message,
        message[:80] or "accepted it",
    )

    store.put_bytes(key, good, "application/json")
    after = await call(client, "get_run_ledger", period=PERIOD, run_id="ledger-restored")
    checks.add(
        "and restoring it restores the reading",
        after["decision"] in (UNCHANGED, CHANGED),
        after["decision"],
    )


# --------------------------------------------------------------------------- one seed


async def run_seed(seed: int, checks: Checks, dsn: str) -> None:
    toollog.logger.handlers.clear()
    toollog.logger.addHandler(logging.NullHandler())
    toollog.logger.propagate = False

    store = archive_mod.archive()
    check_digest_is_session_blind(dsn, checks)

    async with Client(mcp) as client:
        await check_refusals(client, checks)

        first = await check_first_run(client, checks)
        published = await publish(client, store, f"ledger-check-seed-{seed}")
        checks.add(
            "publishing wrote the period index",
            published["period_pointer_key"] == archive_mod.period_key(PERIOD),
            str(published["period_pointer_key"]),
        )
        checks.add(
            "and the pack's watermark is the one the ledger read before it",
            _instant(published["watermark"]) == _instant(first["current_watermark"]),
            str(published["watermark"]),
        )

        await check_unchanged(client, first, published, checks)
        await check_pointer_supersedes(client, store, checks)
        await check_corrupt_pointer_is_not_swallowed(client, store, checks)

        current = await call(client, "get_run_ledger", period=PERIOD, run_id="ledger-baseline")
        await check_three_late_entries(client, dsn, current, checks)

        # Re-baseline before the invisible case, so entries_added is measured against a run
        # that has already seen the three above.
        await publish(client, store, f"ledger-check-seed-{seed}-r2")
        current = await call(client, "get_run_ledger", period=PERIOD, run_id="ledger-baseline-2")
        await check_change_the_watermark_cannot_see(client, dsn, current, checks)

        removed = _remove_probes(dsn)
        checks.add("the probe rows were cleaned up", removed == 6, f"{removed} removed")

        await publish(client, store, f"ledger-check-seed-{seed}-r3")
        await check_amendment_is_reversible(client, dsn, checks)


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, action="append", help="Repeatable. Every fixture seed when omitted.")
    ap.add_argument("--dsn", default=db.DEFAULT_DSN)
    ap.add_argument("--no-reseed", action="store_true", help="Use the database as it stands.")
    ap.add_argument("--verbose", "-v", action="store_true", help="Print passing assertions too.")
    ap.add_argument("--keep", type=Path, help="Write each seed's archive here and leave it.")
    args = ap.parse_args()

    seeds = args.seed or list(FIXTURE_SEEDS)
    if args.no_reseed and len(seeds) > 1:
        ap.error("--no-reseed needs a single --seed, since it cannot change the loaded data")

    # Local, always, and not negotiable by environment. This harness inserts rows.
    db._backend = db.get_backend("local", args.dsn)

    root = args.keep or Path(tempfile.mkdtemp(prefix="check-ledger-"))
    root.mkdir(parents=True, exist_ok=True)
    ok = True
    try:
        for seed in seeds:
            if not args.no_reseed:
                reseed(seed, args.dsn)
            else:
                # --no-reseed leaves whatever a previous run inserted, and a probe row from
                # last time would be counted as part of the baseline. Cheap, and it makes
                # the flag safe to use repeatedly rather than only once.
                _remove_probes(args.dsn)

            # A fresh archive per seed. LocalArchive defaults to build/archive and persists
            # between runs, so a pointer left by the previous seed would make this one's
            # first-run assertion fail against a period that had, locally, already been run.
            # The S3 backend has no equivalent problem: every seed is the same period, and
            # the deployment has one bucket that is meant to remember.
            archive_mod._archive = archive_mod.LocalArchive(root / f"seed-{seed}")

            checks = Checks(seed)
            await run_seed(seed, checks, args.dsn)
            ok = report(seed, checks, args.verbose) and ok
    finally:
        _remove_probes(args.dsn)
        if not args.keep:
            shutil.rmtree(root, ignore_errors=True)

    print("\nall seeds pass" if ok else "\nFAILURES above")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
