import uuid
from datetime import datetime

from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.config import settings
from app.crypto import decrypt, encrypt
from app.db import Base

# These three classes describe the database tables involved in actually
# running a query, as opposed to app/auth/models.py which handles who's
# allowed to ask.


class DataSourceConnection(Base):
    """Which real, outside database a workspace is allowed to query.

    Either the server's default database, or one of the workspace's own:
    - Default (encrypted_connection_url is NULL): resolved from
      APP_TARGET_DATABASE_URL every time it's used, so changing that
      setting moves every default workspace at once, and the default's
      password is never copied into this table. Operator-configured, so
      it's exempt from the private-address check (it may well be on
      localhost).
    - Its own (an invited user's, or one the operator seeded): stored
      ENCRYPTED (see app/crypto.py), and checked by
      app/query/network_guard.py at registration and again by
      get_data_source() on every use.

    Callers just read `connection_url`, which handles both."""

    __tablename__ = "data_source_connections"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id"))
    encrypted_connection_url: Mapped[str | None]

    @property
    def uses_default_target(self) -> bool:
        return self.encrypted_connection_url is None

    @property
    def connection_url(self) -> str:
        if self.encrypted_connection_url is None:
            return settings.target_database_url
        return decrypt(self.encrypted_connection_url)

    @connection_url.setter
    def connection_url(self, value: str) -> None:
        self.encrypted_connection_url = encrypt(value)


class QueryExecution(Base):
    """One attempt to run a query -- and this record *is* the audit
    log entry for it. There's no separate "audit log" table elsewhere;
    every single attempt (whether it succeeded, got rejected by the
    safety checks, or errored out) gets exactly one row here, which is
    what lets you look back and see everything this server has ever
    been asked to do."""

    __tablename__ = "query_executions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id"))
    data_source_connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("data_source_connections.id")
    )
    query_text: Mapped[str]
    status: Mapped[str]  # "succeeded", "rejected", "errored", or "timed_out"
    cost_estimate: Mapped[float | None]
    rejection_reason: Mapped[str | None]
    created_at: Mapped[datetime]


class SchemaCache(Base):
    """A saved copy of "what tables/columns exist?" for a workspace, so
    we don't have to re-ask the real database every single time someone
    wants to browse the schema. Important: this is a convenience copy
    only -- it's never used to decide whether an actual query is safe or
    what it returns. If this cache goes stale, the worst that happens is
    describe_table shows outdated info for a few minutes; it can never
    cause a real query to return wrong data, since real queries always
    go straight to the live database."""

    __tablename__ = "schema_caches"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id"))
    table_metadata_json: Mapped[str]
    cached_at: Mapped[datetime]
