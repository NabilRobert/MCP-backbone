"""benchmark: question difficulty, strict score, model and run time

Revision ID: b7e2c9d4a1f6
Revises: a1d4e6b8c0f2
Create Date: 2026-10-22 20:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'b7e2c9d4a1f6'
down_revision: Union[str, Sequence[str], None] = 'a1d4e6b8c0f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "eval_cases",
        sa.Column("difficulty", sa.String(), nullable=False, server_default="unrated"),
    )
    op.add_column("benchmark_results", sa.Column("strict_correct", sa.Boolean(), nullable=True))
    op.add_column("benchmark_results", sa.Column("model", sa.String(), nullable=True))
    op.add_column(
        "benchmark_results",
        sa.Column("run_started_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("benchmark_results", "run_started_at")
    op.drop_column("benchmark_results", "model")
    op.drop_column("benchmark_results", "strict_correct")
    op.drop_column("eval_cases", "difficulty")
