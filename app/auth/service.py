import datetime
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import ApiKey, Workspace
from app.auth.scopes import ALL_TOOLS
from app.auth.security import generate_api_key, hash_api_key


class AuthenticationError(Exception):
    """Raised whenever a request's API key doesn't check out -- missing,
    wrong, or revoked. Every MCP tool catches this and turns it into a
    clean error message for the AI model (see app/mcp/server.py)."""


class ToolNotPermitted(AuthenticationError):
    """The key is valid, but not allowed to call this tool (its scopes,
    app/auth/scopes.py). Carries the workspace so the refusal can still
    be written to its audit log."""

    def __init__(self, workspace: Workspace, tool: str) -> None:
        self.workspace = workspace
        self.tool = tool
        super().__init__(
            f"this API key isn't allowed to use {tool}; "
            "issue a key with more access from the console's Keys page"
        )


async def authenticate(session: AsyncSession, api_key: str, tool: str | None = None) -> Workspace:
    """The front gate every tool call passes through. Takes whatever
    plain-text key the caller sent, hashes it the same way it was
    hashed when created, and looks for a matching, still-active row.
    If that's found, returns the Workspace it belongs to -- everything
    downstream of this function trusts that the caller is legitimately
    acting as that workspace. With `tool`, also refuses (ToolNotPermitted)
    unless the key's scopes include it."""
    key_hash = hash_api_key(api_key)
    result = await session.execute(
        select(ApiKey).where(ApiKey.key_hash == key_hash, ApiKey.status == "active")
    )
    api_key_row = result.scalar_one_or_none()
    if api_key_row is None:
        raise AuthenticationError("invalid or revoked API key")

    workspace = await session.get(Workspace, api_key_row.workspace_id)
    if workspace is None:
        raise AuthenticationError("workspace not found")
    if tool is not None and tool not in api_key_row.scopes:
        raise ToolNotPermitted(workspace, tool)
    return workspace


async def get_active_api_key(session: AsyncSession, workspace: Workspace) -> ApiKey | None:
    """Looks up a workspace's currently-active key row (not the plain
    text -- that's never stored). Used by the console to show its
    status (created when, revoked when) without exposing the key
    itself."""
    result = await session.execute(
        select(ApiKey).where(ApiKey.workspace_id == workspace.id, ApiKey.status == "active")
    )
    return result.scalar_one_or_none()


async def revoke_api_key(session: AsyncSession, workspace: Workspace) -> None:
    """Shared by scripts/revoke_key.py and the console's key-management
    page, so both go through the exact same logic instead of two
    separate reimplementations drifting apart over time."""
    api_key = await get_active_api_key(session, workspace)
    if api_key is None:
        return
    api_key.status = "revoked"
    api_key.revoked_at = datetime.datetime.now(datetime.UTC)
    await session.commit()


async def rotate_api_key(
    session: AsyncSession, workspace: Workspace, scopes: Iterable[str] = ALL_TOOLS
) -> str:
    """Revokes whatever key is currently active (if any) and issues a
    brand-new one. Returns the new key in plain text -- the only time
    it ever exists that way, same as when a workspace is first created
    (see scripts/seed_workspace.py)."""
    await revoke_api_key(session, workspace)

    new_key = generate_api_key()
    session.add(
        ApiKey(
            workspace_id=workspace.id,
            key_hash=hash_api_key(new_key),
            status="active",
            created_at=datetime.datetime.now(datetime.UTC),
            scopes=sorted(scopes),
        )
    )
    await session.commit()
    return new_key
