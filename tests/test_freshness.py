import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import Job, JobSource, RawJob, Source
from app.pipeline.freshness import (
    MISS_THRESHOLD,
    SourceLink,
    apply_source_crawl,
    counts_as_successful_crawl,
    finalize_source_freshness,
    status_for_associations,
)

SEEN_AT = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
CHECKED_AT = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)


def test_one_successful_miss_does_not_expire() -> None:
    once = apply_source_crawl((SourceLink("role-1"),), set(), crawl_status="success")
    assert once[0].missing_count == 1
    assert once[0].active is True
    assert status_for_associations(once) == "active"


def test_two_successful_misses_expire() -> None:
    once = apply_source_crawl((SourceLink("role-1"),), set(), crawl_status="success")
    twice = apply_source_crawl(once, set(), crawl_status="success")
    assert twice[0].missing_count == MISS_THRESHOLD
    assert twice[0].active is False
    assert status_for_associations(twice) == "expired"


def test_failed_or_incomplete_crawl_does_not_expire() -> None:
    already_missed = (SourceLink("role-1", missing_count=1, active=True),)
    for status in ("failed", "partial", "incomplete"):
        assert counts_as_successful_crawl(status) is False
        result = apply_source_crawl(already_missed, set(), crawl_status=status)
        assert result[0].missing_count == 1
        assert result[0].active is True
        assert status_for_associations(result) == "active"


def test_seen_again_resets_the_miss_count() -> None:
    missed = apply_source_crawl((SourceLink("role-1"),), set(), crawl_status="success")
    seen = apply_source_crawl(missed, {"role-1"}, crawl_status="success")
    assert seen[0].missing_count == 0
    assert seen[0].active is True
    following_miss = apply_source_crawl(seen, set(), crawl_status="success")
    assert following_miss[0].missing_count == 1
    assert status_for_associations(following_miss) == "active"


def test_another_active_source_keeps_the_job_published() -> None:
    missed_twice = apply_source_crawl(
        apply_source_crawl((SourceLink("role-1"),), set(), crawl_status="success"),
        set(),
        crawl_status="success",
    )
    assert status_for_associations((*missed_twice, SourceLink("other-role"))) == "active"


def test_failed_crawl_leaves_stored_jobs_active() -> None:
    session = SessionLocal()
    try:
        source, jobs, raw = _source_with_jobs(session, ["role-1", "role-2"])
        finalize_source_freshness(
            session,
            source.id,
            set(),
            crawl_status="failed",
            checked_at=CHECKED_AT,
        )
        session.refresh(jobs[0])
        session.refresh(jobs[1])
        assert jobs[0].status == "active"
        assert jobs[1].status == "active"
        assert jobs[0].last_checked_at is None
        links = session.scalars(select(JobSource).where(JobSource.source_id == source.id)).all()
        assert len(links) == 2
        assert all(link.missing_count == 0 and link.active for link in links)
        session.refresh(raw)
        assert raw.payload == {"title": "Backend Engineer"}
    finally:
        session.rollback()
        session.close()


def test_stored_job_expires_on_the_second_successful_miss_and_history_remains() -> None:
    session = SessionLocal()
    try:
        source, jobs, raw = _source_with_jobs(session, ["role-1"])
        job = jobs[0]
        finalize_source_freshness(
            session, source.id, set(), crawl_status="success", checked_at=CHECKED_AT
        )
        session.refresh(job)
        link = session.scalars(select(JobSource).where(JobSource.job_id == job.id)).one()
        assert link.missing_count == 1
        assert link.active is True
        assert job.status == "active"
        assert job.last_seen_at == SEEN_AT

        finalize_source_freshness(
            session, source.id, set(), crawl_status="success", checked_at=LATER
        )
        session.refresh(job)
        session.refresh(link)
        assert link.missing_count == 2
        assert link.active is False
        assert job.status == "expired"
        remaining = session.scalar(
            select(func.count()).select_from(JobSource).where(JobSource.job_id == job.id)
        )
        assert remaining == 1
        session.refresh(raw)
        assert raw.payload == {"title": "Backend Engineer"}
        assert raw.processing_status == "pending"
    finally:
        session.rollback()
        session.close()


def test_returning_id_reactivates_an_expired_association() -> None:
    session = SessionLocal()
    try:
        source, jobs, _raw = _source_with_jobs(session, ["role-1"])
        job = jobs[0]
        finalize_source_freshness(
            session, source.id, set(), crawl_status="success", checked_at=CHECKED_AT
        )
        finalize_source_freshness(
            session, source.id, set(), crawl_status="success", checked_at=LATER
        )
        finalize_source_freshness(
            session,
            source.id,
            {"role-1"},
            crawl_status="success",
            checked_at=LATER,
        )
        session.refresh(job)
        link = session.scalars(select(JobSource).where(JobSource.job_id == job.id)).one()
        assert link.missing_count == 0
        assert link.active is True
        assert link.last_seen_at == LATER
        assert job.status == "active"
        assert job.last_seen_at == LATER
    finally:
        session.rollback()
        session.close()


def _source_with_jobs(
    session, source_job_ids: list[str]
) -> tuple[Source, list[Job], RawJob]:
    source = Source(
        name="Example Greenhouse",
        source_type="greenhouse",
        external_key=f"example-{uuid.uuid4().hex}",
        company_name="Example",
    )
    session.add(source)
    session.flush()
    raw = RawJob(
        source_id=source.id,
        source_job_id=source_job_ids[0],
        payload={"title": "Backend Engineer"},
        payload_hash=uuid.uuid4().hex,
        fetched_at=SEEN_AT,
    )
    session.add(raw)
    session.flush()

    jobs: list[Job] = []
    for source_job_id in source_job_ids:
        job = Job(
            title_original="Backend Engineer",
            company_name="Example",
            company_normalized="example",
            india_relevance="india",
            first_seen_at=SEEN_AT,
            last_seen_at=SEEN_AT,
            exact_fingerprint=uuid.uuid4().hex,
        )
        session.add(job)
        session.flush()
        session.add(
            JobSource(
                job_id=job.id,
                source_id=source.id,
                source_job_id=source_job_id,
                raw_job_id=raw.id,
                first_seen_at=SEEN_AT,
                last_seen_at=SEEN_AT,
            )
        )
        jobs.append(job)
    session.flush()
    return source, jobs, raw
