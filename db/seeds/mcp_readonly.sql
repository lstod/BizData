-- The read-only role the deployed server runs as.
--
-- This file is where "read-only" stops being a property of the code and becomes a
-- property of the database. The MCP server could be rewritten tomorrow to issue an
-- UPDATE and it would still fail, because the role it connects as has no privilege to
-- perform one. That is the distinction worth making out loud in SECURITY.md: the grant
-- is the guarantee, and the application is not being trusted to behave.
--
-- Applied by scripts/bootstrap_aurora.py, which creates the role and sets its password
-- from Secrets Manager first — a password does not belong in a committed file, and
-- CREATE ROLE cannot take a bound parameter anyway, since Postgres does not accept
-- parameters in utility statements.
--
-- Run it AFTER scripts/seed.py, every time. `grant select on all tables` is not a
-- standing rule, it is a loop over the tables that exist at the moment it runs, and
-- seed.py drops and recreates all six on every load. The ALTER DEFAULT PRIVILEGES below
-- covers tables created later by the seeding role, which makes the ordering forgiving
-- rather than load-bearing, but re-running after a seed is still the habit to keep.
--
-- No dollar-quoted DO block anywhere in here, deliberately. server/db.py splits files
-- into statements for the Data API using the step-3 scanner, which understands comments,
-- string literals and casts but not $$ bodies. Existence checks therefore live in the
-- Python that calls this, not in PL/pgSQL.

-- Reach the database and see the schema. Neither is implied by the other.
grant connect on database {database} to mcp_readonly;
grant usage on schema public to mcp_readonly;

-- Every table and view that exists right now.
grant select on all tables in schema public to mcp_readonly;

-- And every one the seeding role creates from here on, so a reseed does not silently
-- leave the server looking at tables it cannot read.
alter default privileges for role {owner} in schema public grant select on tables to mcp_readonly;

-- Nothing else. No INSERT, no UPDATE, no DELETE, no TRUNCATE, no CREATE. Stated as an
-- explicit revoke rather than left as an absence, because "we never granted it" and "it
-- is not granted" are the same fact only if nobody ever ran anything else against this
-- database by hand.
revoke create on schema public from mcp_readonly;
revoke all on database {database} from public;
grant connect on database {database} to public;

-- Two session settings, and the story behind the first one changed twice while it was
-- being written. Both versions are worth keeping, because the conclusion held and the
-- reasoning did not.
--
-- LocalBackend issues `set jit = off` and `set time zone 'UTC'` on every connection.
-- The Data API gives every call its own session, so a SET does not survive to the next
-- statement, and step 3 concluded these would have to become Aurora cluster parameters.
--
-- They cannot be. `jit` is not a modifiable parameter in the aurora-postgresql16 family
-- at all — not in the cluster parameter group (128 parameters) and not in the instance
-- group (324). Checked with describe-engine-default-cluster-parameters and
-- describe-engine-default-parameters rather than assumed.
--
-- And then the reason that does not matter: `show jit` on a fresh Aurora PostgreSQL
-- 16.10 cluster returns off already. Aurora ships JIT disabled where community Postgres
-- 16 ships it enabled, which is why there is no parameter to change — there is nothing
-- to turn off. Step 3's 2,599ms-versus-211ms problem does not exist on this engine, and
-- claiming to have fixed it here would be claiming credit for a default.
--
-- The setting stays anyway, and not out of superstition. An engine default is a fact
-- about a version, not a promise about the next one, and the thing it protects against
-- is a minor upgrade quietly re-enabling JIT and turning every tool call into two and a
-- half seconds again — with the compile time attributed to no plan node, so EXPLAIN
-- ANALYZE would account for a tenth of the query's own runtime and look healthy. Pinning
-- it costs one statement at bootstrap.
--
-- A role-level setting is the right hook for both. Postgres stores these in
-- pg_db_role_setting and applies them when a session starts, which is exactly when the
-- Data API needs them applied, and it scopes them to the one role running the view stack
-- rather than to every connection the cluster accepts.
alter role mcp_readonly set jit = off;
alter role mcp_readonly set timezone = 'UTC';
