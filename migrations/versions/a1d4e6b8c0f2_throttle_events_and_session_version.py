"""database-backed throttles, and revocable console sessions

Revision ID: a1d4e6b8c0f2
Revises: f3a9c7e1b5d2
Create Date: 2026-10-22 19:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'a1d4e6b8c0f2'
down_revision: Union[str, Sequence[str], None] = 'f3a9c7e1b5d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Per-IP limits move from process memory to here (app/console/throttle.py),
    # so they survive restarts. `key` is a SHA-256 of the client IP.
    op.create_table(
        "throttle_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("bucket", sa.String(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_throttle_events_key", "throttle_events", ["key"])
    # Bumped by "log out everywhere"; sessions stamped with an older value
    # are refused. Existing sessions carry no stamp, so they end once.
    op.add_column(
        "console_credentials",
        sa.Column("session_version", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("console_credentials", "session_version")
    op.drop_index("ix_throttle_events_key", table_name="throttle_events")
    op.drop_table("throttle_events")
