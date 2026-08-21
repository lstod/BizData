"""The RDS Data API, as one small client both halves of the repository share.

``server/db.py`` reads through it and ``scripts/writers.py`` writes through it, so the
parameter encoding, the record decoding and the resume retry live here once rather than
twice. Nothing in this module knows about queries, tables or tools.

Three things it exists to get right:

**Encoding.** Every parameter goes down as ``stringValue`` or ``isNull``, and never as
``longValue`` or ``doubleValue``. That looks lazy and is the opposite: every placeholder
in ``db/sql/`` already carries an explicit cast — ``:as_of_date::date``,
``:engagement_ids::int[]``, ``:status::text`` — so Postgres is being told the type by the
query rather than inferring it from the wire format. One encoding path, no type table on
the way in, and a parameter that arrives as the wrong type is a query bug rather than a
binding bug. The seed writer casts the same way, from a map it reads out of
``information_schema`` rather than a second copy of the schema.

**Decoding.** This is where the two backends can silently disagree. The Data API returns
``numeric`` as a string and ``date`` as a string, where psycopg returns ``Decimal`` and
``datetime.date``. Left alone that is a tool whose response validates in one backend and
not the other, or worse, validates in both and rounds differently. So every value is
coerced back through ``columnMetadata[i]["typeName"]`` into the type psycopg would have
produced. ``scripts/check_tools.py`` run against both backends is what proves it.

**Resuming.** A cluster at zero ACU is not slow, it is absent: the Data API answers with
an error while it wakes rather than blocking, and the first call after idle is expected to
fail. Every call retries with backoff, so ``scripts/warm.py`` is a convenience for the
recording rather than the only thing standing between a demo and a stack trace.
"""

from __future__ import annotations

import datetime as dt
import os
import time
from dataclasses import dataclass
from decimal import Decimal
from functools import cached_property
from typing import Any, Iterable, Sequence

# Roughly fifteen seconds is the documented resume time for a paused Aurora Serverless v2
# cluster. The ceiling here is deliberately well past that: the cost of waiting too long
# is a slow first call, and the cost of not waiting long enough is a failed demo.
RESUME_TIMEOUT_S = 90.0
RESUME_BACKOFF_S = (1.0, 2.0, 3.0, 5.0, 8.0, 13.0, 13.0, 13.0, 13.0, 13.0)

# Matched against the exception's text rather than its class, because the Data API reports
# a resuming cluster as a BadRequestException with a message and not as its own type.
RESUMING_MARKERS = (
    "resuming",
    "is being resumed",
    "communications link failure",
    "database is currently unavailable",
    "dbinstance is not available",
)

# Anything the Data API itself is briefly unhappy about, as opposed to a bad statement.
RETRYABLE_CODES = (
    "InternalServerErrorException",
    "ServiceUnavailableError",
    "ThrottlingException",
    "TooManyRequestsException",
)


class DataApiError(RuntimeError):
    """A Data API call failed in a way retrying will not fix."""


def encode(value: Any) -> dict[str, Any]:
    """One Python value as a Data API parameter field.

    Everything is a string except null, because the SQL carries the casts. Lists become
    a Postgres array literal so ``:engagement_ids::int[]`` binds without the Data API's
    ``arrayValue``, which would need the element type stated a second time.
    """
    if value is None:
        return {"isNull": True}
    if isinstance(value, bool):
        # Before int, because bool is an int in Python and 'true' is what Postgres wants.
        return {"stringValue": "true" if value else "false"}
    if isinstance(value, (list, tuple, set)):
        return {"stringValue": "{" + ",".join(_array_element(v) for v in value) + "}"}
    if isinstance(value, dt.datetime):
        return {"stringValue": value.isoformat()}
    if isinstance(value, (dt.date, dt.time)):
        return {"stringValue": value.isoformat()}
    return {"stringValue": str(value)}


def _array_element(value: Any) -> str:
    if value is None:
        return "NULL"
    text = str(value)
    if any(c in text for c in ',{}"\\ '):
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text


def parameters(params: dict[str, Any]) -> list[dict[str, Any]]:
    """A parameter dict as the Data API's named parameter list."""
    return [{"name": name, "value": encode(value)} for name, value in params.items()]


def _decode(field: dict[str, Any], type_name: str) -> Any:
    """One Data API field as the value psycopg would have returned for that column."""
    if field.get("isNull"):
        return None

    if "arrayValue" in field:
        return _decode_array(field["arrayValue"])

    # Exactly one of these is present on a non-null scalar field.
    for key in ("stringValue", "longValue", "doubleValue", "booleanValue", "blobValue"):
        if key in field:
            raw = field[key]
            break
    else:  # pragma: no cover - the Data API always sets one
        return None

    name = type_name.lstrip("_")

    if name == "numeric":
        # The one that matters most. Postgres numeric arrives as a string precisely so it
        # does not lose precision in transit, and turning it into a float here would throw
        # away the thing the wire format went to trouble to preserve. Every currency and
        # ratio column in this schema is numeric.
        return Decimal(str(raw))
    if name in ("int2", "int4", "int8"):
        return int(raw)
    if name in ("float4", "float8"):
        return float(raw)
    if name == "bool":
        return raw if isinstance(raw, bool) else str(raw).lower() in ("true", "t", "1")
    if name == "date":
        return dt.date.fromisoformat(str(raw))
    if name in ("timestamp", "timestamptz"):
        return _decode_timestamp(str(raw), utc=name == "timestamptz")
    if name == "time":
        return dt.time.fromisoformat(str(raw))
    return raw


