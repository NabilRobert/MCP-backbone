"""Operator-only CLI to get rid of a workspace's stored database URL, for
example one that no longer decrypts after APP_CONNECTION_ENCRYPTION_KEY
was rotated. Never reads or prints the URL. Changes nothing without
--apply, so run it once first to see what it would do.

Choose one:
- --use-default-database forgets the URL. The workspace then queries the
  server's default database: it isn't cut off, and its key and login keep
  working. scripts/revoke_key.py stops the key.
- --remove-workspace deletes the workspace and everything it owns, audit
  log included. scripts/seed_workspace.py makes a new one; the old audit
  log only comes back from a backup (deploy/RUNBOOK.md).

An operator script, not a console button, for the same reason as the
per-workspace rate limits: it's rare, and it shouldn't sit behind a web
login."""

import argparse
import asyncio

from sqlalchemy import select

from app.auth.models import Workspace
from app.auth.workspaces import count_workspace_rows, remove_workspace, use_default_database
from app.db import SessionLocal
from app.query.models import DataSourceConnection

AFTER_DEFAULT = (
    "It then queries the server's default database. Its key and login keep working; "
    "scripts/revoke_key.py stops the key."
)
AFTER_REMOVAL = "Its audit log goes with it; only a backup brings that back."


async def clean_up(workspace_name: str, should_remove_workspace: bool, apply: bool) -> list[str]:
    """Does what the options ask for, or only describes it without `apply`.
    Returns the lines to print."""
    async with SessionLocal() as session:
        result = await session.execute(select(Workspace).where(Workspace.name == workspace_name))
        matches = result.scalars().all()
        if len(matches) != 1:
            raise SystemExit(
                f"expected one workspace named '{workspace_name}', found {len(matches)}"
            )
        workspace = matches[0]

        stored_urls = await session.execute(
            select(DataSourceConnection.encrypted_connection_url).where(
                DataSourceConnection.workspace_id == workspace.id
            )
        )
        has_stored_url = stored_urls.scalar_one_or_none() is not None
        lines = [
            f"Workspace '{workspace_name}': stored URL {'present' if has_stored_url else 'none'}"
        ]
        counts = await count_workspace_rows(session, workspace)
        lines += [f"  {table}: {count}" for table, count in counts.items()]

        if not should_remove_workspace and not has_stored_url:
            return [*lines, "Nothing to do: it already uses the default database."]

        if should_remove_workspace:
            plan, consequence = "remove the workspace and all of these rows", AFTER_REMOVAL
        else:
            plan, consequence = "forget the stored URL", AFTER_DEFAULT
        if not apply:
            return [
                *lines,
                f"Would {plan}. {consequence}",
                "Nothing changed: add --apply to do it.",
            ]

        if should_remove_workspace:
            await remove_workspace(session, workspace)
        else:
            await use_default_database(session, workspace)
        return [*lines, f"Done: {plan}. {consequence}"]


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--workspace-name", required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--use-default-database", action="store_true")
    action.add_argument("--remove-workspace", action="store_true")
    parser.add_argument("--apply", action="store_true", help="Without this, nothing changes.")
    args = parser.parse_args()

    for line in asyncio.run(clean_up(args.workspace_name, args.remove_workspace, args.apply)):
        print(line)


if __name__ == "__main__":
    main()
