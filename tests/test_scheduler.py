import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED

from sqlalchemy import delete, select

from app.db import SessionLocal
from app.models import CrawlRun, Job, JobSource, RawJob, Source
from app.scheduler.scheduler import DueSourceScheduler
from app.scheduler.tasks import (
    ATS_INTERVAL_MINUTES,
    SLOW_INTERVAL_MINUTES,
    interval_minutes,
    is_due,
    run_source_crawl,
    select_due_sources,
    source_job_id,
)
from app.schemas.raw_job import RawJobEnvelope
from app.connectors.base import ConnectorError, SourceForbidden

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


def test_intervals_follow_the_source_type() -> None:
    assert interval_minutes("greenhouse") == ATS_INTERVAL_MINUTES
    assert interval_minutes("lever", 360) == 360
    assert interval_minutes("ashby") == 6 * 60
    assert interval_minutes("workable") == 6 * 60
    assert interval_minutes("adzuna", 360) == SLOW_INTERVAL_MINUTES
    assert interval_minutes("generic", 24 * 60) == 24 * 60
    assert SLOW_INTERVAL_MINUTES == 12 * 60


def test_only_enabled_sources_whose_time_has_arrived_are_due() -> None:
    assert is_due(True, None, NOW) is True
    assert is_due(True, NOW - timedelta(minutes=1), NOW) is True
    assert is_due(True, NOW + timedelta(hours=1), NOW) is False
    assert is_due(False, None, NOW) is False


def test_due_query_skips_disabled_and_future_sources() -> None:
    session = SessionLocal()
    prefix = uuid.uuid4().hex
    try:
        due = _source(session, prefix, "due", next_crawl_at=NOW - timedelta(minutes=5))
        _source(session, prefix, "later", next_crawl_at=NOW + timedelta(hours=2))
        _source(session, prefix, "off", enabled=False, next_crawl_at=None)
        session.commit()

        found = select_due_sources(session, NOW)
        found_ids = {source.id for source in found}
        assert due.id in found_ids
        assert all(source.external_key.startswith(prefix) for source in found if source.id == due.id)
        assert {source.external_key for source in found if source.external_key.startswith(prefix)} == {
            f"{prefix}-due"
        }
    finally:
        _cleanup(session, prefix)
        session.close()


def test_successful_crawl_stores_raw_and_schedules_six_hours_ahead() -> None:
    session = SessionLocal()
    try:
        source = _source(session, uuid.uuid4().hex, "board", next_crawl_at=None)
        envelope = _envelope(source)
        run = run_source_crawl(session, source, _returning(envelope), checked_at=NOW)
        assert run.status == "success"
        assert run.fetched_count == 1
        assert run.inserted_count == 1
        assert source.consecutive_failures == 0
        assert source.next_crawl_at == NOW + timedelta(minutes=ATS_INTERVAL_MINUTES)
        stored = session.scalars(select(RawJob).where(RawJob.source_id == source.id)).one()
        assert stored.payload["title"] == "Backend Engineer"
    finally:
        session.rollback()
        session.close()


def test_failed_crawl_does_not_expire_jobs() -> None:
    session = SessionLocal()
    try:
        source = _source(session, uuid.uuid4().hex, "board", next_crawl_at=NOW)
        job, link = _linked_job(session, source)
        run = run_source_crawl(session, source, _raising(ConnectorError("timeout")), checked_at=NOW)
        session.refresh(job)
        session.refresh(link)
        assert run.status == "failed"
        assert run.error_text == "timeout"
        assert job.status == "active"
        assert link.missing_count == 0
        assert link.active is True
        assert source.enabled is True
        assert source.consecutive_failures == 1
        assert source.next_crawl_at == NOW + timedelta(minutes=ATS_INTERVAL_MINUTES)
    finally:
        session.rollback()
        session.close()


def test_forbidden_source_is_disabled_without_expiring_jobs() -> None:
    session = SessionLocal()
    try:
        source = _source(session, uuid.uuid4().hex, "board")
        job, link = _linked_job(session, source)
        run = run_source_crawl(session, source, _raising(SourceForbidden("HTTP 403")), checked_at=NOW)
        session.refresh(job)
        session.refresh(link)
        assert run.status == "failed"
        assert source.enabled is False
        assert job.status == "active"
        assert link.active is True
    finally:
        session.rollback()
        session.close()


def test_scheduler_jobs_use_source_ids_not_company_names() -> None:
    session = SessionLocal()
    prefix = uuid.uuid4().hex
    scheduler = DueSourceScheduler(_returning(), concurrency=2)
    scheduler.scheduler.start(paused=True)
    try:
        due = _source(
            session,
            prefix,
            "due",
            company_name="Should Not Be A Job Id",
            next_crawl_at=NOW - timedelta(minutes=1),
        )
        later = _source(session, prefix, "later", next_crawl_at=NOW + timedelta(hours=3))
        _source(session, prefix, "off", enabled=False)
        session.commit()

        job_ids = scheduler.load(NOW)
        assert source_job_id(due.id) in job_ids
        assert source_job_id(later.id) in job_ids
        assert all("Should Not Be A Job Id" not in job_id for job_id in job_ids)
        assert scheduler.scheduler.get_job(source_job_id(due.id)) is not None
        later_job = scheduler.scheduler.get_job(source_job_id(later.id))
        assert later_job is not None
        assert later_job.next_run_time is not None
        assert later_job.next_run_time > NOW
        disabled_ids = [job_id for job_id in job_ids if job_id.endswith("off")]
        assert disabled_ids == []
    finally:
        scheduler.scheduler.shutdown(wait=False)
        _cleanup(session, prefix)
        session.close()


