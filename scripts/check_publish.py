#!/usr/bin/env python3
"""Step 9's Done-when conditions, as assertions.

    scripts/check_publish.py                  every fixture seed, reseeding each
    scripts/check_publish.py --seed 42 -v     one seed, against the database as it stands
    scripts/check_publish.py --keep /tmp/out  leave the packs, artifacts and archive behind

check_escalation.py asserts what the pack says. This asserts what happens to it afterwards:
that a run's artifacts reach runs/<run_id>/ and nothing else does, that the ledger entry is
only written once the artifacts are actually there, and that the tool-call log archived
beside them is this run's and only this run's.

It builds a real workbook and a real deck from the skill's own order of operations, uploads
them through the minted grants, and reads every figure back out of the archive rather than
out of the dict that produced it. The strongest assertion here is the cheapest one: the
archived object's digest equals the digest of the file on disk, so the bytes a partner would
download are the bytes the builder wrote.

What this harness cannot prove is that S3 enforces any of it. The local archive *models*
presigned-URL semantics — a grant that names one key, refuses another, and expires — and a
model is not evidence. scripts/check_archive.py runs the same three refusals against the
real bucket over HTTPS. The division is the same one check_tools.py and check_auth.py
already draw: logic in memory on seventeen seeds, mechanism once against the deployed thing.

Three assertions here are not Done-when conditions and are the ones most likely to catch a
regression:

    the run log holds this run's lines while the buffer behind it holds another run's
    the ledger's tool_calls agrees with the file it describes, not with a count kept
        separately
    republishing writes the same keys with the same digests

The first is the interesting one. run_id is an ordinary tool argument rather than a session,
so the only thing separating two concurrent runs in the archive is a filter on a field. A
decoy run under a different id is called on every seed so that filter is never asked an easy
question.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import json
import logging
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from check_format import DECK_BUILDER  # noqa: E402
from check_pack import BUILDER, SKILL, gather  # noqa: E402
from check_tools import PERIOD, Checks, anchors_for, report, reseed  # noqa: E402
from mcp import Client  # noqa: E402

from server import archive as archive_mod  # noqa: E402
from server import build_info, db, toollog  # noqa: E402
from server.app import mcp  # noqa: E402
from server.tools.publish_pack import LEDGER_NAME, RUN_LOG_NAME  # noqa: E402

FIXTURE_SEEDS = (42, 43, *range(9001, 9016))

WORKBOOK = f"engagement-book-{PERIOD}.xlsx"
DECK = f"delivery-review-{PERIOD}.pptx"

LOG_FIELDS = ("run_id", "tool", "arguments", "total_count", "returned_count", "latency_ms", "scoring_model_version")

LEDGER_FIELDS = (
    "run_id", "period", "status", "published_at", "destination", "prefix",
    "scoring_model_version", "server_version", "artifacts", "tool_calls", "tools_used",
)

# Every way a caller could try to write outside its own folder, or over the server's own
# record of the run. Each one is a refusal rather than a sanitised value: quietly repairing
# a path is how a key ends up somewhere nobody expected and nobody notices.
BAD_RUN_IDS = (
    ("a run id containing a slash", "runs/../elsewhere"),
    ("a run id containing dots", ".."),
    ("a run id with a space", "run 1"),
    ("a run id too short to be one", "ab"),
    ("a run id longer than a path segment should be", "r" * 65),
)

BAD_ARTIFACTS = (
    ("a path rather than a filename", ["sub/dir/book.xlsx"]),
    ("a traversal", ["../../etc/passwd.xlsx"]),
    ("the server's own run log", [RUN_LOG_NAME]),
    ("the server's own ledger", [LEDGER_NAME]),
    ("a file type that is not a pack artifact", ["notes.txt"]),
    ("nothing at all", []),
    ("the same file twice", [WORKBOOK, WORKBOOK]),
    ("more files than a pack has", [f"book-{i}.xlsx" for i in range(9)]),
)


def flowed(text: str) -> str:
    """Markdown prose with its line wrapping collapsed. See check_escalation.py."""
    import re

    return re.sub(r"\s+", " ", text)


def digest(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


async def call(client: Client, tool: str, **arguments: Any) -> dict[str, Any]:
    result = await client.call_tool(tool, arguments)
    if getattr(result, "is_error", False):
        text = "; ".join(getattr(c, "text", "") for c in (result.content or []))
        raise AssertionError(f"{tool} returned an error: {text}")
    return result.structured_content


async def refused(client: Client, **arguments: Any) -> tuple[bool, str]:
    """Call publish_pack expecting a refusal, returning whether it came and what it said.

    The message comes back whole. Two assertions below read it for the filenames it should
    and should not name, and a message truncated for the report column would fail them for
    the wrong reason.
    """
    result = await client.call_tool("publish_pack", arguments)
    text = "; ".join(getattr(c, "text", "") for c in (result.content or []))
    return bool(getattr(result, "is_error", False)), text.strip().replace("\n", " ")


# ------------------------------------------------------------------------ the skill text


def check_skill_document(checks: Checks) -> None:
    """The publish step is one section of a skill that already existed. Its wording is the
    only thing standing between a run and an unarchived pack."""
    raw = SKILL.read_text() if SKILL.exists() else ""
    checks.add("skill: SKILL.md exists", bool(raw), str(SKILL.relative_to(REPO_ROOT)))
    if not raw:
        return

    text = flowed(raw)
    for phrase, what in (
        ("publish_pack", "the tool by name"),
        ("finalize", "the second call"),
        ("presigned", "what the url is"),
        ("fifteen minutes", "how long the url lives"),
        ("run-log.json", "the log the server writes"),
        ("ledger.json", "the ledger the server writes"),
        ("Do not upload", "the reserved-name rule"),
        ("Save both files to the Drive folder", "the drive step"),
        ("run id in the filename", "the drive naming rule"),
        ("Drive is output only", "the read-path rule"),
        ("stop and say so", "the stop rule"),
    ):
        checks.add(f"skill: states {what}", phrase in text, "present" if phrase in text else "absent")


# ------------------------------------------------------------------ minting and refusals


async def check_minting(client: Client, run_id: str, checks: Checks) -> dict[str, Any]:
    before = archive_mod.utcnow()
    prepared = await call(
        client, "publish_pack", run_id=run_id, period=PERIOD, artifacts=[WORKBOOK, DECK]
    )

    checks.add("phase one reports itself as prepared", prepared["phase"] == "prepared", prepared["phase"])
    checks.add(
        "phase one writes nothing",
        not prepared["archived"] and prepared["ledger_key"] is None and prepared["run_log_key"] is None,
        "no archived objects, no ledger",
    )
    checks.add(
        "one grant per artifact, and no more",
        len(prepared["uploads"]) == 2 and prepared["total_count"] == 2,
        f"{len(prepared['uploads'])} grant(s)",
    )
    checks.add(
        "the url lives fifteen minutes",
        prepared["expires_in_seconds"] == archive_mod.DEFAULT_TTL_SECONDS == 900,
        f"{prepared['expires_in_seconds']}s",
    )

    prefix = f"{archive_mod.DEFAULT_PREFIX}/{run_id}"
    checks.add("the prefix is this run's own folder", prepared["prefix"] == prefix, prepared["prefix"])

    keys = [u["key"] for u in prepared["uploads"]]
    checks.add(
        "every key is the prefix and a bare filename",
        keys == [f"{prefix}/{WORKBOOK}", f"{prefix}/{DECK}"],
        ", ".join(keys),
    )
    checks.add(
        "no key can leave the prefix",
        all(k.startswith(f"{prefix}/") and ".." not in k and k.count("/") == 2 for k in keys),
        "confined",
    )
    checks.add(
        "each artifact gets its own url",
        len({u["url"] for u in prepared["uploads"]}) == 2,
        "distinct",
    )
    checks.add(
        "the grant is signed for PUT and nothing else",
        all(u["method"] == "PUT" for u in prepared["uploads"]),
        "PUT",
    )

    expiries = [dt.datetime.fromisoformat(str(u["expires_at"])) for u in prepared["uploads"]]
    within = all(
        0 < (e - before).total_seconds() <= archive_mod.DEFAULT_TTL_SECONDS + 5 for e in expiries
    )
    checks.add(
        "expires_at is the mint time plus that lifetime",
        within,
        f"{(expiries[0] - before).total_seconds():.0f}s ahead",
    )
    return prepared


async def check_refusals(client: Client, run_id: str, checks: Checks) -> None:
    for what, bad in BAD_RUN_IDS:
        ok, message = await refused(client, run_id=bad, period=PERIOD, artifacts=[WORKBOOK])
        checks.add(f"refuses {what}", ok, message[:96] or "accepted")

    for what, bad in BAD_ARTIFACTS:
        ok, message = await refused(client, run_id=run_id, period=PERIOD, artifacts=bad)
        checks.add(f"refuses {what}", ok, message[:96] or "accepted")

    ok, message = await refused(client, run_id=run_id, period="not-a-period", artifacts=[WORKBOOK])
    checks.add("refuses a period that is not a period", ok, message[:96] or "accepted")

    # An empty run id is not refused, and the reason is worth an assertion rather than a
    # missing one. server/toollog.py mints an id whenever the argument is falsy and writes it
    # back into the call, so the handler never sees the empty string and the key gets a
    # generated segment rather than an empty one. Consistent — the log line and the archive
    # folder agree — but the pack is then archived under an id no analysis call used, which
    # is why the skill sets run_id once and uses it throughout.
    minted = await call(client, "publish_pack", run_id="", period=PERIOD, artifacts=[WORKBOOK])
    checks.add(
        "an empty run id becomes a minted one rather than an empty path segment",
        minted["prefix"].startswith(f"{archive_mod.DEFAULT_PREFIX}/req-")
        and minted["run_id"] == minted["prefix"].split("/")[-1],
        minted["prefix"],
    )


def check_grant_semantics(store: Any, run_id: str, checks: Checks) -> None:
    """A grant writes its one key, refuses any other, and stops working.

    Modelled locally, proved against S3 in scripts/check_archive.py. Asserted here anyway,
    because the model is what the other assertions in this file rely on and a model that
    quietly stopped enforcing anything would make all of them vacuous.
    """
    grant = store.presign_put(f"{archive_mod.DEFAULT_PREFIX}/{run_id}/probe.xlsx", 900)

    try:
        info = store.accept_put(grant.url, b"probe")
        wrote = info.key == grant.key
    except archive_mod.ArchiveError as exc:
        wrote, info = False, exc
    checks.add("a grant writes its one key", wrote, str(getattr(info, "key", info)))

    elsewhere = grant.url.replace("probe.xlsx", "somewhere-else.xlsx")
    try:
        store.accept_put(elsewhere, b"probe")
        checks.add("the same grant is refused on any other key", False, "it wrote")
    except archive_mod.ArchiveError as exc:
        checks.add("the same grant is refused on any other key", True, str(exc)[:96])

    stale = store.presign_put(f"{archive_mod.DEFAULT_PREFIX}/{run_id}/stale.xlsx", -1)
    try:
        store.accept_put(stale.url, b"probe")
        checks.add("an expired grant is refused", False, "it wrote")
    except archive_mod.ArchiveError as exc:
        checks.add("an expired grant is refused", True, str(exc)[:96])

    unknown = grant.url.split("?")[0] + "?token=never-minted&expires=99999999999"
    try:
        store.accept_put(unknown, b"probe")
        checks.add("a url this archive never minted is refused", False, "it wrote")
    except archive_mod.ArchiveError as exc:
        checks.add("a url this archive never minted is refused", True, str(exc)[:96])


# ------------------------------------------------------------------------- finalisation


async def check_partial_finalise(
    client: Client, store: Any, run_id: str, prepared: dict[str, Any], files: dict[str, Path], checks: Checks
) -> None:
    """Nothing is vouched for until everything has arrived."""
    ok, message = await refused(
        client, run_id=run_id, period=PERIOD, artifacts=[WORKBOOK, DECK], finalize=True
    )
    checks.add("refuses to finalise a run with nothing uploaded", ok, message[:96] or "accepted")
    checks.add(
        "and names both missing files rather than failing generically",
        WORKBOOK in message and DECK in message,
        message[:96],
    )
    checks.add(
        "and the ledger was not written",
        store.head(f"{archive_mod.DEFAULT_PREFIX}/{run_id}/{LEDGER_NAME}") is None,
        "absent",
    )

    grants = {u["filename"]: u["url"] for u in prepared["uploads"]}
    store.accept_put(grants[WORKBOOK], files[WORKBOOK].read_bytes())

    ok, message = await refused(
        client, run_id=run_id, period=PERIOD, artifacts=[WORKBOOK, DECK], finalize=True
    )
    checks.add("refuses to finalise a run that is half uploaded", ok, message[:96] or "accepted")
    checks.add(
        "and names the one that is missing, not the one that arrived",
        DECK in message and WORKBOOK not in message,
        message[:96],
    )

    store.accept_put(grants[DECK], files[DECK].read_bytes())


def check_archived_objects(
    archived: list[dict[str, Any]], store: Any, run_id: str, files: dict[str, Path], checks: Checks
) -> None:
    prefix = f"{archive_mod.DEFAULT_PREFIX}/{run_id}"
    by_name = {a["filename"]: a for a in archived}
    expected = [WORKBOOK, DECK, RUN_LOG_NAME, LEDGER_NAME]

    checks.add(
        "runs/<run_id>/ holds all four artifacts",
        sorted(by_name) == sorted(expected),
        ", ".join(sorted(by_name)),
    )
    checks.add(
        "and every one of them is really there, read back from the archive",
        all(store.head(f"{prefix}/{name}") is not None for name in expected),
        "four of four",
    )
    checks.add(
        "the two the agent wrote and the two the server wrote are labelled as such",
        {n: by_name[n]["written_by"] for n in expected if n in by_name}
        == {WORKBOOK: "agent", DECK: "agent", RUN_LOG_NAME: "server", LEDGER_NAME: "server"},
        ", ".join(f"{n}={by_name[n]['written_by']}" for n in sorted(by_name)),
    )

    for name in (WORKBOOK, DECK):
        if name not in by_name:
            continue
        raw = files[name].read_bytes()
        checks.add(
            f"{name}: the archived bytes are the bytes on disk",
            by_name[name]["etag"] == digest(raw) and by_name[name]["size_bytes"] == len(raw),
            f"{by_name[name]['size_bytes']} bytes, {by_name[name]['etag'][:12]}",
        )


def check_run_log(
    store: Any, run_id: str, decoy_run_id: str, archived_count: int | None, checks: Checks
) -> list[dict[str, Any]]:
    prefix = f"{archive_mod.DEFAULT_PREFIX}/{run_id}"
    raw = (store.root / f"{prefix}/{RUN_LOG_NAME}").read_bytes()

    try:
        lines = json.loads(raw)
    except json.JSONDecodeError as exc:
        checks.add("run-log.json parses", False, str(exc)[:96])
        return []

    checks.add("run-log.json parses as a list of log lines", isinstance(lines, list) and bool(lines), f"{len(lines)} line(s)")
    if not isinstance(lines, list) or not lines:
        return []

    checks.add(
        "every archived line belongs to this run",
        all(line.get("run_id") == run_id for line in lines),
        f"{len({line.get('run_id') for line in lines})} distinct run id(s)",
    )
    checks.add(
        "and the decoy run's lines were in the buffer and stayed out of the file",
        bool(toollog.recent(decoy_run_id)) and not any(line.get("run_id") == decoy_run_id for line in lines),
        f"{len(toollog.recent(decoy_run_id))} decoy line(s) filtered out",
    )
    checks.add(
        "every archived line carries all seven fields",
        all(all(f in line for f in LOG_FIELDS) for line in lines),
        ", ".join(LOG_FIELDS),
    )
    checks.add(
        "the pack's own tool calls are in it",
        {"list_engagements", "get_time_summary", "get_engagement_burn", "get_financials"}
        <= {line.get("tool") for line in lines},
        f"{len({line.get('tool') for line in lines})} distinct tool(s)",
    )
    # See server/runlog.py: the log is gathered inside the call that writes it, so the
    # successful finalise cannot be in its own file. Asserted rather than left to be noticed,
    # because a future change that made it appear would mean the log was written twice.
    finalised = [
        line for line in lines
        if line.get("tool") == "publish_pack"
        and line.get("arguments", {}).get("finalize")
        and line.get("total_count") is not None
    ]
    checks.add(
        "the call that wrote the log is not in the log",
        not finalised,
        "absent, as it must be" if not finalised else f"{len(finalised)} present",
    )
    checks.add(
        "the count the tool reported is the count in the file",
        archived_count == len(lines),
        f"reported {archived_count}, file holds {len(lines)}",
    )
    return lines


def check_ledger(
    store: Any, run_id: str, lines: list[dict[str, Any]], checks: Checks
) -> dict[str, Any]:
    prefix = f"{archive_mod.DEFAULT_PREFIX}/{run_id}"
    ledger = json.loads((store.root / f"{prefix}/{LEDGER_NAME}").read_bytes())

    checks.add(
        "ledger.json carries every field step 12's table will need",
        all(f in ledger for f in LEDGER_FIELDS),
        ", ".join(f for f in LEDGER_FIELDS if f not in ledger) or "all present",
    )
    checks.add("the ledger records the run", ledger.get("run_id") == run_id, str(ledger.get("run_id")))
    checks.add("the ledger records the period", ledger.get("period") == PERIOD, str(ledger.get("period")))
    checks.add(
        "the ledger is only written complete",
        ledger.get("status") == "complete",
        str(ledger.get("status")),
    )
    checks.add(
        "the ledger lists the three objects it can digest",
        sorted(a["filename"] for a in ledger.get("artifacts", []))
        == sorted([WORKBOOK, DECK, RUN_LOG_NAME]),
        f"{len(ledger.get('artifacts', []))} artifact(s)",
    )
    # The fourth is the ledger itself, which cannot carry the digest of bytes that include
    # it. Named rather than omitted, so the file accounts for every key in the folder.
    accounted = {a["key"] for a in ledger.get("artifacts", [])} | {ledger.get("ledger_key")}
    checks.add(
        "and names itself as the fourth, so all four keys are accounted for",
        accounted == {f"{prefix}/{n}" for n in (WORKBOOK, DECK, RUN_LOG_NAME, LEDGER_NAME)},
        f"{len(accounted)} key(s)",
    )
    checks.add(
        "every key in the ledger is under this run's prefix",
        all(str(k).startswith(f"{prefix}/") for k in accounted),
        "confined",
    )

    live = db.query("scoring_model_version")
    version = live[0]["scoring_model_version"] if live else None
    checks.add(
        "the ledger records the scoring model in force, from SQL",
        ledger.get("scoring_model_version") == version,
        f"{ledger.get('scoring_model_version')} against {version}",
    )
    checks.add(
        "the ledger records the server build that wrote it",
        ledger.get("server_version") == build_info.version(),
        str(ledger.get("server_version")),
    )
    checks.add(
        "the ledger's tool_calls agrees with run-log.json",
        ledger.get("tool_calls") == len(lines),
        f"{ledger.get('tool_calls')} against {len(lines)}",
    )
    checks.add(
        "the ledger says which tools ran and how often",
        sum((ledger.get("tools_used") or {}).values()) == len(lines),
        json.dumps(ledger.get("tools_used", {})),
    )
    checks.add(
        "the ledger says the finalising call is not counted",
        "tool_calls_note" in ledger and "publish_pack" in str(ledger.get("tool_calls_note")),
        "noted" if "tool_calls_note" in ledger else "absent",
    )
    return ledger


async def check_republish(
    client: Client, store: Any, run_id: str, files: dict[str, Path], first: dict[str, Any], checks: Checks
) -> None:
    """Publishing the same pack again writes the same keys with the same digests.

    Not "produces an identical ledger": published_at moves and tool_calls grows, because
    more calls have happened and saying otherwise would be a lie in the archive of record.
    Bucket versioning is what keeps the earlier copy, which is the case the Glacier rule in
    infra/main/storage.tf already anticipates.
    """
    again = await call(client, "publish_pack", run_id=run_id, period=PERIOD, artifacts=[WORKBOOK, DECK])
    grants = {u["filename"]: u["url"] for u in again["uploads"]}
    for name in (WORKBOOK, DECK):
        store.accept_put(grants[name], files[name].read_bytes())

    second = await call(
        client, "publish_pack", run_id=run_id, period=PERIOD, artifacts=[WORKBOOK, DECK], finalize=True
    )

    keys_before = sorted(a["key"] for a in first["archived"])
    keys_after = sorted(a["key"] for a in second["archived"])
    checks.add("republishing writes the same four keys", keys_before == keys_after, ", ".join(keys_after))

    digests_before = {a["filename"]: a["etag"] for a in first["archived"] if a["written_by"] == "agent"}
    digests_after = {a["filename"]: a["etag"] for a in second["archived"] if a["written_by"] == "agent"}
    checks.add(
        "and the artifacts are byte-identical across the two publishes",
        digests_before == digests_after,
        ", ".join(f"{k}={v[:8]}" for k, v in sorted(digests_after.items())),
    )
    checks.add(
        "and the second ledger still says complete",
        second["phase"] == "archived" and second["ledger_key"] == first["ledger_key"],
        second["phase"],
    )


# --------------------------------------------------------------------------- one seed


async def run_seed(seed: int, anchors: dict[str, Any], checks: Checks, workdir: Path) -> None:
    toollog.logger.handlers.clear()
    toollog.logger.addHandler(logging.NullHandler())
    toollog.logger.propagate = False

    period_end = anchors.get("period_end") or "2026-08-31"
    check_skill_document(checks)

    run_id = f"publish-check-seed-{seed}"
    decoy_run_id = f"publish-decoy-seed-{seed}"
    store = archive_mod.archive()

    async with Client(mcp) as client:
        # A real pack, through the skill's own order of operations. There is no point
        # archiving two kilobytes of filler and calling the round trip proved.
        pack = await gather(client, run_id, period_end)

        # The decoy exists so that "only this run's lines" is asked of a buffer that holds
        # someone else's. Same tool, different run id, discarded.
        await call(client, "list_engagements", run_id=decoy_run_id, as_of_date=period_end, limit=5)

        pack_path = workdir / f"pack-{seed}.json"
        pack_path.write_text(json.dumps(pack, default=str, indent=2))

        files: dict[str, Path] = {
            WORKBOOK: workdir / f"engagement-book-{PERIOD}-seed-{seed}.xlsx",
            DECK: workdir / f"delivery-review-{PERIOD}-seed-{seed}.pptx",
        }
        for builder, out in ((BUILDER, files[WORKBOOK]), (DECK_BUILDER, files[DECK])):
            built = subprocess.run(
                [sys.executable, str(builder), str(pack_path), "--out", str(out)],
                capture_output=True, text=True,
            )
            checks.add(
                f"{builder.name} produced something to publish",
                built.returncode == 0 and out.exists(),
                built.stdout.strip()[:96] if built.returncode == 0 else built.stderr.strip()[:96],
            )
        if not all(p.exists() for p in files.values()):
            return

        await check_refusals(client, run_id, checks)
        check_grant_semantics(store, run_id, checks)

        prepared = await check_minting(client, run_id, checks)
        await check_partial_finalise(client, store, run_id, prepared, files, checks)

        archived = await call(
            client, "publish_pack", run_id=run_id, period=PERIOD, artifacts=[WORKBOOK, DECK], finalize=True
        )
        checks.add("phase two reports itself as archived", archived["phase"] == "archived", archived["phase"])
        checks.add(
            "and counts every object it wrote, not only the uploads",
            archived["total_count"] == archived["returned_count"] == 4,
            f"{archived['total_count']}",
        )

        check_archived_objects(archived["archived"], store, run_id, files, checks)
        lines = check_run_log(store, run_id, decoy_run_id, archived["tool_calls_archived"], checks)
        check_ledger(store, run_id, lines, checks)

        await check_republish(client, store, run_id, files, archived, checks)


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, action="append", help="Repeatable. Every fixture seed when omitted.")
    ap.add_argument("--dsn", default=db.DEFAULT_DSN)
    ap.add_argument("--no-reseed", action="store_true", help="Use the database as it stands.")
    ap.add_argument("--verbose", "-v", action="store_true", help="Print passing assertions too.")
    ap.add_argument("--keep", type=Path, help="Write the packs, artifacts and archive here and leave them.")
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

    workdir = args.keep or Path(tempfile.mkdtemp(prefix="bizdata-publish-"))
    workdir.mkdir(parents=True, exist_ok=True)

    # The archive is local whichever database backend is in play. The two are orthogonal —
    # --backend aws is about where the figures come from — and pointing seventeen seeds of
    # this harness at the real bucket would put a few hundred objects in the archive of
    # record for no gain. scripts/check_archive.py is the one that touches S3.
    archive_mod._archive = archive_mod.LocalArchive(workdir / "archive")

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
            print(f"\npacks, artifacts and archive left in {workdir}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)

    print()
    print("all seeds pass" if ok else "FAILURES above")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
