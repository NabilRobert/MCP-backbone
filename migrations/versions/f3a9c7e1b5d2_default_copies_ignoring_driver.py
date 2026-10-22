"""convert default-database copies that differ only in driver prefix

Revision ID: f3a9c7e1b5d2
Revises: e8b2a6f1c3d9
Create Date: 2026-10-22 18:00:00.000000

e8b2a6f1c3d9 turned every stored URL that was an exact copy of
APP_TARGET_DATABASE_URL into a reference to it. On the live server it
missed one: same database and credentials, but written
"postgresql+asyncpg://" where the setting said "postgresql://". That
row was converted by hand. This repeats the conversion with the driver
prefix ignored, for any database that still has such a row. It's a new
migration rather than an edit to e8b2a6f1c3d9, because that one has
already run in production.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine.url import make_url

from app.config import settings
from app.crypto import decrypt


# revision identifiers, used by Alembic.
revision: str = 'f3a9c7e1b5d2'
down_revision: Union[str, Sequence[str], None] = 'e8b2a6f1c3d9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _same_database(a: str, b: str) -> bool:
    """Same database and login, ignoring the driver prefix: app/db.py
    forces asyncpg either way, so the two connect identically."""
    return make_url(a).set(drivername="postgresql") == make_url(b).set(drivername="postgresql")


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, encrypted_connection_url FROM data_source_connections "
            "WHERE encrypted_connection_url IS NOT NULL"
        )
    ).all()
    for row_id, token in rows:
        if _same_database(decrypt(token), settings.target_database_url):
            bind.execute(
                sa.text(
                    "UPDATE data_source_connections SET encrypted_connection_url = NULL "
                    "WHERE id = :id"
                ),
                {"id": row_id},
            )


def downgrade() -> None:
    # Nothing to undo safely: a converted row now follows the default,
    # which is the database it pointed at anyway.
    pass
