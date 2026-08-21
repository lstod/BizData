"""Where generated rows go.

Steps 1 through 4 run against local Postgres in Docker; Aurora does not exist until
step 5. Rather than write a local-only loader and rewrite it later, the generator hands
plain row tuples to a writer chosen by ``BIZDATA_DB_BACKEND``. The rows are identical in
both cases and only the binding differs, which is the same seam ``server/db.py`` uses
for queries at step 3.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Iterable, Protocol, Sequence

# The aws writer shares server/dataapi.py with the read backend, and seed.py puts only
# scripts/ on the path. Adding the repository root here rather than in seed.py keeps the
# dependency with the code that has it.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

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
    """RDS Data API, loading through ``BatchExecuteStatement``.

    There is no ``COPY`` down this pipe, so the 40,000 time entries go as batches of a
    thousand parameter sets — about forty calls, which is the number the decision record
    budgeted for. The scale answer, and it belongs in the README rather than in this
    class, is a CSV in S3 and ``aws_s3.table_import_from_s3``.

    Two things differ from the local writer rather than being a straight port.
    ``apply_sql`` takes whole files and the Data API takes one statement per call, so the
    files are split on the way through. And every value is bound as a string, which means
    the generated ``INSERT`` has to say what each column is: ``:hours::numeric`` rather
    than ``:hours``. Those casts come from ``information_schema`` on first use rather than
    from a table of types in this file — a second copy of the schema is a second thing to
    keep in step with db/schema.sql, and it would drift silently the first time a column
    changed type.

    This writer holds the master secret and the deployed server holds ``mcp_readonly``.
    That split is the read-only claim, so the two secrets are read from two differently
    named environment variables and never fall back to one another.
    """

    # A thousand parameter sets per call, dropping to 250 if the Data API rejects the
    # request as too large. Adaptive rather than pessimistic: 40 calls at the larger size
    # is seconds, and 160 at the smaller one is still fine, so there is no reason to pay
    # the smaller size on every run for a limit that may never be hit.
    CHUNK_SIZES = (1000, 250, 50)

    def __init__(self, dsn: str | None = None) -> None:
        from server.dataapi import DataApi

        self.api = DataApi.from_env(secret_env="BIZDATA_MASTER_SECRET_ARN")
        self._types: dict[str, dict[str, str]] = {}

    def apply_sql(self, sql: str) -> None:
        from server.db import split_statements

        for statement in split_statements(sql):
            self.api.execute(statement)

    def column_types(self, table: str) -> dict[str, str]:
        """``column_name -> udt_name`` for one table, read from the live database."""
        if table not in self._types:
            rows = self.api.query(
                "select column_name, udt_name from information_schema.columns "
                "where table_schema = 'public' and table_name = :table",
                {"table": table},
            )
            if not rows:
                raise RuntimeError(
                    f"No table {table!r} in the database. The schema has to be applied "
                    f"before rows can be loaded into it."
                )
            self._types[table] = {r["column_name"]: r["udt_name"] for r in rows}
        return self._types[table]

    def insert_sql(self, table: str, columns: Sequence[str]) -> str:
        types = self.column_types(table)
        missing = [c for c in columns if c not in types]
        if missing:
            raise RuntimeError(f"{table} has no column {', '.join(missing)}")
        collist = ", ".join(columns)
        values = ", ".join(f":{c}::{types[c]}" for c in columns)
        return f"insert into {table} ({collist}) values ({values})"

    def load(self, table: str, columns: Sequence[str], rows: Iterable[tuple]) -> int:
        from server.dataapi import DataApiError, chunked

        sql = self.insert_sql(table, columns)
        sets = [dict(zip(columns, row)) for row in rows]

        size_index = 0
        remaining = sets
        while True:
            try:
                for chunk in chunked(remaining, self.CHUNK_SIZES[size_index]):
                    self.api.batch(sql, chunk)
                return len(sets)
            except DataApiError:
                size_index += 1
                if size_index >= len(self.CHUNK_SIZES):
                    raise
                # Start the table over. Every id is generated, so a partial load followed
                # by a retry would collide on the primary key rather than resume.
                self.api.execute(f"delete from {table}")

    def close(self) -> None:
        return None


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
