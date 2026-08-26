"""publish_pack — the fifth tool, and the only write in the system.

The four-tool cap holds where it matters, which is the *read* surface: four tools is the
number that keeps triage legible, and folding an action into one of their signatures to
preserve a number would be worse design than admitting there are five things. This one
retrieves nothing. It moves a finished pack into the archive of record.

**Two phases, one signature.** Called without ``finalize`` it mints one presigned PUT per
artifact and stops. Called with ``finalize`` it checks that those artifacts actually arrived,
then writes the two objects the server owns — the run's tool-call log and its ledger entry —
and reports what is in the archive. That is not ceremony. The ledger entry is the record
that says "this run produced these files", and a ledger written at minting time would be
recording an intention: four keys, two of which might never have been uploaded, in a file
whose whole job is to be trustworthy. Writing it only after ``head`` returns a size for every
artifact is what makes "runs/<run_id>/ contains all four artifacts" a checked condition
rather than a claim.

It is also where the confirmation step belongs. Phase one is inspectable — the caller sees
the exact keys before anything is written — and phase two is the caller saying yes.

**The agent never holds an AWS credential.** It holds a URL that can write one key, using
one method, for fifteen minutes. Two layers keep that true even if this module is wrong: the
key is built here from a validated run id and a validated bare filename and is never taken
from the caller as a path, and the Lambda's IAM statement is scoped to the ``runs/`` prefix,
so a key escaping this function would still be refused by S3.

On auth. There is one Cognito scope, ``bizdata/read``, and this write tool runs under it.
That is a deliberate limitation rather than an oversight — the SDK enforces scopes
server-wide, so a second scope means either breaking every read client or building per-tool
enforcement, and what it would protect is a URL that can write one key under one prefix for
fifteen minutes. It is written up in docs/notes/step-9-publish.md and belongs in SECURITY.md
at step 11 under "what you would add first".
"""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from server import archive as archive_mod
from server import build_info, db, runlog
from server.toollog import logged
from server.tools.common import Response, as_period_start, month_end

# A run id becomes a path segment, so it is validated as one. No slashes, no dots, nothing
# that a path join could read as "go up a level". The generator's own ids are ``req-`` or
# ``pack-`` plus hex, so this is wide enough for anything a Skill would set and narrow
# enough that the key cannot leave its prefix.
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,63}$")

# A bare filename. One dot, for the extension.
FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")

PUBLISHABLE_SUFFIXES = (".xlsx", ".pptx")

RUN_LOG_NAME = "run-log.json"
LEDGER_NAME = "ledger.json"

# The two names the server writes. An agent asking to upload one of these is asking to
# overwrite the record of its own run with a file it wrote, which is exactly the thing the
# split between the two halves of runs/<run_id>/ exists to prevent.
RESERVED_NAMES = frozenset({RUN_LOG_NAME, LEDGER_NAME})

MAX_ARTIFACTS = 8


class Upload(BaseModel):
    """One grant: write this file, to this key, with this method, until this moment."""

    model_config = ConfigDict(extra="forbid")

    filename: str
    key: str
    method: str = Field(default="PUT", description="The only method the URL is signed for.")
    url: str = Field(description="Presigned. Writes this one key and nothing else.")
    expires_at: dt.datetime


class Archived(BaseModel):
    """One object observed in the archive, read back rather than assumed."""

    model_config = ConfigDict(extra="forbid")

    filename: str
    key: str
    size_bytes: int
    etag: str
    written_by: str = Field(description="agent, through a presigned URL, or server.")


class PublishPackResult(Response):
    model_config = ConfigDict(extra="forbid")

    phase: str = Field(description="prepared when URLs were minted, archived when the run was finalised.")
    period: str
    destination: str = Field(description="The bucket, or the directory standing in for one locally.")
    prefix: str = Field(description="Everything this run writes sits under here.")

    expires_in_seconds: int | None = Field(
        default=None, description="Lifetime of the minted URLs. Null once finalised."
    )
    uploads: list[Upload] = Field(default_factory=list)
    archived: list[Archived] = Field(default_factory=list)

    run_log_key: str | None = Field(default=None, description="Written by the server at finalise.")
    ledger_key: str | None = Field(default=None, description="Written by the server at finalise.")
    period_pointer_key: str | None = Field(
        default=None, description="The period index this run is now the head of. Written by the server."
    )
    watermark: dt.datetime | None = Field(
        default=None, description="Everything filed up to this instant is in the archived pack."
    )
    figures_digest: str | None = Field(
        default=None, description="Fingerprint of the inputs. The next run compares against it."
    )
    tool_calls_archived: int | None = Field(
        default=None, description="Lines in run-log.json. Excludes the finalising call itself."
    )

    next_step: str


