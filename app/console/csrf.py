"""CSRF protection for the console's forms, a second layer behind the
session cookie's same_site="strict" (app/main.py). SameSite alone
depends on every browser honouring it, and it doesn't stop a request
from a sibling subdomain, which counts as "same site".

The synchronizer-token pattern: a random token kept in the signed
session cookie, repeated in a hidden field of every console form, and
compared on every POST. Another site can make a browser send the
cookie, but can't read the page to learn the token.

Hand-written rather than a library: starlette-csrf only reads the token
from a header (these are plain HTML forms), and fastapi-csrf-protect
adds a second signed cookie next to the session that already exists."""

import secrets

from fastapi import HTTPException, Request

_SESSION_KEY = "csrf_token"
_FORM_FIELD = "csrf_token"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def csrf_token(request: Request) -> str:
    """This session's token, made on first use. Templates call it to
    fill each form's hidden field; making it also starts the session,
    so the login and signup forms get one before anyone logs in."""
    token = request.session.get(_SESSION_KEY)
    if not isinstance(token, str):
        token = secrets.token_urlsafe(32)
        request.session[_SESSION_KEY] = token
    return token


def rotate_csrf_token(request: Request) -> None:
    """Drops the token at login, so one learned before login (say, by
    someone at the same browser) is useless after it. The next page
    rendered makes a new one."""
    request.session.pop(_SESSION_KEY, None)


async def verify_csrf(request: Request) -> None:
    """A router-wide dependency: every console POST must carry the
    session's token, so a new form can't forget the check. A missing
    session token (a tab left open across a logout or a server restart)
    fails the same way as a forged one."""
    if request.method in _SAFE_METHODS:
        return
    expected = request.session.get(_SESSION_KEY)
    submitted = (await request.form()).get(_FORM_FIELD)
    if (
        not isinstance(expected, str)
        or not isinstance(submitted, str)
        or not secrets.compare_digest(expected, submitted)
    ):
        raise HTTPException(
            status_code=403, detail="This form has expired. Reload the page and try again."
        )
