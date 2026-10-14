"""Operator-only CLI to revoke a workspace's active API key.

Revocation blocks query execution only -- the workspace's ConsoleCredential
is unaffected, so revoking a compromised key doesn't lock the operator out
of viewing its audit history (Phase 3)."""

import argparse
import asyncio

from sqlalchemy import select

from app.auth.models import Workspace
from app.auth.service import get_active_api_key, revoke_api_key
from app.db import SessionLocal


async def revoke(workspace_name: str) -> None:
    """Finds the given workspace and revokes its currently-active key,
    via the same revoke_api_key() the web console's key-management page
    uses -- one shared implementation, not two that could drift apart."""
    async with SessionLocal() as session:
        workspace = (
            await session.execute(select(Workspace).where(Workspace.name == workspace_name))
        ).scalar_one_or_none()
        if workspace is None:
            raise SystemExit(f"no workspace named '{workspace_name}'")

        if await get_active_api_key(session, workspace) is None:
            raise SystemExit(f"workspace '{workspace_name}' has no active API key")

        await revoke_api_key(session, workspace)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-name", required=True)
    args = parser.parse_args()

    asyncio.run(revoke(args.workspace_name))
    print(f"API key for workspace '{args.workspace_name}' revoked.")


if __name__ == "__main__":
    main()