def _decode_timestamp(raw: str, utc: bool) -> dt.datetime:
    """Parse a Data API timestamp, which is ISO-ish but not always ISO.

    Two shapes turn up that ``fromisoformat`` will not take on its own: a space instead of
    a ``T``, which it tolerates, and a fractional part the Data API pads or truncates to
    lengths Python did not always accept. Normalising to six digits is cheaper than being
    surprised by a nine-digit one.
    """
    text = raw.strip().replace(" ", "T", 1)
    if "." in text:
        head, _, tail = text.partition(".")
        digits = "".join(c for c in tail if c.isdigit())[:6]
        suffix = tail[len(digits) :] if not digits else tail.lstrip("0123456789")
        text = f"{head}.{digits.ljust(6, '0')}{suffix}"
    parsed = dt.datetime.fromisoformat(text)
    if utc and parsed.tzinfo is None:
        # The cluster parameter group pins the session timezone to UTC, so a naive
        # timestamptz off the wire is already UTC and only needs saying so. psycopg
        # returns these tz-aware, and a tz-naive twin would compare unequal.
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def _decode_array(array_value: dict[str, Any]) -> list[Any]:
    for key, kind in (
        ("stringValues", str),
        ("longValues", int),
        ("doubleValues", float),
        ("booleanValues", bool),
    ):
        if key in array_value:
            return [kind(v) for v in array_value[key]]
    if "arrayValues" in array_value:
        return [_decode_array(v) for v in array_value["arrayValues"]]
    return []


def records(response: dict[str, Any]) -> list[dict[str, Any]]:
    """The rows of an ``ExecuteStatement`` response, as dicts keyed by column name."""
    metadata = response.get("columnMetadata") or []
    names = [column.get("name") or f"column_{i}" for i, column in enumerate(metadata)]
    types = [column.get("typeName", "") for column in metadata]
    return [
        {names[i]: _decode(field, types[i]) for i, field in enumerate(row)}
        for row in response.get("records") or []
    ]


def _is_retryable(exc: Exception) -> bool:
    code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
    if code in RETRYABLE_CODES:
        return True
    text = str(exc).lower()
    return any(marker in text for marker in RESUMING_MARKERS)


@dataclass(frozen=True)
class DataApi:
    """One Aurora cluster, reached over HTTPS with a secret and no connection.

    ``secret_arn`` decides what this client is allowed to do, which is the whole read-only
    story: the deployed server is handed the ``mcp_readonly`` secret and the seed script is
    handed the master one. Nothing else distinguishes them.
    """

    cluster_arn: str
    secret_arn: str
    database: str = "bizdata"

    @classmethod
    def from_env(cls, secret_env: str = "BIZDATA_SECRET_ARN") -> DataApi:
        cluster = os.environ.get("BIZDATA_CLUSTER_ARN")
        secret = os.environ.get(secret_env)
        missing = [n for n, v in (("BIZDATA_CLUSTER_ARN", cluster), (secret_env, secret)) if not v]
        if missing:
            raise DataApiError(
                f"The aws backend needs {' and '.join(missing)} in the environment. "
                f"Both come out of `terraform output` in infra/main."
            )
        return cls(
            cluster_arn=str(cluster),
            secret_arn=str(secret),
            database=os.environ.get("BIZDATA_DATABASE", "bizdata"),
        )

    @cached_property
    def client(self) -> Any:
        import boto3

        return boto3.client("rds-data")

    def _call(self, operation: str, **kwargs: Any) -> dict[str, Any]:
        deadline = time.monotonic() + RESUME_TIMEOUT_S
        attempt = 0
        while True:
            try:
                return getattr(self.client, operation)(**kwargs)
            except Exception as exc:
                if not _is_retryable(exc) or time.monotonic() >= deadline:
                    raise DataApiError(f"{operation} failed: {exc}") from exc
                pause = RESUME_BACKOFF_S[min(attempt, len(RESUME_BACKOFF_S) - 1)]
                time.sleep(min(pause, max(0.0, deadline - time.monotonic())))
                attempt += 1

    def query(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        response = self._call(
            "execute_statement",
            resourceArn=self.cluster_arn,
            secretArn=self.secret_arn,
            database=self.database,
            sql=sql,
            parameters=parameters(params or {}),
            includeResultMetadata=True,
        )
        return records(response)

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> int:
        """Run one statement, returning rows affected. For DDL and single-row DML."""
        response = self._call(
            "execute_statement",
            resourceArn=self.cluster_arn,
            secretArn=self.secret_arn,
            database=self.database,
            sql=sql,
            parameters=parameters(params or {}),
        )
        return int(response.get("numberOfRecordsUpdated", 0))

    def batch(self, sql: str, parameter_sets: Sequence[dict[str, Any]]) -> None:
        if not parameter_sets:
            return
        self._call(
            "batch_execute_statement",
            resourceArn=self.cluster_arn,
            secretArn=self.secret_arn,
            database=self.database,
            sql=sql,
            parameterSets=[parameters(p) for p in parameter_sets],
        )


def chunked(rows: Iterable[Any], size: int) -> Iterable[list[Any]]:
    batch: list[Any] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch
