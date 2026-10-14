"""Login handling for the web console. This is a SEPARATE credential
system from the API key -- see app/auth/models.py's ConsoleCredential --
specifically so revoking a workspace's API key never locks the operator
out of viewing that workspace's own audit history."""

import secrets

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from app.auth.models import ConsoleCredential, Workspace
from app.auth.security import hash_password, verify_password

# A real-looking but fake stored hash, used only so that checking a
# username that doesn't exist takes the same amount of time as checking
# one that does -- without this, a login attempt could be used to
# discover which usernames exist just by measuring how long the
# response takes (skipping the slow password hash entirely for an
# unknown username would be a visible, fast-vs-slow difference).
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
    return await session.get(Workspace, credential.workspace_id)


def log_in(request: Request, workspace: Workspace) -> None:
    """Remembers which workspace this browser is logged in as, using a
    signed cookie (Starlette's SessionMiddleware) -- not a database
    table, since a console login session is exactly the kind of small,
    short-lived state a cookie is meant for."""
    request.session["workspace_id"] = str(workspace.id)


def log_out(request: Request) -> None:
    request.session.clear()


async def get_logged_in_workspace(request: Request, session: AsyncSession) -> Workspace | None:
    """Looks up whichever workspace (if any) the current browser session
    is logged in as. Returns None if there's no session, or if the
    session refers to a workspace that's since been deleted."""
    workspace_id = request.session.get("workspace_id")
    if workspace_id is None:
        return None
    return await session.get(Workspace, workspace_id)
