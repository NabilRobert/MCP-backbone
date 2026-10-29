"""Self-serve signup: each person gets their own workspace -- a console
login, an API key, and a target database. The console's /console/signup
page calls sign_up() below.

Two tiers, gated by risk:
- No invite code: a demo account on the server's default database
  (APP_TARGET_DATABASE_URL, the shared demo dataset), capped per IP and
  per day.
- An invite code (scripts/create_invite.py): may also connect the user's
  own database. That URL is checked by app/query/network_guard.py and
  actually connected to before anything is saved."""

import asyncio
import datetime
import re
import secrets
from dataclasses import dataclass, field

from pydantic import BaseModel
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import InviteCode, Workspace
from app.auth.security import hash_api_key
from app.auth.workspaces import UsernameTaken, WorkspaceSpec, create_workspace
from app.config import settings
from app.console import throttle
from app.db import create_target_engine
from app.query.network_guard import UnsafeTargetError, ensure_public_target

MIN_PASSWORD_LENGTH = 12
CONNECT_TIMEOUT_SECONDS = 10
_USERNAME = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")


class SignupForm(BaseModel):
    """What the console's signup form sends (app/console/routes.py reads
    it straight from the form fields). Blank invite code: a demo account."""

    username: str
    password: str
    invite_code: str = ""
    connection_url: str = ""


class SignupError(Exception):
    """A signup attempt was refused. The message is safe to show the user."""


@dataclass
class SignupResult:
    workspace: Workspace
    api_key: str  # plain text -- the only time it ever exists that way
    warnings: list[str] = field(default_factory=list[str])


def generate_invite_code() -> str:
    """128 random bits: far beyond guessing, and short enough to paste."""
    return f"inv_{secrets.token_urlsafe(16)}"


async def create_invite(session: AsyncSession, note: str, expires_in_days: int | None = 14) -> str:
    """Issues one invite code and returns it in plain text. Only its
    hash is stored, the same way API keys are (a random code can't be
    brute-forced, so a fast hash is enough)."""
    code = generate_invite_code()
    now = datetime.datetime.now(datetime.UTC)
    session.add(
        InviteCode(
            code_hash=hash_api_key(code),
            note=note,
            created_at=now,
            expires_at=(
                now + datetime.timedelta(days=expires_in_days) if expires_in_days else None
            ),
        )
    )
    await session.commit()
    return code


async def _check_target(connection_url: str) -> list[str]:
    """Connects to a user-supplied database once, so a typo fails at
    signup instead of on the first query, and reports (as warnings, not
    errors -- it's their database) whether the account can write. This
    server never writes, but a read-only account is the second line of
    defence behind its own query checks."""
    try:
        await ensure_public_target(connection_url)
    except UnsafeTargetError as exc:
        raise SignupError(str(exc)) from exc

    engine = create_target_engine(connection_url)
    try:
        async with asyncio.timeout(CONNECT_TIMEOUT_SECONDS):
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
                is_superuser = (
                    await conn.execute(
                        text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
                    )
                ).scalar()
                can_write = (
                    await conn.execute(
                        text(
                            "SELECT has_database_privilege(current_database(), 'CREATE') "
                            "OR EXISTS (SELECT 1 FROM information_schema.role_table_grants "
                            "WHERE grantee = current_user AND privilege_type IN "
                            "('INSERT', 'UPDATE', 'DELETE', 'TRUNCATE'))"
                        )
                    )
                ).scalar()
    except TimeoutError as exc:
        raise SignupError("could not connect to that database (timed out)") from exc
    except Exception as exc:  # any driver error means "can't connect"; say why
        raise SignupError(f"could not connect to that database: {exc}") from exc
    finally:
        await engine.dispose()

    if is_superuser:
        return ["That login is a superuser. Use a read-only login for this server instead."]
    if can_write:
        return ["That login can write to the database. A read-only login is safer."]
    return []


async def _check_demo_capacity(
    session: AsyncSession, client_ip: str, now: datetime.datetime
) -> None:
    """Raises SignupError if demo signups are off, or this network or the
    whole server has used up today's demo accounts."""
    if not settings.demo_signups_enabled:
        raise SignupError("Signup currently needs an invite code.")
    if await throttle.is_blocked(
        session,
        throttle.DEMO_SIGNUP,
        client_ip,
        settings.demo_signups_per_ip_per_day,
        24 * 60 * 60,
    ):
        raise SignupError("Too many demo accounts from your network today. Try again tomorrow.")
    created_today = (
        await session.execute(
            select(func.count(Workspace.id)).where(
                Workspace.created_via == "demo",
                Workspace.created_at > now - datetime.timedelta(days=1),
            )
        )
    ).scalar_one()
    if created_today >= settings.demo_signups_per_day:
        raise SignupError(
            "The demo is full for today. Try again tomorrow, or ask for an invite code."
        )