def _validate_run_id(run_id: str) -> str:
    if not RUN_ID_RE.match(run_id or ""):
        raise ValueError(
            f"run_id must be 3 to 64 characters of letters, digits, hyphen or underscore, "
            f"got {run_id!r}. It becomes a folder name in the archive."
        )
    return run_id


def _validate_filenames(artifacts: list[str]) -> list[str]:
    if not artifacts:
        raise ValueError("artifacts must name at least one file, such as engagement-book-2026-08.xlsx.")
    if len(artifacts) > MAX_ARTIFACTS:
        raise ValueError(f"artifacts must name at most {MAX_ARTIFACTS} files, got {len(artifacts)}.")

    seen: set[str] = set()
    for name in artifacts:
        if name in RESERVED_NAMES:
            raise ValueError(
                f"{name} is written by the server, not uploaded. It is this run's own record "
                f"and publish_pack writes it when you call again with finalize."
            )
        if not FILENAME_RE.match(name or "") or "/" in name or "\\" in name:
            raise ValueError(
                f"artifacts must be bare filenames, not paths, got {name!r}. "
                f"The key is built from run_id and the filename."
            )
        if Path(name).suffix.lower() not in PUBLISHABLE_SUFFIXES:
            raise ValueError(
                f"{name} is not a pack artifact. Publishable: {', '.join(PUBLISHABLE_SUFFIXES)}."
            )
        if name in seen:
            raise ValueError(f"{name} is named twice.")
        seen.add(name)
    return list(artifacts)


def _scoring_model_version() -> str | None:
    rows = db.query("scoring_model_version")
    return str(rows[0]["scoring_model_version"]) if rows else None


def _watermark(period_end: dt.date) -> dict:
    """Where the data stood at the moment this pack was vouched for.

    Read at finalise rather than at minting, and that ordering is the whole value of the
    number. A watermark taken when the URLs were issued would describe the data as it was
    before the agent spent ten minutes building a workbook, and anything filed during those
    ten minutes would be invisible to the next run — recorded as already seen, never
    reported as late. Taken here it is a claim the archive can stand behind: everything
    filed up to this instant is in the files sitting beside this record.

    The digest is a fingerprint of the same inputs, and it is what the next run actually
    decides on. See db/sql/period_watermark.sql for why both exist.
    """
    return db.query("period_watermark", {"period_end": period_end})[0]


def _ledger_entry(
    run_id: str,
    period: str,
    prefix: str,
    destination: str,
    artifacts: list[Archived],
    tool_calls: list[dict],
    scoring_model_version: str | None,
    watermark: dt.datetime | None,
    figures_digest: str | None,
    entries_scanned: int,
    invoices_scanned: int,
) -> dict:
    """This run's record, in the shape step 12 needed and stayed with.

    Step 9 wrote this as a row on the assumption step 12 would move it into a ``run_ledger``
    table. Step 12 did not, and the reason is in server/tools/get_run_ledger.py: the server
    holds one Postgres credential belonging to a role with SELECT and nothing else, and a
    table it writes costs a second role, a second secret and an asterisk on a claim that is
    currently absolute. So the row stayed a file, and gained the two fields it was missing.

    ``watermark`` and ``figures_digest`` are what make the next run's decision possible. The
    digest decides — equal digests mean an identical pack, so re-running is a no-op — and
    the watermark is what makes the *list* of what arrived since recoverable rather than
    just the fact that something did.

    ``artifacts`` holds three of the four objects in the folder, and ``ledger_key`` names the
    fourth. A ledger cannot carry its own digest — the digest is of the bytes that include
    it. That is the same shape as the run log not containing the call that wrote it, and both
    are named in the file rather than left as a discrepancy for a reader to find.
    """
    tools_used: dict[str, int] = {}
    for line in tool_calls:
        name = str(line.get("tool", "unknown"))
        tools_used[name] = tools_used.get(name, 0) + 1

    return {
        "run_id": run_id,
        "period": period,
        "status": "complete",
        "published_at": archive_mod.utcnow().isoformat(),
        "destination": destination,
        "prefix": prefix,
        "scoring_model_version": scoring_model_version,
        "server_version": build_info.version(),
        # Never rounded on the way in or out. utcnow() truncates to the second, and a
        # watermark a second early re-reports entries the previous run already counted.
        "watermark": watermark.isoformat() if watermark else None,
        "figures_digest": figures_digest,
        # Recorded because the watermark cannot enumerate everything the digest detects.
        # submitted_at is not monotonic — nothing in the schema makes it so — and an entry
        # filed below the previous high-water mark moves the digest while staying invisible
        # to a "since" scan. Comparing these counts is what lets the next run say which of
        # those happened instead of asserting nothing was filed. See get_run_ledger.
        "entries_scanned": entries_scanned,
        "invoices_scanned": invoices_scanned,
        "artifacts": [
            {"filename": a.filename, "key": a.key, "size_bytes": a.size_bytes, "etag": a.etag, "written_by": a.written_by}
            for a in artifacts
        ],
        "ledger_key": f"{prefix}/{LEDGER_NAME}",
        "ledger_note": (
            "This file is the fourth object in the folder. It is named here rather than "
            "listed above because a ledger cannot carry the digest of bytes that include it."
        ),
        "tool_calls": len(tool_calls),
        "tools_used": dict(sorted(tools_used.items())),
        # Named rather than left to be inferred from the count. See server/runlog.py: a run's
        # log cannot contain the call that wrote the log.
        "tool_calls_note": (
            "Excludes the publish_pack call that wrote this entry, which had not returned "
            "when the log was gathered."
        ),
    }


