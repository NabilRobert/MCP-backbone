"""Refuses a target-database connection URL that points anywhere it
shouldn't. This matters because users can register their own
connection URL at signup (app/auth/signup.py): without this check, one
pointing at 127.0.0.1:5432 would reach this server's OWN bookkeeping
database (audit log, key hashes), since that only listens on the
server's loopback interface -- exactly where this server connects from.
The same goes for anything else on the server's private network.

This is the validation ADR-004 said would be needed before connection
strings could ever be set by anyone but the operator.

Checked twice: when a URL is registered, and again every time it's
used (app/query/service.py's get_data_source), since DNS can change
after registration.

Known gap, stated plainly: the address is checked here, then resolved
again by the database driver when it actually connects. A hostname
whose DNS flips between those two lookups (DNS rebinding) could slip
through. Closing it means connecting to the checked IP directly, which
breaks TLS hostname verification for sslmode=verify-full; for now the
invite-only signup (only people the operator chose can register a URL)
is what bounds that risk."""

import asyncio
import ipaddress
import socket

from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import ArgumentError

from app.config import settings

# URL query parameters that only tune TLS. Everything else is refused:
# the asyncpg dialect also accepts e.g. ?host=... (extra or multiple
# hosts), which would let the real destination bypass the host check
# below entirely.
_ALLOWED_QUERY_PARAMS = {"ssl", "sslmode"}


class UnsafeTargetError(Exception):
    """The connection URL is malformed or points somewhere this server
    refuses to connect to."""


def _is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        # "::ffff:127.0.0.1" is loopback in disguise -- judge the IPv4 inside.
        ip = ip.ipv4_mapped
    # is_global is False for loopback, private (10/8, 172.16/12,
    # 192.168/16), link-local (169.254/16, incl. cloud metadata
    # endpoints), carrier-grade NAT (100.64/10), and reserved ranges.
    return ip.is_global and not ip.is_multicast


async def ensure_public_target(connection_url: str) -> None:
    """Raises UnsafeTargetError unless connection_url is a well-formed
    Postgres URL whose host resolves only to public internet addresses."""
    try:
        url = make_url(connection_url)
    except ArgumentError as exc:
        raise UnsafeTargetError("connection URL could not be parsed") from exc

    if not url.drivername.startswith("postgresql"):
        raise UnsafeTargetError("only postgresql:// connection URLs are supported")
    host = url.host
    if not host:
        raise UnsafeTargetError("connection URL must include a host")
    if "," in host or "/" in host:
        raise UnsafeTargetError("connection URL must name exactly one network host")
    unexpected = set(url.query) - _ALLOWED_QUERY_PARAMS
    if unexpected:
        raise UnsafeTargetError(
            f"connection URL has unsupported parameters: {', '.join(sorted(unexpected))}"
        )

    if settings.allow_private_targets:
        return

    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, url.port or 5432, type=socket.SOCK_STREAM
        )
    except socket.gaierror as exc:
        raise UnsafeTargetError(f"could not resolve database host '{host}'") from exc

    addresses = {str(info[4][0]) for info in infos}
    if not addresses or not all(_is_public(address) for address in addresses):
        raise UnsafeTargetError(
            f"database host '{host}' resolves to a private or reserved address; "
            "only databases reachable on the public internet are allowed"
        )
