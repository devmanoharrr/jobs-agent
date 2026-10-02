"""Add seen/missing bookkeeping to job_sources.

Revision ID: 0002_job_source_freshness
Revises: 0001_initial_schema
Create Date: 2026-10-02

"""

from typing import Sequence, Union

from alembic import op

revision: str = "0002_job_source_freshness"
down_revision: Union[str, Sequence[str], None] = "0001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE job_sources
          ADD COLUMN missing_count integer NOT NULL DEFAULT 0,
          ADD COLUMN active boolean NOT NULL DEFAULT true
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE job_sources
          DROP COLUMN missing_count,
          DROP COLUMN active
        """
    )
