import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select

from app.db import SessionLocal
from app.models.job import Job
from app.models.raw_job import RawJob
from app.models.source import Source
from app.pipeline.raw_store import persist_raw_if_changed
from app.schemas.raw_job import RawJobEnvelope


def _envelope(title: str) -> RawJobEnvelope:
    return RawJobEnvelope(
        source_type="greenhouse",
        source_external_key="groww",
        source_job_id="4970739101",
        source_url="https://job-boards.eu.greenhouse.io/groww/jobs/4970739101",
        fetched_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
        payload={"id": 4970739101, "title": title, "company_name": "Groww"},
    )


def test_persist_raw_keeps_history_and_does_not_overwrite() -> None:
    session = SessionLocal()
    try:
        source = Source(
            name="Raw store test",
            source_type="greenhouse",
            external_key=f"raw-test-{uuid.uuid4().hex}",
            company_name="Groww",
        )
        session.add(source)
        session.flush()
        jobs_before = session.scalar(select(func.count()).select_from(Job))

        first = persist_raw_if_changed(session, source, _envelope("Assistant Manager"))
        again = persist_raw_if_changed(session, source, _envelope("Assistant Manager"))
        changed = persist_raw_if_changed(session, source, _envelope("Assistant Manager - Updated"))
        session.flush()

        rows = session.scalars(select(RawJob).where(RawJob.source_id == source.id)).all()
        assert again.id == first.id
        assert changed.id != first.id
        assert len(rows) == 2
        stored = {row.id: row.payload["title"] for row in rows}
        assert stored[first.id] == "Assistant Manager"
        assert stored[changed.id] == "Assistant Manager - Updated"
        assert session.scalar(select(func.count()).select_from(Job)) == jobs_before
    finally:
        session.rollback()
        session.close()
