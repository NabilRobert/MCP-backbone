"""invite codes, encrypted connection URLs, per-bucket rate limits

Revision ID: a7c3e9b2d410
Revises: 1dc23f88f669
Create Date: 2026-10-22 15:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.crypto import decrypt, encrypt


# revision identifiers, used by Alembic.
revision: str = 'a7c3e9b2d410'
down_revision: Union[str, Sequence[str], None] = '1dc23f88f669'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "invite_codes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code_hash", sa.String(), nullable=False),
        sa.Column("note", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("used_by_workspace_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(["used_by_workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code_hash"),
    )

    # Existing counters all tracked run_query, so they become "query".
    op.add_column(
        "rate_limit_counters",
        sa.Column("bucket", sa.String(), nullable=False, server_default="query"),
    )

    # Encrypt every existing connection URL in place. Needs
    # APP_CONNECTION_ENCRYPTION_KEY set -- app/crypto.py raises a clear
    # error before anything changes if it isn't.
    op.add_column(
        "data_source_connections",
        sa.Column("encrypted_connection_url", sa.String(), nullable=True),
    )
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id, connection_url FROM data_source_connections")).all()
    for row_id, plaintext in rows:
        bind.execute(
            sa.text(
                "UPDATE data_source_connections SET encrypted_connection_url = :enc WHERE id = :id"
            ),
            {"enc": encrypt(plaintext), "id": row_id},
        )
    op.alter_column("data_source_connections", "encrypted_connection_url", nullable=False)
    op.drop_column("data_source_connections", "connection_url")


def downgrade() -> None:
    op.add_column(
        "data_source_connections",
        sa.Column("connection_url", sa.String(), nullable=True),
    )
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, encrypted_connection_url FROM data_source_connections")
    ).all()
    for row_id, token in rows:
        bind.execute(
            sa.text("UPDATE data_source_connections SET connection_url = :url WHERE id = :id"),
            {"url": decrypt(token), "id": row_id},
        )
    op.alter_column("data_source_connections", "connection_url", nullable=False)
    op.drop_column("data_source_connections", "encrypted_connection_url")

    op.drop_column("rate_limit_counters", "bucket")
    op.drop_table("invite_codes")
