import uuid
from datetime import datetime, timezone

from sqlalchemy import inspect

from app.db import SessionLocal, engine
from app.models import AiBudget, AiCache, CrawlRun, Job, JobSource, RawJob, Source


def test_source_and_job_columns_match_database() -> None:
    inspector = inspect(engine)
    for model in (Source, Job, RawJob, JobSource, CrawlRun, AiCache, AiBudget):
        db_columns = {column["name"] for column in inspector.get_columns(model.__tablename__)}
        model_columns = {column.name for column in model.__table__.columns}
        assert model_columns == db_columns


def test_source_and_job_round_trip() -> None:
    now = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    session = SessionLocal()
    try:
        source = Source(
            name="Example Greenhouse",
            source_type="greenhouse",
            external_key=f"example-{uuid.uuid4().hex}",
            company_name="Example",
            base_url="https://boards.greenhouse.io/example",
        )
        session.add(source)
        session.flush()

        job = Job(
            title_original="Backend Engineer",
            company_name="Example",
            company_normalized="example",
            india_relevance="india",
            first_seen_at=now,
            last_seen_at=now,
            exact_fingerprint="example|backend-engineer|bengaluru",
            canonical_source_id=source.id,
            city="Bengaluru",
        )
        session.add(job)
        session.flush()
        session.refresh(source)
        session.refresh(job)

        assert source.country_scope == "IN"
        assert source.enabled is True
        assert source.crawl_interval_minutes == 360
        assert source.consecutive_failures == 0
        assert source.source_metadata == {}
        assert job.status == "active"
        assert job.skills == []
        assert job.ai_enriched is False
        assert job.canonical_source_id == source.id
        assert job.canonical_source is not None
        assert job.canonical_source.external_key == source.external_key
    finally:
        session.rollback()
        session.close()
