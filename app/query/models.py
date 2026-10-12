import uuid
from datetime import datetime

from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class DataSourceConnection(Base):
    __tablename__ = "data_source_connections"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id"))
    connection_url: Mapped[str]


class QueryExecution(Base):
    """A single query attempt. This record is the audit log entry — see
    Phase 3: QueryExecution and AuditEntry were merged, they are always 1:1."""

    __tablename__ = "query_executions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id"))
    data_source_connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("data_source_connections.id")
    )
    query_text: Mapped[str]
    status: Mapped[str]
    cost_estimate: Mapped[float | None]
    rejection_reason: Mapped[str | None]
    created_at: Mapped[datetime]


class SchemaCache(Base):
    """Advisory only. Never consulted during query execution — see Phase 3/4."""

    __tablename__ = "schema_caches"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id"))
    table_metadata_json: Mapped[str]
    cached_at: Mapped[datetime]
