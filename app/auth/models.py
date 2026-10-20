import uuid
from datetime import datetime

from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

# These four classes each describe one database table -- SQLAlchemy
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


class RateLimitCounter(Base):
    """Tracks how many requests a workspace has made recently, to stop
    one workspace from running up huge costs. window_started_at marks
    when the current counting period began; count resets to 0 once a
    new window starts (see app/auth/rate_limit.py for the actual
    reset/check logic -- this class only stores the numbers)."""

    __tablename__ = "rate_limit_counters"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id"))
    window_started_at: Mapped[datetime]
    count: Mapped[int] = mapped_column(default=0)
