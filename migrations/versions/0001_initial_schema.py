"""Initial schema from blueprint section 7.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-10-01

"""

from typing import Sequence, Union

from alembic import op

revision: str = "0001_initial_schema"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE sources (
          id uuid PRIMARY KEY,
          name text NOT NULL,
          source_type text NOT NULL,
          base_url text,
          external_key text NOT NULL,
          company_name text,
          country_scope text DEFAULT 'IN',
          enabled boolean NOT NULL DEFAULT true,
          crawl_interval_minutes integer NOT NULL DEFAULT 360,
          last_crawled_at timestamptz,
          next_crawl_at timestamptz,
          last_success_at timestamptz,
          consecutive_failures integer NOT NULL DEFAULT 0,
          robots_checked_at timestamptz,
          metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
          UNIQUE (source_type, external_key)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE raw_jobs (
          id uuid PRIMARY KEY,
          source_id uuid NOT NULL REFERENCES sources (id),
          source_job_id text NOT NULL,
          canonical_url text,
          payload jsonb NOT NULL,
          payload_hash text NOT NULL,
          fetched_at timestamptz NOT NULL,
          crawl_run_id uuid,
          processing_status text NOT NULL DEFAULT 'pending',
          processing_error text,
          UNIQUE (source_id, source_job_id, payload_hash)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE jobs (
          id uuid PRIMARY KEY,
          title_original text NOT NULL,
          title_normalized text,
          company_name text NOT NULL,
          company_normalized text NOT NULL,
          description_text text,
          description_html text,
          city text,
          state text,
          country_code text,
          work_mode text,
          india_relevance text NOT NULL,
          employment_type text,
          experience_min numeric,
          experience_max numeric,
          salary_min numeric,
          salary_max numeric,
          salary_currency text,
          skills jsonb NOT NULL DEFAULT '[]'::jsonb,
          posted_at timestamptz,
          expires_at timestamptz,
          first_seen_at timestamptz NOT NULL,
          last_seen_at timestamptz NOT NULL,
          last_checked_at timestamptz,
          status text NOT NULL DEFAULT 'active',
          canonical_source_id uuid REFERENCES sources (id),
          canonical_apply_url text,
          exact_fingerprint text NOT NULL,
          fuzzy_fingerprint text,
          ai_enriched boolean NOT NULL DEFAULT false,
          enrichment_version text
        )
        """
    )
    op.execute(
        """
        CREATE INDEX jobs_india_idx ON jobs (india_relevance, status);
        CREATE INDEX jobs_company_idx ON jobs (company_normalized);
        CREATE INDEX jobs_posted_idx ON jobs (posted_at DESC);
        """
    )
    op.execute(
        """
        CREATE TABLE job_sources (
          job_id uuid NOT NULL REFERENCES jobs (id),
          source_id uuid NOT NULL REFERENCES sources (id),
          source_job_id text NOT NULL,
          source_url text,
          raw_job_id uuid REFERENCES raw_jobs (id),
          first_seen_at timestamptz NOT NULL,
          last_seen_at timestamptz NOT NULL,
          PRIMARY KEY (job_id, source_id, source_job_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE crawl_runs (
          id uuid PRIMARY KEY,
          source_id uuid NOT NULL REFERENCES sources (id),
          started_at timestamptz NOT NULL,
          finished_at timestamptz,
          status text NOT NULL,
          fetched_count integer NOT NULL DEFAULT 0,
          inserted_count integer NOT NULL DEFAULT 0,
          updated_count integer NOT NULL DEFAULT 0,
          rejected_count integer NOT NULL DEFAULT 0,
          error_text text
        )
        """
    )
    op.execute(
        """
        CREATE TABLE ai_cache (
          cache_key text PRIMARY KEY,
          task_type text NOT NULL,
          model text NOT NULL,
          prompt_version text NOT NULL,
          response_json jsonb NOT NULL,
          created_at timestamptz NOT NULL
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS job_sources")
    op.execute("DROP TABLE IF EXISTS crawl_runs")
    op.execute("DROP TABLE IF EXISTS raw_jobs")
    op.execute("DROP TABLE IF EXISTS jobs")
    op.execute("DROP TABLE IF EXISTS ai_cache")
    op.execute("DROP TABLE IF EXISTS sources")