async def _sign_up_demo(
    session: AsyncSession, spec: WorkspaceSpec, client_ip: str, now: datetime.datetime
) -> SignupResult:
    """No invite code: a demo account on the server's default database.
    Open to anyone, because the risky part of signup -- making this
    server connect to a host the user picked, and storing their database
    password -- isn't available here. What's left to guard against is
    someone mass-creating accounts to multiply the per-workspace rate
    limits, so demo signups are capped per client IP and in total, per
    day."""
    await _check_demo_capacity(session, client_ip, now)
    try:
        workspace, api_key = await create_workspace(session, spec, now)
        await session.commit()
    except (IntegrityError, UsernameTaken) as exc:
        await session.rollback()
        raise SignupError("That username is taken.") from exc
    except BaseException:
        await session.rollback()
        raise

    await throttle.record(session, throttle.DEMO_SIGNUP, client_ip)
    return SignupResult(workspace=workspace, api_key=api_key)


async def _sign_up_invited(
    session: AsyncSession, invite_code: str, spec: WorkspaceSpec, now: datetime.datetime
) -> SignupResult:
    """With an invite code, the user may also connect their own database.
    Everything happens in one transaction, so if any step fails nothing
    is created and the invite stays unused. The invite is checked FIRST,
    before touching the user's connection URL -- otherwise anyone,
    invited or not, could make this server open connections to hosts of
    their choosing."""
    # Claims the invite atomically: the row lock taken by this UPDATE
    # holds until commit, so two simultaneous signups with the same code
    # can't both succeed -- the second one finds used_at already set.
    claimed = await session.execute(
        update(InviteCode)
        .where(
            InviteCode.code_hash == hash_api_key(invite_code),
            InviteCode.used_at.is_(None),
            (InviteCode.expires_at.is_(None)) | (InviteCode.expires_at > now),
        )
        .values(used_at=now)
        .returning(InviteCode.id)
    )
    invite_id = claimed.scalar_one_or_none()
    if invite_id is None:
        await session.rollback()
        raise SignupError("That invite code is invalid, expired, or already used.")

    try:
        warnings = await _check_target(spec.connection_url) if spec.connection_url else []
        workspace, api_key = await create_workspace(session, spec, now)
        await session.execute(
            update(InviteCode)
            .where(InviteCode.id == invite_id)
            .values(used_by_workspace_id=workspace.id)
        )
        await session.commit()
    except (IntegrityError, UsernameTaken) as exc:
        # The username check can lose a race with a simultaneous signup;
        # the unique constraint is the real guarantee.
        await session.rollback()
        raise SignupError("That username is taken.") from exc
    except BaseException:
        await session.rollback()
        raise

    return SignupResult(workspace=workspace, api_key=api_key, warnings=warnings)


async def sign_up(
    session: AsyncSession, form: SignupForm, client_ip: str = "unknown"
) -> SignupResult:
    """Creates a workspace from the console's signup form. Without an
    invite code, that's a demo account on the default database
    (_sign_up_demo above). With one, the user may also connect their own
    database (_sign_up_invited)."""
    username = form.username.strip()
    invite_code = form.invite_code.strip()
    connection_url = form.connection_url.strip()
    if not _USERNAME.match(username):
        raise SignupError("Username must be 3-32 letters, digits, '.', '_' or '-'.")
    if len(form.password) < MIN_PASSWORD_LENGTH:
        raise SignupError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")

    now = datetime.datetime.now(datetime.UTC)
    spec = WorkspaceSpec(
        name=username,
        console_username=username,
        console_password=form.password,
        created_via="invite" if invite_code else "demo",
        connection_url=connection_url or None,
    )
    if invite_code:
        return await _sign_up_invited(session, invite_code, spec, now)
    if connection_url:
        raise SignupError(
            "Connecting your own database needs an invite code. "
            "Leave the database field blank to use the demo database."
        )
    return await _sign_up_demo(session, spec, client_ip, now)