def test_scheduler_never_runs_more_than_the_concurrency_limit() -> None:
    session = SessionLocal()
    prefix = uuid.uuid4().hex
    current = 0
    peak = 0
    lock = threading.Lock()
    release = threading.Event()
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    parked = _park_other_sources(session, prefix)

    async def fetch(_source: Source) -> list[RawJobEnvelope]:
        nonlocal current, peak
        with lock:
            current += 1
            peak = max(peak, current)
        release.wait(timeout=5)
        with lock:
            current -= 1
        return []

    finished = 0
    jobs_done = threading.Event()

    def _job_finished(_event) -> None:
        nonlocal finished
        with lock:
            finished += 1
            if finished >= 4:
                jobs_done.set()

    scheduler = DueSourceScheduler(fetch, concurrency=2)
    scheduler.scheduler.add_listener(_job_finished, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
    scheduler.scheduler.start(paused=True)
    try:
        for name in ("a", "b", "c", "d"):
            _source(session, prefix, name, next_crawl_at=past)
        session.commit()
        scheduler.load(past)
        scheduler.scheduler.resume()

        deadline = time.time() + 5
        while time.time() < deadline:
            with lock:
                if current >= 2 and peak <= 2:
                    break
            time.sleep(0.02)
        time.sleep(0.3)
        with lock:
            assert peak == 2
            assert current <= 2
        release.set()
        assert jobs_done.wait(timeout=5)
    finally:
        release.set()
        jobs_done.wait(timeout=5)
        scheduler.scheduler.shutdown(wait=False)
        _restore_sources(session, parked)
        _cleanup(session, prefix)
        session.close()


def _source(
    session,
    prefix: str,
    name: str,
    *,
    company_name: str = "Example",
    enabled: bool = True,
    next_crawl_at: datetime | None = None,
) -> Source:
    source = Source(
        name=company_name,
        source_type="greenhouse",
        external_key=f"{prefix}-{name}",
        company_name=company_name,
        enabled=enabled,
        next_crawl_at=next_crawl_at,
    )
    session.add(source)
    session.flush()
    return source


def _linked_job(session, source: Source) -> tuple[Job, JobSource]:
    job = Job(
        title_original="Backend Engineer",
        company_name="Example",
        company_normalized="example",
        india_relevance="india",
        first_seen_at=NOW,
        last_seen_at=NOW,
        exact_fingerprint=uuid.uuid4().hex,
        status="active",
    )
    session.add(job)
    session.flush()
    link = JobSource(
        job_id=job.id,
        source_id=source.id,
        source_job_id="role-1",
        first_seen_at=NOW,
        last_seen_at=NOW,
    )
    session.add(link)
    session.flush()
    return job, link


def _envelope(source: Source) -> RawJobEnvelope:
    return RawJobEnvelope(
        source_type="greenhouse",
        source_external_key=source.external_key,
        source_job_id="role-1",
        source_url="https://job-boards.greenhouse.io/example/jobs/1",
        fetched_at=NOW,
        payload={"id": 1, "title": "Backend Engineer", "company_name": "Example"},
    )


def _returning(*envelopes: RawJobEnvelope):
    async def fetch(_source: Source) -> list[RawJobEnvelope]:
        return list(envelopes)

    return fetch


def _raising(exc: Exception):
    async def fetch(_source: Source) -> list[RawJobEnvelope]:
        raise exc

    return fetch


def _park_other_sources(session, prefix: str) -> list[tuple[uuid.UUID, datetime | None]]:
    others = session.scalars(select(Source).where(~Source.external_key.startswith(prefix))).all()
    parked = [(source.id, source.next_crawl_at) for source in others]
    later = datetime.now(timezone.utc) + timedelta(days=30)
    for source in others:
        source.next_crawl_at = later
    session.commit()
    return parked


def _restore_sources(session, parked: list[tuple[uuid.UUID, datetime | None]]) -> None:
    for source_id, next_crawl_at in parked:
        source = session.get(Source, source_id)
        if source is not None:
            source.next_crawl_at = next_crawl_at
    session.commit()


def _cleanup(session, prefix: str) -> None:
    source_ids = list(
        session.scalars(select(Source.id).where(Source.external_key.startswith(prefix)))
    )
    if not source_ids:
        session.rollback()
        return
    job_ids = list(
        session.scalars(select(JobSource.job_id).where(JobSource.source_id.in_(source_ids)))
    )
    session.execute(delete(JobSource).where(JobSource.source_id.in_(source_ids)))
    session.execute(delete(RawJob).where(RawJob.source_id.in_(source_ids)))
    session.execute(delete(CrawlRun).where(CrawlRun.source_id.in_(source_ids)))
    if job_ids:
        session.execute(delete(Job).where(Job.id.in_(job_ids)))
    session.execute(delete(Source).where(Source.id.in_(source_ids)))
    session.commit()
