#!/usr/bin/env python3
"""Create the mcp_readonly role in Aurora and prove it is read-only.

    eval "$(terraform -chdir=infra/main output -raw shell_exports)"
    python scripts/bootstrap_aurora.py

Run it after scripts/seed.py, every time. `grant select on all tables` is a loop over the
tables that exist when it runs, and seed.py drops and recreates all six on every load.

Everything happens over the Data API, using the RDS-managed master secret. Nothing here
touches the console, and the password never appears in a file, a variable or a log line —
it is read from the Secrets Manager secret Terraform created and written straight into a
CREATE ROLE.

The last thing the script does is the part worth having: it connects again as
mcp_readonly and checks four claims rather than asserting them in a README. That it can
read. That `jit` really is off for that role, which is the step-3 performance finding and
the reason this file sets a role-level GUC at all. That an INSERT is refused. And that a
CREATE TABLE is refused. Step 5's Done-when asks for the INSERT rejection as evidence, and
evidence you generated on purpose is better than evidence you went looking for afterwards.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from server.dataapi import DataApi, DataApiError  # noqa: E402
from server.db import split_statements  # noqa: E402

GRANTS_PATH = REPO_ROOT / "db" / "seeds" / "mcp_readonly.sql"

ROLE = "mcp_readonly"


def quote_literal(value: str) -> str:
    """Single-quote a string for Postgres.

    CREATE ROLE is a utility statement and Postgres does not accept bound parameters in
    one, so the password has to be interpolated into the text. The generated password
    comes from a restricted alphabet with no quote in it, and this doubles any anyway —
    the check costs nothing and the alternative is a rule that holds until someone widens
    the character set in secrets.tf.
    """
    return "'" + value.replace("'", "''") + "'"


def secret_credentials(api: DataApi, secret_arn: str) -> dict[str, str]:
    import boto3

    payload = boto3.client("secretsmanager").get_secret_value(SecretId=secret_arn)
    return json.loads(payload["SecretString"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--skip-verify",
        action="store_true",
        help="create the role but do not connect as it afterwards",
    )
    args = parser.parse_args(argv)

    master = DataApi.from_env(secret_env="BIZDATA_MASTER_SECRET_ARN")
    readonly = DataApi.from_env(secret_env="BIZDATA_SECRET_ARN")

    credentials = secret_credentials(master, readonly.secret_arn)
    username = credentials["username"]
    password = credentials["password"]
    if username != ROLE:
        raise SystemExit(f"The readonly secret names user {username!r}, expected {ROLE!r}.")

    print(f"cluster  {master.cluster_arn}")
    print(f"database {master.database}")

    # Create or update, decided here rather than in a DO block, because the statement
    # splitter that feeds the Data API does not parse dollar-quoted bodies.
    exists = master.query(
        "select 1 as present from pg_roles where rolname = :role", {"role": ROLE}
    )
    verb, past = ("alter", "updated") if exists else ("create", "created")
    master.execute(f"{verb} role {ROLE} with login password {quote_literal(password)}")
    print(f"  {past} role {ROLE}")

    # The owner is whoever seed.py runs as, which is the master user, and it is read from
    # the secret rather than hardcoded so that ALTER DEFAULT PRIVILEGES names the role
    # that will actually create the tables.
    owner = secret_credentials(master, master.secret_arn)["username"]

    statements = split_statements(
        GRANTS_PATH.read_text().format(database=master.database, owner=owner)
    )
    for statement in statements:
        master.execute(statement)
    print(f"  applied {GRANTS_PATH.relative_to(REPO_ROOT)}, {len(statements)} statements")

    if args.skip_verify:
        return 0

    print()
    print(f"verifying, connected as {ROLE}")
    failures: list[str] = []

    tables = readonly.query(
        "select count(*) as n from information_schema.tables where table_schema = 'public'"
    )
    print(f"  select                 ok, {tables[0]['n']} relations visible")

    jit = readonly.query("show jit")
    jit_value = list(jit[0].values())[0]
    if str(jit_value) == "off":
        print(f"  jit                    off")
    else:
        failures.append(f"jit is {jit_value!r}, expected 'off'")

    zone = readonly.query("show timezone")
    print(f"  timezone               {list(zone[0].values())[0]}")

    for label, statement in (
        ("insert refused", "insert into clients (id, name, segment) values (-1, 'x', 'enterprise')"),
        ("create refused", "create table should_not_exist (id integer)"),
    ):
        try:
            readonly.execute(statement)
        except DataApiError as exc:
            # Keep the Postgres error and drop the boto3 wrapper around it. The useful
            # part is "ERROR: permission denied for table clients; SQLState: 42501", not
            # the name of the API operation that carried it.
            detail = str(exc).split("\n")[0]
            _, _, postgres = detail.partition("ERROR:")
            print(f"  {label:<22} ERROR: {(postgres or detail).strip().rstrip('.')}")
        else:
            failures.append(f"{label}: the statement SUCCEEDED, which means the role is not read-only")

    if failures:
        print()
        for failure in failures:
            print(f"FAILED  {failure}")
        return 1

    print()
    print("mcp_readonly can read, cannot write, and runs with jit off.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
