"""Operator-only CLI to set a new console password: for a login that can't
get in, or one still on the old scrypt hash that nobody logs in with (only
a successful login upgrades a hash). Every existing console session for
that login is logged out.

The password is asked for twice and never taken as an argument, so it
can't end up in shell history.

    .venv/bin/python -m scripts.reset_console_password --username eval"""

import argparse
import asyncio
import getpass
from collections.abc import Callable

from app.console.auth import ResetRefused, reset_console_password
from app.db import SessionLocal


def read_new_password(prompt: Callable[[str], str] = getpass.getpass) -> str:
    """Asks twice, and stops if the two don't match."""
    password = prompt("New console password: ")
    if prompt("Same password again: ") != password:
        raise SystemExit("the two passwords don't match; nothing changed")
    return password


async def reset(username: str, new_password: str) -> None:
    async with SessionLocal() as session:
        try:
            await reset_console_password(session, username, new_password)
        except ResetRefused as exc:
            raise SystemExit(f"{exc}; nothing changed") from exc


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--username", required=True, help="The console login to reset")
    args = parser.parse_args()

    asyncio.run(reset(args.username, read_new_password()))
    print(f"Console password reset for '{args.username}'. Its existing sessions are logged out.")


if __name__ == "__main__":
    main()
