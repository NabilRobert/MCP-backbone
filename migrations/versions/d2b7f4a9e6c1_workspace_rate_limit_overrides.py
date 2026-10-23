"""per-workspace rate-limit overrides

Revision ID: d2b7f4a9e6c1
Revises: c8f1e3a5d7b9
Create Date: 2026-10-23 18:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'd2b7f4a9e6c1'
down_revision: Union[str, Sequence[str], None] = 'c8f1e3a5d7b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Nullable with no default: every existing workspace stays NULL, which
    # means "use the server-wide default", so nobody's limit changes.
    op.add_column("workspaces", sa.Column("query_rate_limit", sa.Integer(), nullable=True))
    op.add_column("workspaces", sa.Column("sample_rate_limit", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_workspaces_query_rate_limit", "workspaces", "query_rate_limit >= 0"
    )
    op.create_check_constraint(
        "ck_workspaces_sample_rate_limit", "workspaces", "sample_rate_limit >= 0"
    )


def downgrade() -> None:
    op.drop_constraint("ck_workspaces_sample_rate_limit", "workspaces", type_="check")
    op.drop_constraint("ck_workspaces_query_rate_limit", "workspaces", type_="check")
    op.drop_column("workspaces", "sample_rate_limit")
    op.drop_column("workspaces", "query_rate_limit")
