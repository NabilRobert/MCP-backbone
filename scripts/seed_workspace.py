"""Operator-only CLI to create a workspace, its data source connection,
console login, and first API key, without an invite. (Invited users sign
up themselves at /console/signup; see app/auth/signup.py.)"""

import argparse
import asyncio
import datetime
import getpass

from app.auth.models import ApiKey, ConsoleCredential, Workspace
from app.auth.security import generate_api_key, hash_api_key, hash_password
from app.db import SessionLocal
from app.query.models import DataSourceConnection
from app.query.network_guard import ensure_public_target


async def seed(
    name: str,
    connection_url: str | None,
    console_username: str,
    console_password: str,
    query_rate_limit: int | None = None,
    sample_rate_limit: int | None = None,
) -> str:
    """Creates everything a brand-new workspace needs in one go: the
    workspace itself, which real database it's allowed to query, a
    dashboard login, and its first API key. Returns the API key in
    plain text -- the ONLY time it ever exists in plain text, since
    only its hash gets stored (see app/auth/security.py).

    The two rate limits are optional per-hour allowances for this
    workspace alone; None keeps the server-wide defaults. This script
    is the only place they can be set, on purpose (see Workspace in
    app/auth/models.py)."""
    # Same address check a self-serve signup gets: the operator can mistype
    # too. No URL means the server's default database, which is exempt.
    if connection_url:
        await ensure_public_target(connection_url)
    async with SessionLocal() as session:
        workspace = Workspace(
            name=name,
            created_at=datetime.datetime.now(datetime.UTC),
            query_rate_limit=query_rate_limit,
            sample_rate_limit=sample_rate_limit,
        )
        session.add(workspace)
        await session.flush()  # assigns workspace.id before it's used below

        if connection_url:
            session.add(
                DataSourceConnection(workspace_id=workspace.id, connection_url=connection_url)
            )
        else:
            session.add(
                DataSourceConnection(workspace_id=workspace.id, encrypted_connection_url=None)
            )
        session.add(
            ConsoleCredential(
                workspace_id=workspace.id,
                username=console_username,
                password_hash=hash_password(console_password),
            )
        )

        api_key = generate_api_key()
        session.add(
            ApiKey(
                workspace_id=workspace.id,
                key_hash=hash_api_key(api_key),
                status="active",
                created_at=datetime.datetime.now(datetime.UTC),
            )
        )

        await session.commit()
        return api_key


def _non_negative_int(value: str) -> int:
    """argparse type: refuse a negative limit here, with a clear message,
    instead of as a database constraint error after the password prompt."""
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return number


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True, help="Workspace name")
    parser.add_argument(
        "--connection-url",
        help="Target database connection string. Omit to use the server's default "
        "(APP_TARGET_DATABASE_URL).",
    )
    parser.add_argument("--console-username", required=True)
    parser.add_argument(
        "--query-rate-limit",
        type=_non_negative_int,
        help="run_query calls per hour for this workspace only. Omit for the server "
        "default (APP_DEMO_RATE_LIMIT).",
    )
    parser.add_argument(
        "--sample-rate-limit",
        type=_non_negative_int,
        help="sample_rows calls per hour for this workspace only. Omit for the server "
        "default (APP_SAMPLE_RATE_LIMIT).",
    )
    args = parser.parse_args()

    # getpass hides what you type, like a normal password prompt --
    # safer than putting the password directly on the command line,
    # where it could end up saved in your shell's history.
    console_password = getpass.getpass("Console password: ")

    api_key = asyncio.run(
        seed(
            args.name,
            args.connection_url,
            args.console_username,
            console_password,
            args.query_rate_limit,
            args.sample_rate_limit,
        )
    )
    print("Workspace created.")
    print(f"API key (shown once, store it now): {api_key}")


if __name__ == "__main__":
    main()
