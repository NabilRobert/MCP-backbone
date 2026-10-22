"""workspace created_via, for the daily demo signup cap

Revision ID: c41f8d2e9b07
Revises: a7c3e9b2d410
Create Date: 2026-10-22 16:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'c41f8d2e9b07'
down_revision: Union[str, Sequence[str], None] = 'a7c3e9b2d410'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing workspaces were all made by the operator or with an
    # invite; neither counts toward the demo cap, so "operator" is a
    # safe default for them.
    op.add_column(
        "workspaces",
        sa.Column("created_via", sa.String(), nullable=False, server_default="operator"),
    )
    # Invite signups so far are identifiable: their invite records them.
    op.execute(
        "UPDATE workspaces SET created_via = 'invite' WHERE id IN "
        "(SELECT used_by_workspace_id FROM invite_codes WHERE used_by_workspace_id IS NOT NULL)"
    )


def downgrade() -> None:
    op.drop_column("workspaces", "created_via")
