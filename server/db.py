"""Where the tools' queries run.

The read-side twin of scripts/writers.py, and the same seam for the same reason: steps 1
through 4 run against local Postgres in Docker, Aurora does not exist until step 5, and a
local-only query layer written now is a rewrite then. One interface, two backends, chosen
by ``BIZDATA_DB_BACKEND``.

Every query lives as text in db/sql/ with ``:name`` placeholders and is identical in both
backends. Only the binding differs: the RDS Data API takes ``:name`` natively, and psycopg
wants ``%(name)s``, so the local backend rewrites on the way in. Writing the files in the
Data API's form rather than psycopg's is deliberate — the form that survives to production
is the one that should be in the repository, and it reads as SQL rather than as Python
string formatting.

The rewrite is a scanner rather than a regular expression, and that is not fastidiousness.
A ``:name`` pattern applied to this repository's SQL eats the second half of every
``::date`` cast, and there are colons inside the comments and string literals too. The
scanner below skips casts, line comments, block comments, quoted literals and quoted
identifiers, which is the whole grammar these files use.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
SQL_DIR = REPO_ROOT / "db" / "sql"

DEFAULT_DSN = "postgresql://bizdata:bizdata@localhost:5433/bizdata"

# Postgres identifier rules, near enough: a placeholder is a colon followed by a letter or
# underscore. Anything else after a colon is an operator or punctuation and is left alone.
_NAME_START = re.compile(r"[A-Za-z_]")
_NAME_CHAR = re.compile(r"[A-Za-z0-9_]")


class QueryError(RuntimeError):
    """A query could not be prepared. Always a bug in the repository, never in the input."""


def _scan(sql: str):
    """Walk ``sql``, yielding ``(kind, text)`` where kind is 'sql', 'skip' or a param name.

    'skip' is anything the rewriter must pass through untouched: comments, string
    literals, quoted identifiers, and ``::`` casts.
    """
    i, n = 0, len(sql)
    start = 0
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""

        if ch == "-" and nxt == "-":
            end = sql.find("\n", i)
            end = n if end == -1 else end
        elif ch == "/" and nxt == "*":
            end = sql.find("*/", i + 2)
            end = n if end == -1 else end + 2
        elif ch in "'\"":
            # Doubled quotes escape themselves in both literals and identifiers.
            j = i + 1
            while j < n:
                if sql[j] == ch:
                    if j + 1 < n and sql[j + 1] == ch:
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            end = j
        elif ch == ":" and nxt == ":":
            end = i + 2
        elif ch == ":" and _NAME_START.match(nxt or ""):
            j = i + 1
            while j < n and _NAME_CHAR.match(sql[j]):
                j += 1
            if start < i:
                yield "sql", sql[start:i]
            yield "param", sql[i + 1 : j]
            i = start = j
            continue
        else:
            i += 1
            continue

        if start < i:
            yield "sql", sql[start:i]
        yield "skip", sql[i:end]
        i = start = end

    if start < n:
        yield "sql", sql[start:n]


def placeholders(sql: str) -> set[str]:
    """Every ``:name`` in ``sql``, ignoring casts, comments and quoted text."""
    return {text for kind, text in _scan(sql) if kind == "param"}


def to_pyformat(sql: str) -> str:
    """Rewrite ``:name`` to psycopg's ``%(name)s``.

    Every other percent sign in the text is doubled, including the ones inside string
    literals and comments. psycopg scans the whole query for ``%`` and does not care that
    a given one sits inside quotes, so ``format('%s%%', x)`` in a query that also carries
    a named parameter is read as a positional placeholder and the call fails with the two
    styles mixed. Doubling here and letting psycopg halve it back is the only correct
    order.
    """
    parts: list[str] = []
    for kind, text in _scan(sql):
        parts.append(f"%({text})s" if kind == "param" else text.replace("%", "%%"))
    return "".join(parts)


@lru_cache(maxsize=None)
def load_sql(name: str) -> str:
    """Read ``db/sql/<name>.sql``. Cached: these are read-only files read on every call."""
    path = SQL_DIR / f"{name}.sql"
    try:
        return path.read_text()
    except FileNotFoundError:
        raise QueryError(f"No query file at {path.relative_to(REPO_ROOT)}") from None


def check_params(sql: str, params: dict[str, Any], where: str) -> None:
    """Fail loudly on a placeholder with no value, or a value with no placeholder.

    psycopg raises on the first and silently ignores the second, and an ignored parameter
    is a filter that quietly did not apply — a tool that returns the whole portfolio when
    it was asked for one client.
    """
    wanted = placeholders(sql)
    given = set(params)
    if missing := wanted - given:
        raise QueryError(f"{where}: no value for {', '.join(sorted(missing))}")
    if extra := given - wanted:
        raise QueryError(f"{where}: {', '.join(sorted(extra))} bound but never used")


class Backend(Protocol):
    def query(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]: ...


class LocalBackend:
    """psycopg against local Postgres, one connection per query.

    Per query rather than pooled, and that is the point rather than a shortcut: the Data
    API at step 5 is a stateless HTTPS call with no connection to hold, and the Lambda it
    runs in has nowhere sensible to keep a pool. Matching that shape now means nothing
    about connection lifetime changes when the backend swaps.

    Every query runs in a read-only transaction. The deployed server gets this from the
    ``mcp_readonly`` Postgres role instead — a permission rather than a promise — but the
    local database is owned by the seed script, so the guarantee has to come from here.
    """

    def __init__(self, dsn: str | None = None) -> None:
        import psycopg  # imported here so the aws path never needs it installed

        self._psycopg = psycopg
        self.dsn = dsn or os.environ.get("BIZDATA_DSN", DEFAULT_DSN)

    def query(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        from psycopg.rows import dict_row

        params = params or {}
        check_params(sql, params, "query")

        # A query with no placeholders is handed to psycopg with no parameters at all,
        # rather than an empty dict. Otherwise psycopg treats the text as a format string
        # and every literal percent sign in it would need doubling for no reason.
        text, values = (to_pyformat(sql), params) if params else (sql, None)

        with self._psycopg.connect(self.dsn, row_factory=dict_row) as conn:
            conn.read_only = True
            with conn.cursor() as cur:
                # UTC explicitly, because half the mess in this dataset is about when a
                # row was filed and db/checks/ compares against UTC instants.
                cur.execute("set time zone 'UTC'")

                # JIT off, and this one is worth more than it looks. The tool queries read
                # a stack of four views, and the planner's estimate for that stack comes
                # out around 1.5 million — far above the default jit_above_cost of 100000
                # — so Postgres compiles the plan before running it. Measured on
                # list_engagements: 2.6 seconds with JIT, 0.22 without, and the compile
                # time shows up in no plan node, so EXPLAIN ANALYZE reports a query that
                # accounts for a tenth of its own runtime.
                #
                # The estimate is wrong rather than the query being expensive. JIT pays for
                # itself on long analytical scans; every query here is tens of milliseconds
                # of actual work, so it can only ever be overhead.
                #
                # Step 5 needs this as an Aurora cluster parameter rather than a statement:
                # the Data API gives each call its own session, so a SET does not survive
                # to the query that follows it.
                cur.execute("set jit = off")

                cur.execute(text, values)
                return list(cur.fetchall())


class AwsBackend:
    """RDS Data API, filled in at step 5.

    The shape is already decided and the SQL needs no translation to reach it: the files
    in db/sql/ are already in the Data API's ``:name`` form, so this backend passes the
    text through unchanged and builds the typed ``parameters`` list that
    ``ExecuteStatement`` takes — each value tagged ``stringValue``, ``longValue``,
    ``doubleValue`` or ``isNull``, which is the one real piece of work here.

    Two things that will differ rather than being a straight port: the Data API returns
    ``records`` as positional typed values rather than named columns, so column names come
    from ``columnMetadata`` and the dict rows are assembled here; and responses are capped
    at 1 MiB, which every tool already satisfies by aggregating, but which becomes an error
    rather than a slow response if one ever stops.
    """

    def __init__(self, dsn: str | None = None) -> None:
        raise NotImplementedError(
            "The aws backend lands at step 5, with the Aurora cluster and its Data API. "
            "Until then run against local Postgres: unset BIZDATA_DB_BACKEND or set it "
            "to 'local', and start the database with `docker compose up -d`."
        )

    def query(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:  # pragma: no cover
        raise NotImplementedError


BACKENDS = {"local": LocalBackend, "aws": AwsBackend}


def get_backend(backend: str | None = None, dsn: str | None = None) -> Backend:
    name = (backend or os.environ.get("BIZDATA_DB_BACKEND") or "local").lower()
    try:
        cls = BACKENDS[name]
    except KeyError:
        raise SystemExit(
            f"Unknown BIZDATA_DB_BACKEND {name!r}. Expected one of: {', '.join(sorted(BACKENDS))}."
        ) from None
    return cls(dsn)


_backend: Backend | None = None


def backend() -> Backend:
    """The process-wide backend, built on first use.

    Lazy rather than built at import so that importing server.app — which the tests and
    the Lambda both do — does not require a reachable database.
    """
    global _backend
    if _backend is None:
        _backend = get_backend()
    return _backend


def query(name: str, params: dict[str, Any] | None = None, **substitutions: str) -> list[dict[str, Any]]:
    """Run ``db/sql/<name>.sql``.

    ``substitutions`` fills ``{marker}`` slots in the file, which exist only where a
    grouping expression varies and a bound parameter cannot reach — see
    db/sql/get_time_summary.sql. Values come from fixed dictionaries in the tool modules,
    never from tool arguments.
    """
    sql = load_sql(name)
    if substitutions:
        sql = sql.format(**substitutions)
    return backend().query(sql, params)


def all_query_names() -> Sequence[str]:
    """Every query file, for the harness that checks they all parse and bind."""
    return sorted(p.stem for p in SQL_DIR.glob("*.sql"))
