"""Operator-only CLI to revoke a workspace's active API key.

Revocation blocks query execution only -- the workspace's ConsoleCredential
is unaffected, so revoking a compromised key doesn't lock the operator out
of viewing its audit history (Phase 3)."""

import argparse
import asyncio
import datetime

from sqlalchemy import select

from app.auth.models import ApiKey, Workspace
from app.db import SessionLocal


async def revoke(workspace_name: str) -> None:
    """Finds the given workspace's currently-active key and flips it to
    "revoked" -- from that moment on, authenticate() in
    app/auth/service.py will refuse it immediately."""
    async with SessionLocal() as session:
        workspace = (
            await session.execute(select(Workspace).where(Workspace.name == workspace_name))
        ).scalar_one_or_none()
        if workspace is None:
            raise SystemExit(f"no workspace named '{workspace_name}'")

        api_key = (
            await session.execute(
                select(ApiKey).where(ApiKey.workspace_id == workspace.id, ApiKey.status == "active")
            )
        ).scalar_one_or_none()
        if api_key is None:
            raise SystemExit(f"workspace '{workspace_name}' has no active API key")

        api_key.status = "revoked"
        api_key.revoked_at = datetime.datetime.now(datetime.UTC)
        await session.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-name", required=True)
    args = parser.parse_args()

    asyncio.run(revoke(args.workspace_name))
    print(f"API key for workspace '{args.workspace_name}' revoked.")


if __name__ == "__main__":
    main()
