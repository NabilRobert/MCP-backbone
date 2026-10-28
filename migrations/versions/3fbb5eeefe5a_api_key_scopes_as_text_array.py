"""api_keys.scopes as text[] instead of varchar[]

Revision ID: 3fbb5eeefe5a
Revises: d2b7f4a9e6c1
Create Date: 2026-10-28 13:30:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '3fbb5eeefe5a'
down_revision: Union[str, Sequence[str], None] = 'd2b7f4a9e6c1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Why: every tool call authenticates on a fresh connection (the
# bookkeeping engine uses NullPool), and asyncpg has no built-in codec
# for varchar[], so it looked the type up on every connection: about
# 8.5 ms per call, measured. text[] is built in. The values are the same
# strings either way. The default is set again too: on its own, Postgres
# keeps the old expression with its ::varchar[] cast, which a database
# built fresh from the models wouldn't have.
_ALL_TOOLS = "'{list_tables,describe_table,sample_rows,run_query}'"


def upgrade() -> None:
    op.execute(
        "ALTER TABLE api_keys ALTER COLUMN scopes TYPE text[], "
        f"ALTER COLUMN scopes SET DEFAULT {_ALL_TOOLS}::text[]"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE api_keys ALTER COLUMN scopes TYPE varchar[], "
        f"ALTER COLUMN scopes SET DEFAULT {_ALL_TOOLS}::varchar[]"
    )
