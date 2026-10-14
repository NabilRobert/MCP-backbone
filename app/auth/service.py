from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import ApiKey, Workspace
from app.auth.security import hash_api_key


class AuthenticationError(Exception):
    """Raised whenever a request's API key doesn't check out -- missing,
    wrong, or revoked. Every MCP tool catches this and turns it into a
    clean error message for the AI model (see app/mcp/server.py)."""


async def authenticate(session: AsyncSession, api_key: str) -> Workspace:
    """The front gate every tool call passes through. Takes whatever
    plain-text key the caller sent, hashes it the same way it was
    hashed when created, and looks for a matching, still-active row.
    If that's found, returns the Workspace it belongs to -- everything
    downstream of this function trusts that the caller is legitimately
    acting as that workspace."""
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
    return workspace
