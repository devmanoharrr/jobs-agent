import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import Job, JobSource, RawJob, Source
from app.pipeline.publish import publish_candidate
from app.schemas.canonical_job import CanonicalJobCandidate

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)


def test_india_posting_is_published_once() -> None:
    session = SessionLocal()
    try:
        source = _source(session, "acme")
        raw = _raw(session, source, "role-1")
        first = publish_candidate(
            session, source, _candidate("Backend Engineer", "Acme", ["Bengaluru, Karnataka, India"]), raw, checked_at=NOW
        )
        second = publish_candidate(
            session,
            source,
            _candidate("Backend Engineer", "Acme", ["Bengaluru, Karnataka, India"]),
            raw,
            checked_at=LATER,
        )
        session.flush()
        jobs = session.scalars(select(Job).where(Job.company_name == "Acme")).all()
        assert first == "created"
        assert second == "updated"
        assert len(jobs) == 1
        assert jobs[0].india_relevance == "india"
        assert jobs[0].city == "Bengaluru"
        assert jobs[0].state == "Karnataka"
        assert jobs[0].country_code == "IN"
        assert jobs[0].status == "active"
        assert jobs[0].last_seen_at == LATER
        assert session.scalar(select(func.count()).select_from(JobSource).where(JobSource.job_id == jobs[0].id)) == 1
        assert raw.processing_status == "processed"
    finally:
        session.rollback()
        session.close()


def test_not_india_is_not_published() -> None:
    session = SessionLocal()
    try:
        source = _source(session, "foreign")
        raw = _raw(session, source, "role-2")
        outcome = publish_candidate(
            session,
            source,
            _candidate("Account Executive", "Foreign Co", ["Athens, Greece"], description="Office in Athens."),
            raw,
            checked_at=NOW,
        )
        assert outcome == "rejected"
        assert raw.processing_status == "rejected"
        assert raw.processing_error == "not_india"
        assert session.scalar(select(func.count()).select_from(Job).where(Job.company_name == "Foreign Co")) == 0
    finally:
        session.rollback()
        session.close()


def test_same_title_at_two_companies_stays_two_jobs() -> None:
    session = SessionLocal()
    try:
        left = _source(session, "acme")
        right = _source(session, "globex")
        publish_candidate(
            session,
            left,
            _candidate("Backend Engineer", "Acme", ["Bengaluru, Karnataka, India"], source_job_id="a"),
            _raw(session, left, "a"),
            checked_at=NOW,
        )
        publish_candidate(
            session,
            right,
            _candidate("Backend Engineer", "Globex", ["Bengaluru, Karnataka, India"], source_job_id="b"),
            _raw(session, right, "b"),
            checked_at=NOW,
        )
        titles = session.scalars(
            select(Job.company_name).where(Job.canonical_source_id.in_([left.id, right.id]))
        ).all()
        assert sorted(titles) == ["Acme", "Globex"]
    finally:
        session.rollback()
        session.close()


def _source(session, key: str) -> Source:
    source = Source(
        name=key,
        source_type="greenhouse",
        external_key=f"publish-{key}-{uuid.uuid4().hex}",
        company_name=key,
    )
    session.add(source)
    session.flush()
    return source


def _raw(session, source: Source, source_job_id: str) -> RawJob:
    row = RawJob(
        source_id=source.id,
        source_job_id=source_job_id,
        payload={"id": source_job_id},
        payload_hash=uuid.uuid4().hex,
        fetched_at=NOW,
    )
    session.add(row)
    session.flush()
    return row


def _candidate(
    title: str,
    company: str,
    locations: list[str],
    *,
    description: str = "Build services for customers in the city.",
    source_job_id: str = "role-1",
) -> CanonicalJobCandidate:
    return CanonicalJobCandidate(
        title_original=title,
        company_name=company,
        description_text=description,
        locations_raw=locations,
        apply_url=f"https://boards.greenhouse.io/{company.lower()}/jobs/{source_job_id}",
        source_job_id=source_job_id,
        source_type="greenhouse",
        source_external_key=company.lower(),
    )
