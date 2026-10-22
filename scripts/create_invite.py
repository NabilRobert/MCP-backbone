"""Operator-only CLI to issue a single-use invite code. Whoever you give
it to can sign up at /console/signup for their own workspace."""

import argparse
import asyncio

from app.auth.signup import create_invite
from app.db import SessionLocal


async def issue(note: str, expires_in_days: int | None) -> str:
    async with SessionLocal() as session:
        return await create_invite(session, note, expires_in_days)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--note", required=True, help="Who this invite is for (kept for your records)"
    )
    parser.add_argument(
        "--expires-in-days",
        type=int,
        default=14,
        help="Days until the code expires (default 14; 0 = never)",
    )
    args = parser.parse_args()

    code = asyncio.run(issue(args.note, args.expires_in_days or None))
    print(f"Invite code (shown once, single use): {code}")


if __name__ == "__main__":
    main()
