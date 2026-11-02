"""Creates a workspace with everything it needs to be used: which
database it queries, a console login, and its first API key. Shared by
the console's self-serve signup (app/auth/signup.py) and the operator's
scripts/seed_workspace.py, so both build a workspace the same way.
scripts/remove_connection.py uses the two functions that undo it.

Checking a user-supplied connection URL is the caller's job (signup
connects to it first; the seed script runs the address guard)."""

import datetime
from dataclasses import dataclass

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import ApiKey, ConsoleCredential, InviteCode, RateLimitCounter, Workspace
from app.auth.security import generate_api_key, hash_api_key, hash_password
from app.query.models import DataSourceConnection, QueryExecution, SchemaCache

# Every table holding rows that belong to a workspace, in a safe order to
# delete from: query_executions also points at data_source_connections.
_WORKSPACE_MODELS = (
    QueryExecution,
    SchemaCache,
    RateLimitCounter,
    ApiKey,
    ConsoleCredential,
    DataSourceConnection,
)


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


async def count_workspace_rows(session: AsyncSession, workspace: Workspace) -> dict[str, int]:
    """How many rows each table holds for this workspace, by table name."""
    counts: dict[str, int] = {}
    for model in _WORKSPACE_MODELS:
        result = await session.execute(
            select(func.count()).select_from(model).where(model.workspace_id == workspace.id)
        )
        counts[model.__tablename__] = result.scalar_one()
    return counts


async def use_default_database(session: AsyncSession, workspace: Workspace) -> None:
    """Forgets the workspace's stored database URL, without reading it, so
    it also works on a value that no longer decrypts. A NULL URL means the
    server's default database, not "no access": the workspace's key and
    login keep working there. Its schema cache goes too, since that
    describes the old database."""
    await session.execute(
        update(DataSourceConnection)
        .where(DataSourceConnection.workspace_id == workspace.id)
        .values(encrypted_connection_url=None)
    )
    await session.execute(delete(SchemaCache).where(SchemaCache.workspace_id == workspace.id))
    await session.commit()


async def remove_workspace(session: AsyncSession, workspace: Workspace) -> None:
    """Deletes the workspace and everything it owns, its audit log included,
    in one transaction. An invite it used stays spent: used_at is kept, so
    the code can't be redeemed again."""
    await session.execute(
        update(InviteCode)
        .where(InviteCode.used_by_workspace_id == workspace.id)
        .values(used_by_workspace_id=None)
    )
    for model in _WORKSPACE_MODELS:
        await session.execute(delete(model).where(model.workspace_id == workspace.id))
    await session.delete(workspace)
    await session.commit()
