"""Remember which jobs were accepted by Jobi local and prod.

Revision ID: 0005_jobi_sync
Revises: 0004_ai_budget
Create Date: 2026-10-05

"""

from typing import Sequence, Union

from alembic import op

revision: str = "0005_jobi_sync"
down_revision: Union[str, Sequence[str], None] = "0004_ai_budget"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE jobi_syncs (
          id uuid PRIMARY KEY,
          target text NOT NULL,
          external_key text NOT NULL,
          synced_at timestamptz NOT NULL,
          UNIQUE (target, external_key)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS jobi_syncs")
