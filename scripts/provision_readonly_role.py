# pyright: basic
# (asyncpg ships no type stubs; this file's only job is a handful of raw
# asyncpg calls, so strict mode here is mostly noise.)
"""Operator-only CLI to provision a read-only Postgres role on a target
database, for use as that workspace's DataSourceConnection.

Run this AFTER the target dataset is fully loaded. GRANT ... ON ALL TABLES
IN SCHEMA only covers tables that exist at the moment it runs, not ones
added afterward (confirmed by testing: a table created after provisioning
was invisible to the role until the grant was re-run). The
ALTER DEFAULT PRIVILEGES line below only extends that to tables created
*later by this same admin connection* -- not to tables added by a
different role that owns the schema.

Also defense-in-depth only, same caveat as Phase 6 spike 1:
REVOKE EXECUTE FROM PUBLIC only protects functions that exist at the time
this runs, not ones added later by whoever owns the target schema. The
real defense against a SECURITY DEFINER bypass is the function-call
allowlist in app/query/validation.py."""

import argparse
import asyncio
import secrets

import asyncpg


async def provision(admin_dsn: str, role_name: str, database: str, schema: str) -> str:
    """Connects to the target database as a superuser/admin (admin_dsn)
    and creates a brand-new, deliberately limited database login
    (role_name) that this server's AI-facing queries will actually use.
    Each line below grants or removes one specific permission -- plain
    English for each is in the comments."""
    password = secrets.token_urlsafe(24)
    conn = await asyncpg.connect(admin_dsn)
    try:
        # Start clean, in case a role with this name already exists.
        await conn.execute(f'drop role if exists "{role_name}"')
        # Create the login itself, with a freshly generated password.
        await conn.execute(f"create role \"{role_name}\" login password '{password}'")
        # Allow it to connect to the database at all...
        await conn.execute(f'grant connect on database "{database}" to "{role_name}"')
        # ...and to "see into" this particular schema (a schema is a
        # named group of tables, like a folder of tables)...
        await conn.execute(f'grant usage on schema "{schema}" to "{role_name}"')
        # ...and to read (but not write) every table that exists in it
        # right now.
        await conn.execute(f'grant select on all tables in schema "{schema}" to "{role_name}"')
        # Extend that same read permission to tables created LATER, as
        # long as they're created by this same admin connection (see
        # the module docstring above for exactly what this does and
        # doesn't cover).
        await conn.execute(
            f'alter default privileges in schema "{schema}" '
            f'grant select on tables to "{role_name}"'
        )
        # Block this role from calling any custom function in the
        # schema -- defense-in-depth against the SECURITY DEFINER
        # bypass described in the module docstring.
        await conn.execute(f'revoke execute on all functions in schema "{schema}" from public')
        await conn.execute(
            f'alter default privileges in schema "{schema}" revoke execute on functions from public'
        )
        # Grants to PUBLIC apply to every role, this one included, and
        # can't be revoked per role. A schema that grants CREATE to PUBLIC
        # (Pagila's dump does: "GRANT ALL ON SCHEMA public TO PUBLIC")
        # would let this "read-only" role create tables. Not revoked here,
        # since that would change access for every other role on a
        # database this script doesn't own outright -- refused instead,
        # so it can't go unnoticed.
        can_create = await conn.fetchval(
            "select has_schema_privilege($1, $2, 'CREATE')", role_name, schema
        )
        if can_create:
            raise RuntimeError(
                f'role "{role_name}" can still CREATE in schema "{schema}" through a grant '
                f'to PUBLIC. Fix with: REVOKE CREATE ON SCHEMA "{schema}" FROM PUBLIC;'
            )
    finally:
        await conn.close()
    return password


async def drop_role(admin_dsn: str, role_name: str) -> None:
    """Removes a role created by provision() above -- used by tests to
    clean up the throwaway roles they create. DROP ROLE on its own
    fails if the role still holds any granted privileges (which it
    will, since provision() grants several) -- DROP OWNED BY strips
    those first."""
    conn = await asyncpg.connect(admin_dsn)
    try:
        await conn.execute(f'drop owned by "{role_name}"')
        await conn.execute(f'drop role if exists "{role_name}"')
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admin-dsn", required=True, help="Superuser connection string")
    parser.add_argument("--role-name", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--schema", default="public")
    args = parser.parse_args()

    password = asyncio.run(provision(args.admin_dsn, args.role_name, args.database, args.schema))
    print(f"Role '{args.role_name}' created on database '{args.database}'.")
    print(f"Password (shown once, store it now): {password}")
    print(
        "Note: both the SELECT grant and the EXECUTE revoke only reliably "
        "cover the current moment and tables/functions created later by "
        "THIS admin connection. See the module docstring for what that "
        "does and doesn't cover."
    )


if __name__ == "__main__":
    main()
