"""Creates a workspace with everything it needs to be used: which
database it queries, a console login, and its first API key. Shared by
the console's self-serve signup (app/auth/signup.py) and the operator's
scripts/seed_workspace.py, so both build a workspace the same way.

Checking a user-supplied connection URL is the caller's job (signup
connects to it first; the seed script runs the address guard)."""

import datetime
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import ApiKey, ConsoleCredential, Workspace
from app.auth.security import generate_api_key, hash_api_key, hash_password
from app.query.models import DataSourceConnection


class UsernameTaken(Exception):
    """Another workspace already has this console username."""


@dataclass(frozen=True)
class WorkspaceSpec:
    """What a new workspace is made from."""

    name: str
    console_username: str
    console_password: str
    # "operator" (scripts/seed_workspace.py), "invite" or "demo".
    created_via: str
    # None: the server's default database (APP_TARGET_DATABASE_URL).
    connection_url: str | None = None
    # Per-hour allowances for this workspace alone; None keeps the server
    # defaults. Only the operator sets these (see Workspace).
    query_rate_limit: int | None = None
    sample_rate_limit: int | None = None


async def create_workspace(
    session: AsyncSession, spec: WorkspaceSpec, now: datetime.datetime
) -> tuple[Workspace, str]:
    """Adds the workspace, its database, console login and first API key
    to the current transaction (the caller commits). Returns the
    workspace and the API key in plain text: the only time it ever
    exists that way, since only its hash is stored."""
    taken = await session.execute(
        select(ConsoleCredential.id).where(ConsoleCredential.username == spec.console_username)
    )
    if taken.first() is not None:
        raise UsernameTaken(spec.console_username)

    workspace = Workspace(
        name=spec.name,
        created_at=now,
        created_via=spec.created_via,
        query_rate_limit=spec.query_rate_limit,
        sample_rate_limit=spec.sample_rate_limit,
    )
    session.add(workspace)
    await session.flush()  # assigns workspace.id before it's used below

    # None = the server's default database, resolved from settings on
    # every use (see DataSourceConnection), never copied in here.
    if spec.connection_url is None:
        session.add(DataSourceConnection(workspace_id=workspace.id, encrypted_connection_url=None))
    else:
        session.add(
            DataSourceConnection(workspace_id=workspace.id, connection_url=spec.connection_url)
        )
    session.add(
        ConsoleCredential(
            workspace_id=workspace.id,
            username=spec.console_username,
            password_hash=hash_password(spec.console_password),
        )
    )
    api_key = generate_api_key()
    session.add(
        ApiKey(
            workspace_id=workspace.id,
            key_hash=hash_api_key(api_key),
            status="active",
            created_at=now,
        )
    )
    return workspace, api_key