def _write_period_pointer(store: Any, period_label: str, ledger: dict) -> str:
    """Point the period at the run that just covered it. Step 12's index.

    One object per period rather than per run, because the question it answers is "has
    August been done", and answering that from the per-run ledgers would mean listing the
    bucket. This role has no ``ListBucket`` and is not getting one — see the IAM comment in
    infra/main/lambda.tf — so the index is a key the server can construct rather than a
    directory it can walk.

    **Written last, and read immediately before.** Last because it is the object that makes
    a run visible to the next one, and pointing at a folder whose ledger had not been
    written would be pointing at an incomplete run. Read first because ``supersedes`` and
    ``run_count`` are the only record that a period was published more than once: the object
    is overwritten, so without them the second run erases the evidence of the first. The
    bucket is versioned, so the prior pointers survive as prior versions — but a reader
    should not have to ask S3 for version history to learn that a period was republished.

    A pointer that is present and unreadable is left to fail rather than being overwritten
    quietly. It is the same judgement get_run_ledger makes on the read side: a corrupt
    ledger means something has written over the record of a run, and repairing it silently
    on the way past destroys the only evidence of that.
    """
    key = archive_mod.period_key(period_label)
    prior_raw = store.get_bytes(key)
    prior = json.loads(prior_raw) if prior_raw else {}

    pointer = {
        "period": period_label,
        "run_id": ledger["run_id"],
        "published_at": ledger["published_at"],
        "watermark": ledger["watermark"],
        "figures_digest": ledger["figures_digest"],
        "destination": ledger["destination"],
        "prefix": ledger["prefix"],
        "ledger_key": ledger["ledger_key"],
        "artifacts": [a["filename"] for a in ledger["artifacts"]],
        "entries_scanned": ledger["entries_scanned"],
        "invoices_scanned": ledger["invoices_scanned"],
        "scoring_model_version": ledger["scoring_model_version"],
        "server_version": ledger["server_version"],
        "supersedes": prior.get("run_id"),
        "run_count": int(prior.get("run_count", 0)) + 1,
        "pointer_note": (
            "One object per period, overwritten by each run that publishes it. The bucket "
            "is versioned, so every prior pointer is still retrievable as a prior version; "
            "supersedes and run_count carry the same fact without needing version history."
        ),
    }
    store.put_bytes(key, json.dumps(pointer, indent=2, default=str).encode(), archive_mod.content_type_for(key))
    return key


