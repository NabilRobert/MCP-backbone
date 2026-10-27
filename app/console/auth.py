"""Login handling for the web console. This is a SEPARATE credential
system from the API key -- see app/auth/models.py's ConsoleCredential --
specifically so revoking a workspace's API key never locks the operator
out of viewing that workspace's own audit history."""

import secrets

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from app.auth.models import ConsoleCredential, Workspace
from app.auth.security import hash_password, needs_rehash, verify_password
from app.console.csrf import rotate_csrf_token

# A real-looking but fake stored hash, used only so that checking a
# username that doesn't exist takes the same amount of time as checking
# one that does -- without this, a login attempt could be used to
# discover which usernames exist just by measuring how long the
# response takes (skipping the slow password hash entirely for an
# unknown username would be a visible, fast-vs-slow difference).
# Known gap: a login still on a legacy scrypt hash is checked a few
# milliseconds faster than this Argon2 dummy, until its next successful
# login upgrades it; the per-IP login throttle limits how many timings
# anyone can collect.
_DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(16))


async def verify_console_login(
    session: AsyncSession, username: str, password: str
) -> Workspace | None:
    """Checks a submitted username/password against the stored
    ConsoleCredential. Returns the matching Workspace on success, or
    None if the username doesn't exist or the password is wrong."""
    result = await session.execute(
        select(ConsoleCredential).where(ConsoleCredential.username == username)
    )
    credential = result.scalar_one_or_none()

    if credential is None:
        verify_password(password, _DUMMY_PASSWORD_HASH)  # always do the slow check
        return None
    if not verify_password(password, credential.password_hash):
        return None
    if needs_rehash(credential.password_hash):
        # The only moment the plain-text password is available, so the
        # only moment an old hash can be upgraded to the current scheme.
        credential.password_hash = hash_password(password)
        await session.commit()
    return await session.get(Workspace, credential.workspace_id)


async def _credential_for(session: AsyncSession, workspace_id: object) -> ConsoleCredential | None:
    result = await session.execute(
        select(ConsoleCredential).where(ConsoleCredential.workspace_id == workspace_id)
    )
    return result.scalar_one_or_none()


async def log_in(request: Request, session: AsyncSession, workspace: Workspace) -> None:
    """Remembers which workspace this browser is logged in as, in a
    signed cookie (Starlette's SessionMiddleware), stamped with the
    login's current session_version so it can be revoked later."""
    credential = await _credential_for(session, workspace.id)
    rotate_csrf_token(request)
    request.session["workspace_id"] = str(workspace.id)
    request.session["session_version"] = credential.session_version if credential else 0


def log_out(request: Request) -> None:
    request.session.clear()


async def get_logged_in_workspace(request: Request, session: AsyncSession) -> Workspace | None:
    """Looks up whichever workspace (if any) the current browser session
    is logged in as. Returns None if there's no session, or if the
    session refers to a workspace that's since been deleted."""
    workspace_id = request.session.get("workspace_id")
    if workspace_id is None:
        return None
    credential = await _credential_for(session, workspace_id)
    if credential is None or request.session.get("session_version") != credential.session_version:
        # Revoked by "log out everywhere" (or the login no longer exists).
        request.session.clear()
        return None
    return await session.get(Workspace, workspace_id)


async def revoke_all_sessions(session: AsyncSession, workspace: Workspace) -> None:
    """Invalidates every console session for this workspace, on every
    browser, including the current one."""
    credential = await _credential_for(session, workspace.id)
    if credential is not None:
        credential.session_version += 1
        await session.commit()
