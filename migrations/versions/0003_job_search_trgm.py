"""Trigram indexes for ILIKE job search.

Revision ID: 0003_job_search_trgm
Revises: 0002_job_source_freshness
Create Date: 2026-10-02

"""

from typing import Sequence, Union

from alembic import op

revision: str = "0003_job_search_trgm"
down_revision: Union[str, Sequence[str], None] = "0002_job_source_freshness"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        """
        CREATE INDEX jobs_title_trgm_idx ON jobs USING gin (title_original gin_trgm_ops);
        CREATE INDEX jobs_company_name_trgm_idx ON jobs USING gin (company_name gin_trgm_ops);
        CREATE INDEX jobs_description_trgm_idx ON jobs USING gin (description_text gin_trgm_ops);
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS jobs_description_trgm_idx")
    op.execute("DROP INDEX IF EXISTS jobs_company_name_trgm_idx")
    op.execute("DROP INDEX IF EXISTS jobs_title_trgm_idx")
