"""Store the sendable job JSON and the per-posting trail.

Revision ID: 0006_job_structure
Revises: 0005_jobi_sync
Create Date: 2026-10-06

"""

from typing import Sequence, Union

from alembic import op

revision: str = "0006_job_structure"
down_revision: Union[str, Sequence[str], None] = "0005_jobi_sync"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE jobs
          ADD COLUMN salary_period text,
          ADD COLUMN summary text,
          ADD COLUMN role_category text,
          ADD COLUMN experience_label text,
          ADD COLUMN responsibilities jsonb NOT NULL DEFAULT '[]'::jsonb,
          ADD COLUMN requirements jsonb NOT NULL DEFAULT '[]'::jsonb,
          ADD COLUMN nice_to_have jsonb NOT NULL DEFAULT '[]'::jsonb,
          ADD COLUMN benefits jsonb NOT NULL DEFAULT '[]'::jsonb,
          ADD COLUMN structured_payload jsonb,
          ADD COLUMN pipeline_trace jsonb NOT NULL DEFAULT '[]'::jsonb,
          ADD COLUMN structure_status text NOT NULL DEFAULT 'pending',
          ADD COLUMN structure_hash text
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE jobs
          DROP COLUMN IF EXISTS structure_hash,
          DROP COLUMN IF EXISTS structure_status,
          DROP COLUMN IF EXISTS pipeline_trace,
          DROP COLUMN IF EXISTS structured_payload,
          DROP COLUMN IF EXISTS benefits,
          DROP COLUMN IF EXISTS nice_to_have,
          DROP COLUMN IF EXISTS requirements,
          DROP COLUMN IF EXISTS responsibilities,
          DROP COLUMN IF EXISTS experience_label,
          DROP COLUMN IF EXISTS role_category,
          DROP COLUMN IF EXISTS summary,
          DROP COLUMN IF EXISTS salary_period
        """
    )
