"""Where generated rows go.

Steps 1 through 4 run against local Postgres in Docker; Aurora does not exist until
step 5. Rather than write a local-only loader and rewrite it later, the generator hands
plain row tuples to a writer chosen by ``BIZDATA_DB_BACKEND``. The rows are identical in
both cases and only the binding differs, which is the same seam ``server/db.py`` uses
for queries at step 3.
"""

from __future__ import annotations

import os
from typing import Iterable, Protocol, Sequence

DEFAULT_DSN = "postgresql://bizdata:bizdata@localhost:5433/bizdata"

# Dependency order. Foreign keys are checked on insert, so this is the order rows have
# to land in and the order the writers are handed tables.
TABLE_ORDER = (
    "clients",
    "people",
    "engagements",
    "sow_line_items",
    "time_entries",
    "invoices",
)


class Writer(Protocol):
    def apply_sql(self, sql: str) -> None: ...

    def load(self, table: str, columns: Sequence[str], rows: Iterable[tuple]) -> int: ...

    def close(self) -> None: ...


class LocalWriter:
    """psycopg against local Postgres, loading through COPY.

    COPY is used rather than executemany because 40,000 rows through round-trips is
    slow enough to discourage re-running the seed, and re-running the seed is the whole
    point of a deterministic generator.
    """

    def __init__(self, dsn: str | None = None) -> None:
        import psycopg  # imported here so the aws path never needs it installed

        self._psycopg = psycopg
        self.dsn = dsn or os.environ.get("BIZDATA_DSN", DEFAULT_DSN)
        self.conn = psycopg.connect(self.dsn, autocommit=False)

    def apply_sql(self, sql: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute(sql)
        self.conn.commit()

    def load(self, table: str, columns: Sequence[str], rows: Iterable[tuple]) -> int:
        collist = ", ".join(columns)
        count = 0
        with self.conn.cursor() as cur:
            with cur.copy(f"copy {table} ({collist}) from stdin") as copy:
                for row in rows:
                    copy.write_row(row)
                    count += 1
        self.conn.commit()
        return count

    def close(self) -> None:
        self.conn.close()


class AwsWriter:
    """RDS Data API, filled in at step 5.

    The shape is already decided: ``BatchExecuteStatement`` in chunks of a thousand,
    about forty calls for the time entries. It is a stub rather than an absence so the
    backend switch is exercised from day one and step 5 is a deployment rather than a
    redesign.

    One thing that will differ here rather than being a straight port: ``apply_sql``
    takes whole files, and the Data API's ``ExecuteStatement`` accepts one statement per
    call, so the AWS implementation has to split on statement boundaries where the local
    one hands psycopg the file as-is.
    """

    def __init__(self, dsn: str | None = None) -> None:
        raise NotImplementedError(
            "The aws backend lands at step 5, with the Aurora cluster and its Data API. "
            "Until then run against local Postgres: unset BIZDATA_DB_BACKEND or set it "
            "to 'local', and start the database with `docker compose up -d`."
        )

    def apply_sql(self, sql: str) -> None:  # pragma: no cover - unreachable stub
        raise NotImplementedError

    def load(self, table: str, columns: Sequence[str], rows: Iterable[tuple]) -> int:  # pragma: no cover
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover
        raise NotImplementedError


WRITERS = {"local": LocalWriter, "aws": AwsWriter}


def get_writer(backend: str | None = None, dsn: str | None = None) -> Writer:
    name = (backend or os.environ.get("BIZDATA_DB_BACKEND") or "local").lower()
    try:
        cls = WRITERS[name]
    except KeyError:
        raise SystemExit(
            f"Unknown BIZDATA_DB_BACKEND {name!r}. Expected one of: {', '.join(sorted(WRITERS))}."
        ) from None
    return cls(dsn)
