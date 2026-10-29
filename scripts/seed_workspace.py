"""Operator-only CLI to create a workspace, its data source connection,
console login, and first API key, without an invite. (Invited users sign
up themselves at /console/signup; see app/auth/signup.py.)"""

import argparse
import asyncio
import datetime
import getpass

from app.auth.workspaces import UsernameTaken, WorkspaceSpec, create_workspace
from app.db import SessionLocal
from app.query.network_guard import ensure_public_target


async def seed(spec: WorkspaceSpec) -> str:
    """Creates a workspace with everything it needs (app/auth/workspaces.py)
    and returns its API key in plain text -- the ONLY time it ever exists
    in plain text, since only its hash gets stored.

    The spec's two rate limits are optional per-hour allowances for this
    workspace alone; None keeps the server-wide defaults. This script is
    the only place they can be set, on purpose (see Workspace in
    app/auth/models.py)."""
    # Same address check a self-serve signup gets: the operator can mistype
    # too. No URL means the server's default database, which is exempt.
    if spec.connection_url:
        await ensure_public_target(spec.connection_url)
    async with SessionLocal() as session:
        try:
            _, api_key = await create_workspace(session, spec, datetime.datetime.now(datetime.UTC))
        except UsernameTaken as exc:
            raise SystemExit(f"console username '{exc}' is already taken") from exc
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

    spec = WorkspaceSpec(
        name=args.name,
        console_username=args.console_username,
        console_password=console_password,
        created_via="operator",
        connection_url=args.connection_url,
        query_rate_limit=args.query_rate_limit,
        sample_rate_limit=args.sample_rate_limit,
    )
    api_key = asyncio.run(seed(spec))
    print("Workspace created.")
    print(f"API key (shown once, store it now): {api_key}")


if __name__ == "__main__":
    main()
