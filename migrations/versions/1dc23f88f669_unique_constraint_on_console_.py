"""unique constraint on console_credentials username

Revision ID: 1dc23f88f669
Revises: 072ff1f9b9d6
Create Date: 2026-10-20 14:56:58.358591

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '1dc23f88f669'
down_revision: Union[str, Sequence[str], None] = '072ff1f9b9d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Prevents two workspaces from ever being seeded with the same
    # console login username again -- a real duplicate ("arthur" twice)
    # made every console login attempt crash with MultipleResultsFound
    # instead of a clean "wrong password", found on a real deployment.
    op.create_unique_constraint(
        "uq_console_credentials_username", "console_credentials", ["username"]
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_console_credentials_username", "console_credentials", type_="unique"
    )
