"""Operator-only CLI to create a workspace, its data source connection,
console login, and first API key. See ADR-004: connection strings are never
set through the console or any client, only here."""

import argparse
import asyncio
import datetime
import getpass

from app.auth.models import ApiKey, ConsoleCredential, Workspace
from app.auth.security import generate_api_key, hash_api_key, hash_password
from app.db import SessionLocal
from app.query.models import DataSourceConnection


async def seed(name: str, connection_url: str, console_username: str, console_password: str) -> str:
    """Creates everything a brand-new workspace needs in one go: the
    workspace itself, which real database it's allowed to query, a
    dashboard login, and its first API key. Returns the API key in
    plain text -- the ONLY time it ever exists in plain text, since
    only its hash gets stored (see app/auth/security.py)."""
    async with SessionLocal() as session:
        workspace = Workspace(name=name, created_at=datetime.datetime.now(datetime.UTC))
        session.add(workspace)
        await session.flush()  # assigns workspace.id before it's used below

        session.add(
            DataSourceConnection(workspace_id=workspace.id, connection_url=connection_url)
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True, help="Workspace name")
    parser.add_argument("--connection-url", required=True, help="Target database connection string")
    parser.add_argument("--console-username", required=True)
    args = parser.parse_args()

    # getpass hides what you type, like a normal password prompt --
    # safer than putting the password directly on the command line,
    # where it could end up saved in your shell's history.
    console_password = getpass.getpass("Console password: ")

    api_key = asyncio.run(
        seed(args.name, args.connection_url, args.console_username, console_password)
    )
    print("Workspace created.")
    print(f"API key (shown once, store it now): {api_key}")


if __name__ == "__main__":
    main()
