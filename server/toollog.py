"""One structured JSON line per tool call.

This exists at step 3 rather than at the end, and that is the whole point of it. Project #2
is an eval harness over this workflow, and it needs a corpus of real calls with real
arguments and real row counts. Bolting the logging on after the Skills exist means
re-running everything to manufacture history that should have been accumulating from the
first call. It also answers "how do you debug it when the client says the number is wrong"
with something that exists.

Seven fields, every line:

    run_id                 groups the calls that belong to one delivery-pack run
    tool                   the tool name
    arguments              what it was called with, defaults applied
    total_count            rows matching, before paging
    returned_count         rows in this response
    latency_ms             wall clock inside the handler, database included
    scoring_model_version  the weights in force when the answer was computed

Locally the line goes to stdout. Deployed at step 5 the same line lands in CloudWatch Logs
untouched, because it is already one JSON object per line, and step 9's ``publish_pack``
writes each run's full set to S3 under runs/<run_id>/.

Every line is also kept in a bounded in-process deque, which is what ``server/runlog.py``
reads on the local backend — there is no CloudWatch on a laptop, and the harness is one
process, so the buffer holds the whole run. It is not a handler, deliberately: all four
seeded harnesses clear the logger's handlers and attach their own, and a buffer that could
be detached that way would be empty exactly when it was needed.

On run_id: MCP has been stateless since the 2026-07-28 revision, so the server cannot infer
that six calls belong to one run — there is no session to hang it on, and Cowork's connector
does not let a Skill set a custom HTTP header. So run_id is an ordinary optional argument on
every tool, which assemble-delivery-pack sets once per run at step 6. Absent, the server
mints one per request, prefixed ``req-`` so an ungrouped call is visibly ungrouped rather
than quietly pretending to be a run of one.
"""

from __future__ import annotations

import functools
import inspect
import json
import logging
import time
import uuid
from collections import deque
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable, TypeVar

LOGGER_NAME = "bizdata.toolcall"

logger = logging.getLogger(LOGGER_NAME)

F = TypeVar("F", bound=Callable[..., Any])

# Bounded, because this lives in a long-running process and an unbounded log of every call
# a Lambda ever served is a memory leak with a slow fuse. A pack run is around thirty calls,
# so this holds the last sixty or so runs and forgets the rest.
RECENT_LIMIT = 2000

_recent: deque[dict[str, Any]] = deque(maxlen=RECENT_LIMIT)


def _plain(value: Any) -> Any:
    """Make an argument value JSON-safe without hiding what it was."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


def new_run_id() -> str:
    return f"req-{uuid.uuid4().hex[:12]}"


def logged(fn: F) -> F:
    """Wrap a tool handler so every call emits exactly one log line.

    ``functools.wraps`` is load-bearing rather than tidiness: MCPServer builds the tool's
    input schema with ``inspect.signature``, which follows ``__wrapped__``, so the schema
    the client sees is still derived from the real handler and its real annotations. Lose
    that and every tool publishes ``(*args, **kwargs)``. scripts/check_tools.py asserts the
    published schema for exactly this reason.
    """
    signature = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        arguments = {k: _plain(v) for k, v in bound.arguments.items()}

        run_id = arguments.get("run_id") or new_run_id()
        arguments["run_id"] = run_id
        # Reflect the minted id back into the call so the handler can echo it in the
        # response. A run id the caller cannot see is a run id it cannot quote back.
        if "run_id" in signature.parameters:
            bound.arguments["run_id"] = run_id

        started = time.perf_counter()
        try:
            result = fn(*bound.args, **bound.kwargs)
        except Exception as exc:
            _emit(
                run_id=run_id,
                tool=fn.__name__,
                arguments=arguments,
                total_count=None,
                returned_count=None,
                latency_ms=_elapsed(started),
                scoring_model_version=None,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise

        _emit(
            run_id=run_id,
            tool=fn.__name__,
            arguments=arguments,
            total_count=getattr(result, "total_count", None),
            returned_count=getattr(result, "returned_count", None),
            latency_ms=_elapsed(started),
            scoring_model_version=getattr(result, "scoring_model_version", None),
        )
        return result

    return wrapper  # type: ignore[return-value]


def _elapsed(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


def _emit(**line: Any) -> None:
    # separators without spaces so the line is compact in CloudWatch, sort_keys off so the
    # seven fields stay in the order a human reads them.
    text = json.dumps(line, default=str, separators=(",", ":"))
    # Round-tripped rather than buffered as-is, so the buffer holds what a reader of the log
    # would get and not what the emitter happened to have in hand. A datetime serialised by
    # ``default=str`` comes back as the string CloudWatch would have shown.
    _recent.append(json.loads(text))
    logger.info(text)


def recent(run_id: str | None = None) -> list[dict[str, Any]]:
    """Every line this process has emitted, oldest first, optionally one run's."""
    lines = list(_recent)
    if run_id is None:
        return lines
    return [line for line in lines if line.get("run_id") == run_id]


def forget() -> None:
    """Drop the buffer. For harnesses that want one seed's calls and not the last seed's."""
    _recent.clear()


def configure(stream: Any = None, level: int = logging.INFO) -> logging.Handler:
    """Send the tool-call log to ``stream`` as bare JSON lines, nothing prepended.

    A formatter of ``%(message)s`` and ``propagate = False`` together are what keep the
    output parseable: anything that decorates the line — a timestamp, a level, a logger
    name — makes every consumer strip a prefix before it can read the JSON, and project #2
    is the consumer.
    """
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return handler
