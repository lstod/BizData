"""One run's tool-call log, gathered back up so it can be archived beside its artifacts.

``server/toollog.py`` writes the log. This reads it, which turns out to be the harder half:
the line is written wherever the process's stdout goes, and by the time anything wants to
archive a run, "wherever stdout went" is CloudWatch Logs on the deployed side and an
in-process buffer on the local one. Same seam as ``server/db.py`` and ``server/archive.py``,
chosen by ``BIZDATA_RUNLOG_BACKEND``.

Why the *server* gathers this rather than the agent. The tool-call log is the server's
record of what it was asked and what it answered — arguments, row counts, latency, the
scoring model in force. An agent's account of its own calls is a different document with a
different trust level, and the whole reason the log exists is to be the one that can settle
an argument about a figure. So step 9 gives the Lambda ``logs:FilterLogEvents`` on its own
log group and writes ``run-log.json`` itself.

Two properties of the deployed path, both real and both written down rather than smoothed
over:

**The finalizing call is not in its own log.** ``publish_pack`` emits its log line when the
handler returns, and the handler is what writes the file, so ``run-log.json`` necessarily
records every call in the run except the one that wrote it. This is not a defect that can
be fixed by ordering; it is the shape of the thing. The ledger entry records the count, so
the absence is visible rather than silent.

**CloudWatch ingestion is not instantaneous.** A line emitted a second ago may not be
filterable yet. ``CloudWatchSource`` therefore merges the in-process buffer over the
CloudWatch result and de-duplicates, which recovers anything the query was too early to
see — including, on a warm Lambda, most of the run. Merged rather than preferred, because
across a cold start the buffer holds nothing and CloudWatch holds everything.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Protocol

from server import toollog

# How far back to look. A pack run is minutes; a day of slack costs nothing on a log group
# this size and covers a run that was interrupted and resumed.
DEFAULT_LOOKBACK_SECONDS = 24 * 60 * 60


class RunLogError(RuntimeError):
    pass


class RunLogSource(Protocol):
    name: str

    def for_run(self, run_id: str) -> list[dict[str, Any]]: ...


def _sort_key(line: dict[str, Any]) -> Any:
    """Chronological where the source gave us a timestamp, insertion order otherwise."""
    return (line.get("_ingested_at") or 0, line.get("tool") or "")


def _dedupe(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop exact repeats, keeping the first.

    Two genuinely distinct calls to the same tool with the same arguments would differ in
    ``latency_ms``, which is a float measured to a tenth of a millisecond, so an exact
    collision across the whole line is a duplicate rather than a coincidence often enough
    for this to be the right trade. The alternative — a per-call sequence number — is a
    change to the log format that project #2 already consumes.
    """
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for line in lines:
        body = {k: v for k, v in line.items() if not k.startswith("_")}
        fingerprint = json.dumps(body, sort_keys=True, default=str)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        out.append(body)
    return out


class LocalSource:
    """The in-process buffer in server/toollog.py.

    Complete by construction when the whole run happened in this process, which is exactly
    the harness's situation and never the Lambda's.
    """

    name = "local"

    def for_run(self, run_id: str) -> list[dict[str, Any]]:
        return _dedupe(toollog.recent(run_id))


class CloudWatchSource:
    """``logs:FilterLogEvents`` against this function's own log group.

    The filter pattern is the run id as a quoted substring, which CloudWatch applies to the
    raw message before anything parses it. That is cheap and slightly loose — a run id
    appearing inside some other line's arguments would match — so every event is parsed as
    JSON and re-checked on the ``run_id`` field before it is kept.
    """

    name = "cloudwatch"

    def __init__(self, log_group: str | None = None) -> None:
        import boto3

        resolved = log_group or os.environ.get("BIZDATA_LOG_GROUP")
        if not resolved:
            raise RunLogError(
                "The cloudwatch runlog backend needs BIZDATA_LOG_GROUP in the environment. "
                "It comes out of `terraform -chdir=infra/main output -raw log_group`."
            )
        self.log_group = str(resolved)
        self.client: Any = boto3.client("logs")

    def for_run(self, run_id: str, lookback_seconds: int = DEFAULT_LOOKBACK_SECONDS) -> list[dict[str, Any]]:
        start_ms = int((time.time() - lookback_seconds) * 1000)
        lines: list[dict[str, Any]] = []
        token: str | None = None

        while True:
            kwargs: dict[str, Any] = {
                "logGroupName": self.log_group,
                "startTime": start_ms,
                "filterPattern": f'"{run_id}"',
                "limit": 1000,
            }
            if token:
                kwargs["nextToken"] = token
            try:
                page = self.client.filter_log_events(**kwargs)
            except Exception as exc:
                raise RunLogError(f"filter_log_events on {self.log_group} failed: {exc}") from exc

            for event in page.get("events", []):
                try:
                    body = json.loads(event.get("message", "").strip())
                except json.JSONDecodeError:
                    continue
                # Only tool-call lines. server/reqlog.py writes to the same stream and its
                # lines carry an "event" key and no "tool".
                if not isinstance(body, dict) or body.get("run_id") != run_id or "tool" not in body:
                    continue
                body["_ingested_at"] = event.get("timestamp", 0)
                lines.append(body)

            token = page.get("nextToken")
            if not token:
                break

        lines.sort(key=_sort_key)
        # The buffer last, so CloudWatch's copy of a line wins the de-duplication and keeps
        # its ingestion order. What the buffer adds is the tail CloudWatch has not caught up
        # with yet.
        return _dedupe(lines + toollog.recent(run_id))


BACKENDS = {"local": LocalSource, "cloudwatch": CloudWatchSource}


def get_source(backend: str | None = None) -> RunLogSource:
    name = (backend or os.environ.get("BIZDATA_RUNLOG_BACKEND") or "local").lower()
    try:
        cls = BACKENDS[name]
    except KeyError:
        raise SystemExit(
            f"Unknown BIZDATA_RUNLOG_BACKEND {name!r}. Expected one of: {', '.join(sorted(BACKENDS))}."
        ) from None
    return cls()  # type: ignore[return-value]


_source: RunLogSource | None = None


def source() -> RunLogSource:
    global _source
    if _source is None:
        _source = get_source()
    return _source


def for_run(run_id: str) -> list[dict[str, Any]]:
    return source().for_run(run_id)
