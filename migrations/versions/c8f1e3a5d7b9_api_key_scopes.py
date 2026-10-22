"""per-tool permissions on API keys

Revision ID: c8f1e3a5d7b9
Revises: b7e2c9d4a1f6
Create Date: 2026-10-23 10:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'c8f1e3a5d7b9'
down_revision: Union[str, Sequence[str], None] = 'b7e2c9d4a1f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing keys keep exactly what they could do before: every tool.
    op.add_column(
        "api_keys",
        sa.Column(
            "scopes",
            postgresql.ARRAY(sa.String()),
            nullable=False,
            server_default="{list_tables,describe_table,sample_rows,run_query}",
        ),
    )


def downgrade() -> None:
    op.drop_column("api_keys", "scopes")
