import uuid
from datetime import datetime

from sqlalchemy import ARRAY, CheckConstraint, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.auth.scopes import ALL_TOOLS
from app.db import Base

# These classes each describe one database table -- SQLAlchemy
# turns them into real "CREATE TABLE" statements (see migrations/).
# Every row has a random, unguessable id (a UUID) instead of a simple
# counting number, so nobody can guess another row's id by incrementing.


class Workspace(Base):
    """One "tenant" -- a customer/project using this server. Everything
    else (keys, rate limits, which database to query) belongs to exactly
    one workspace."""

    __tablename__ = "workspaces"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str]
    created_at: Mapped[datetime]
    # How it was created: "operator" (scripts/seed_workspace.py),
    # "invite", or "demo" (self-serve, no invite). The daily demo
    # signup cap counts "demo" rows.
    created_via: Mapped[str] = mapped_column(default="operator", server_default="operator")
    # Per-hour allowances for run_query and sample_rows. NULL (every
    # workspace, unless the operator sets one) means the server-wide
    # default (APP_DEMO_RATE_LIMIT / APP_SAMPLE_RATE_LIMIT). Set only by
    # the operator (scripts/seed_workspace.py), never from the console:
    # a self-serve signup that could raise its own limit wouldn't be
    # limited at all.
    query_rate_limit: Mapped[int | None]
    sample_rate_limit: Mapped[int | None]

    __table_args__ = (
        CheckConstraint("query_rate_limit >= 0", name="ck_workspaces_query_rate_limit"),
        CheckConstraint("sample_rate_limit >= 0", name="ck_workspaces_sample_rate_limit"),
    )


class ApiKey(Base):
    """The credential an AI client presents to run queries. We never
    store the key itself, only a one-way hash of it (see
    app/auth/security.py) -- so even if this table leaked, nobody could
    recover a usable key from it. status is either "active" or
    "revoked"; a revoked key stops authenticating immediately, but
    doesn't affect the workspace's separate ConsoleCredential below."""

    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id"))
    key_hash: Mapped[str]
    status: Mapped[str]
    created_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]
    # The MCP tools this key may call (app/auth/scopes.py). Every key made
    # before scopes existed, and every key made without choosing, gets all.
    scopes: Mapped[list[str]] = mapped_column(
        ARRAY(String),
        default=lambda: list(ALL_TOOLS),
        server_default="{" + ",".join(ALL_TOOLS) + "}",
    )


class ConsoleCredential(Base):
    """The login for the web dashboard -- separate on purpose from
    ApiKey above. If the API key gets compromised and revoked, you can
    still log into the dashboard with this to see what happened."""

    __tablename__ = "console_credentials"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id"))
    # unique=True because app/console/auth.py's login check uses
    # scalar_one_or_none() on this column -- a real duplicate username
    # (confirmed: two workspaces both seeded with "arthur") made that
    # raise MultipleResultsFound instead of a clean "wrong password",
    # crashing every login attempt for either workspace. Enforced at
    # the database level so this can't recur no matter which script
    # creates a ConsoleCredential.
    username: Mapped[str] = mapped_column(unique=True)
    password_hash: Mapped[str]
    # Stamped into every login's session cookie. A session whose stamp no
    # longer matches is refused, so bumping this ("log out everywhere")
    # revokes every existing session server-side, which a signed cookie
    # alone can't do.
    session_version: Mapped[int] = mapped_column(default=0, server_default="0")


class RateLimitCounter(Base):
    """Tracks how many requests a workspace has made recently, to stop
    one workspace from running up huge costs. window_started_at marks
    when the current counting period began; count resets to 0 once a
    new window starts (see app/auth/rate_limit.py for the actual
    reset/check logic -- this class only stores the numbers)."""

    __tablename__ = "rate_limit_counters"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id"))
    # Which allowance this counter tracks: "query" (run_query) or
    # "sample" (sample_rows). Separate rows, so each has its own limit.
    bucket: Mapped[str] = mapped_column(default="query", server_default="query")
    window_started_at: Mapped[datetime]
    count: Mapped[int] = mapped_column(default=0)


class InviteCode(Base):
    """A single-use code that lets one person sign up for their own
    workspace (app/auth/signup.py). Issued by the operator with
    scripts/create_invite.py. Like API keys, only a hash is stored: the
    code exists in plain text exactly once, when it's issued. `note`
    records who it was for. used_at/used_by_workspace_id are set when
    it's redeemed, after which it can never be redeemed again."""

    __tablename__ = "invite_codes"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    code_hash: Mapped[str] = mapped_column(unique=True)
    note: Mapped[str]
    created_at: Mapped[datetime]
    expires_at: Mapped[datetime | None]
    used_at: Mapped[datetime | None]
    used_by_workspace_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("workspaces.id"))


class ThrottleEvent(Base):
    """One counted event for a per-IP limit (app/console/throttle.py):
    a failed login or signup, or a demo signup. `key` is a SHA-256 of the
    client IP, never the IP itself, and rows are pruned after a day."""

    __tablename__ = "throttle_events"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    bucket: Mapped[str]
    key: Mapped[str] = mapped_column(index=True)
    created_at: Mapped[datetime]
