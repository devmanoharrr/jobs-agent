"""Daily OpenRouter request counter.

Revision ID: 0004_ai_budget
Revises: 0003_job_search_trgm
Create Date: 2026-10-02

"""

from typing import Sequence, Union

from alembic import op

revision: str = "0004_ai_budget"
down_revision: Union[str, Sequence[str], None] = "0003_job_search_trgm"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE ai_budget (
          day date PRIMARY KEY,
          request_count integer NOT NULL DEFAULT 0
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ai_budget")
