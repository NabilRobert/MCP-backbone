"""one rate-limit counter per bucket and one schema cache per workspace

Two concurrent first requests could each insert a row, after which every
lookup failed with MultipleResultsFound and the workspace was cut off
(reported by sql-analyst-env, 2026-11-07). The code now upserts against
these constraints. Duplicates already in the table are removed first.

Revision ID: 45617dce8cdd
Revises: 3fbb5eeefe5a
Create Date: 2026-11-07 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '45617dce8cdd'
down_revision: Union[str, Sequence[str], None] = '3fbb5eeefe5a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Keep the highest count, so a duplicate never hands out extra requests.
    op.execute(
        "delete from rate_limit_counters a using rate_limit_counters b"
        " where a.workspace_id = b.workspace_id and a.bucket = b.bucket"
        " and (a.count < b.count or (a.count = b.count and a.id < b.id))"
    )
    op.create_unique_constraint(
        "uq_rate_limit_counters_workspace_bucket",
        "rate_limit_counters",
        ["workspace_id", "bucket"],
    )
    # Keep the newest copy of the schema.
    op.execute(
        "delete from schema_caches a using schema_caches b"
        " where a.workspace_id = b.workspace_id"
        " and (a.cached_at < b.cached_at or (a.cached_at = b.cached_at and a.id < b.id))"
    )
    op.create_unique_constraint("uq_schema_caches_workspace", "schema_caches", ["workspace_id"])


def downgrade() -> None:
    op.drop_constraint("uq_schema_caches_workspace", "schema_caches", type_="unique")
    op.drop_constraint(
        "uq_rate_limit_counters_workspace_bucket", "rate_limit_counters", type_="unique"
    )
