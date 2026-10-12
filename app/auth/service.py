from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import ApiKey, Workspace
from app.auth.security import hash_api_key


class AuthenticationError(Exception):
    pass


async def authenticate(session: AsyncSession, api_key: str) -> Workspace:
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
