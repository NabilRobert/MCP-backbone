"""default-database workspaces reference the setting instead of a copy

Revision ID: e8b2a6f1c3d9
Revises: c41f8d2e9b07
Create Date: 2026-10-22 17:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.config import settings
from app.crypto import decrypt, encrypt


# revision identifiers, used by Alembic.
revision: str = 'e8b2a6f1c3d9'
down_revision: Union[str, Sequence[str], None] = 'c41f8d2e9b07'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # NULL now means "the server's default database (APP_TARGET_DATABASE_URL)".
    op.alter_column("data_source_connections", "encrypted_connection_url", nullable=True)

    # Every existing row that is just a copy of the CURRENT default becomes
    # a reference to it, so changing APP_TARGET_DATABASE_URL afterwards
    # moves those workspaces too. Run this before changing the setting.
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, encrypted_connection_url FROM data_source_connections")
    ).all()
    for row_id, token in rows:
        if token is not None and decrypt(token) == settings.target_database_url:
            bind.execute(
                sa.text(
                    "UPDATE data_source_connections SET encrypted_connection_url = NULL "
                    "WHERE id = :id"
                ),
                {"id": row_id},
            )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE data_source_connections SET encrypted_connection_url = :enc "
            "WHERE encrypted_connection_url IS NULL"
        ),
        {"enc": encrypt(settings.target_database_url)},
    )
    op.alter_column("data_source_connections", "encrypted_connection_url", nullable=False)