@logged
def publish_pack(
    run_id: str,
    period: str,
    artifacts: list[str],
    finalize: bool = False,
) -> PublishPackResult:
    """Archive a finished delivery pack to S3 under this run's id. The only write in the system.

    Call it twice. The first call mints one presigned PUT URL per artifact, each scoped to a
    single object key under runs/<run_id>/ and expiring in fifteen minutes; upload each file
    to its own URL with an HTTP PUT. The second call, with finalize set and the same
    arguments, checks that every artifact arrived, then writes this run's full tool-call log
    and its ledger entry alongside them and reports what is in the archive.

    Do not call it with finalize until the uploads have succeeded: it refuses to write a
    ledger entry for a pack that is not there, and names the missing file when it does.
    Publish the workbook and the deck only — the log and the ledger are the server's own
    record and are refused as uploads.

    Args:
        run_id: The run id used on every tool call in this run. It becomes the archive folder.
        period: YYYY-MM, the period the pack covers.
        artifacts: Bare filenames of the pack files to publish, .xlsx and .pptx.
        finalize: False to mint URLs, True to verify the uploads and close the run out.
    """
    _validate_run_id(run_id)
    names = _validate_filenames(artifacts)
    period_start = as_period_start(period)
    period_label = f"{period_start:%Y-%m}"

    store = archive_mod.archive()
    prefix = f"{archive_mod.DEFAULT_PREFIX}/{run_id}"
    keys = {name: f"{prefix}/{name}" for name in names}
    version = _scoring_model_version()

    if not finalize:
        grants = [store.presign_put(keys[name], archive_mod.DEFAULT_TTL_SECONDS) for name in names]
        return PublishPackResult(
            run_id=run_id,
            total_count=len(names),
            returned_count=len(names),
            scoring_model_version=version,
            phase="prepared",
            period=period_label,
            destination=store.destination,
            prefix=prefix,
            expires_in_seconds=archive_mod.DEFAULT_TTL_SECONDS,
            uploads=[
                Upload(filename=name, key=grant.key, url=grant.url, expires_at=grant.expires_at)
                for name, grant in zip(names, grants)
            ],
            next_step=(
                f"PUT each file to its url, then call publish_pack again with the same run_id, "
                f"period and artifacts, and finalize=true."
            ),
        )

    # ---- finalise: verify first, then write ------------------------------------------
    found: list[Archived] = []
    missing: list[str] = []
    for name in names:
        info = store.head(keys[name])
        if info is None or info.size_bytes == 0:
            missing.append(name)
            continue
        found.append(
            Archived(
                filename=name,
                key=info.key,
                size_bytes=info.size_bytes,
                etag=info.etag,
                written_by="agent",
            )
        )

    if missing:
        raise ValueError(
            f"Nothing to finalise: {', '.join(missing)} did not arrive at {prefix}/. "
            f"Upload each artifact to its presigned url first, then call again with finalize. "
            f"If a url has expired, call publish_pack without finalize to mint new ones."
        )

    tool_calls = runlog.for_run(run_id)
    run_log_bytes = json.dumps(tool_calls, indent=2, default=str).encode()
    run_log_key = f"{prefix}/{RUN_LOG_NAME}"
    log_info = store.put_bytes(run_log_key, run_log_bytes, archive_mod.content_type_for(RUN_LOG_NAME))
    found.append(
        Archived(
            filename=RUN_LOG_NAME,
            key=log_info.key,
            size_bytes=log_info.size_bytes,
            etag=log_info.etag,
            written_by="server",
        )
    )

    now = _watermark(month_end(period_start))
    ledger = _ledger_entry(
        run_id=run_id,
        period=period_label,
        prefix=prefix,
        destination=store.destination,
        artifacts=found,
        tool_calls=tool_calls,
        scoring_model_version=version,
        watermark=now["watermark"],
        figures_digest=str(now["figures_digest"]),
        entries_scanned=int(now["entries_scanned"]),
        invoices_scanned=int(now["invoices_scanned"]),
    )
    ledger_key = f"{prefix}/{LEDGER_NAME}"
    ledger_bytes = json.dumps(ledger, indent=2, default=str).encode()
    ledger_info = store.put_bytes(ledger_key, ledger_bytes, archive_mod.content_type_for(LEDGER_NAME))
    found.append(
        Archived(
            filename=LEDGER_NAME,
            key=ledger_info.key,
            size_bytes=ledger_info.size_bytes,
            etag=ledger_info.etag,
            written_by="server",
        )
    )

    # Last, and only now. The pointer is what makes this run visible to the next one, so it
    # cannot be written before the folder it points at is complete.
    pointer_key = _write_period_pointer(store, period_label, ledger)

    return PublishPackResult(
        run_id=run_id,
        total_count=len(found),
        returned_count=len(found),
        scoring_model_version=version,
        phase="archived",
        period=period_label,
        destination=store.destination,
        prefix=prefix,
        expires_in_seconds=None,
        archived=found,
        run_log_key=run_log_key,
        ledger_key=ledger_key,
        period_pointer_key=pointer_key,
        watermark=now["watermark"],
        figures_digest=str(now["figures_digest"]),
        tool_calls_archived=len(tool_calls),
        next_step=(
            f"Archived. Save the same files to the Drive folder with {run_id} in each filename, "
            f"then report both destinations."
        ),
    )
