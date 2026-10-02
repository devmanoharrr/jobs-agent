"""Run one source crawl and decide when that source is due again.

Company names are not part of the schedule. A source is due when it is enabled
and next_crawl_at is empty or in the past. Failures are recorded and do not
expire jobs.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import settings
from app.connectors.base import ConnectorError, SourceForbidden, SourceNotFound
from app.connectors.registry import build_connector
from app.models.crawl_run import CrawlRun
from app.models.raw_job import RawJob
from app.models.source import Source
from app.pipeline.freshness import finalize_source_freshness
from app.pipeline.publish import publish_candidate
from app.pipeline.raw_store import normalize_raw, persist_raw_if_changed
from app.schemas.raw_job import RawJobEnvelope
from app.utils.hashing import hash_payload

ATS_TYPES = {"greenhouse", "lever", "ashby", "workable"}
SLOW_TYPES = {"adzuna", "generic"}
ATS_INTERVAL_MINUTES = 6 * 60
SLOW_INTERVAL_MINUTES = 12 * 60

Fetch = Callable[[Source], Awaitable[list[RawJobEnvelope]]]


def interval_minutes(source_type: str, configured: int | None = None) -> int:
    """ATS boards default to 6 hours. Adzuna and generic sites stay on a 12 hour floor."""
    if source_type in SLOW_TYPES:
        return max(configured or 0, SLOW_INTERVAL_MINUTES)
    if configured is not None:
        return configured
    if source_type in ATS_TYPES:
        return ATS_INTERVAL_MINUTES
    return SLOW_INTERVAL_MINUTES


def is_due(enabled: bool, next_crawl_at: datetime | None, now: datetime) -> bool:
    if not enabled:
        return False
    if next_crawl_at is None:
        return True
    return next_crawl_at <= now


def select_due_sources(session: Session, now: datetime) -> list[Source]:
    return list(
        session.scalars(
            select(Source)
            .where(
                Source.enabled.is_(True),
                or_(Source.next_crawl_at.is_(None), Source.next_crawl_at <= now),
            )
            .order_by(Source.next_crawl_at.asc().nullsfirst())
        )
    )


async def fetch_source(source: Source) -> list[RawJobEnvelope]:
    timeout = httpx.Timeout(settings.request_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout) as client:
        connector = build_connector(source.source_type, client, source.company_name)
        return await connector.fetch(source)


def run_source_crawl(
    session: Session,
    source: Source,
    fetch: Fetch,
    *,
    checked_at: datetime | None = None,
) -> CrawlRun:
    """Fetch one source, store raw payloads, and move next_crawl_at forward.

    The caller commits. A failed fetch leaves job status and miss counts unchanged.
    """
    checked_at = checked_at or datetime.now(timezone.utc)
    run = CrawlRun(source_id=source.id, started_at=checked_at, status="running")
    session.add(run)
    session.flush()

    try:
        envelopes = asyncio.run(fetch(source))
    except (SourceForbidden, SourceNotFound) as exc:
        _fail(session, source, run, checked_at, exc, disable=True)
        return run
    except Exception as exc:
        _fail(session, source, run, checked_at, exc, disable=False)
        return run

    inserted, updated, rejected = _store_raw(session, source, envelopes, checked_at=checked_at)
    seen_ids = {envelope.source_job_id for envelope in envelopes}
    finalize_source_freshness(
        session,
        source.id,
        seen_ids,
        crawl_status="success",
        checked_at=checked_at,
    )
    _schedule_next(source, checked_at, success=True)
    run.finished_at = checked_at
    run.status = "success"
    run.fetched_count = len(envelopes)
    run.inserted_count = inserted
    run.updated_count = updated
    run.rejected_count = rejected
    session.flush()
    return run


def _store_raw(
    session: Session,
    source: Source,
    envelopes: list[RawJobEnvelope],
    *,
    checked_at: datetime,
) -> tuple[int, int, int]:
    try:
        connector = build_connector(source.source_type, company_name=source.company_name)
    except ConnectorError:
        connector = None

    inserted = 0
    updated = 0
    rejected = 0
    for envelope in envelopes:
        digest = hash_payload(envelope.payload)
        already_stored = (
            session.scalar(
                select(RawJob.id).where(
                    RawJob.source_id == source.id,
                    RawJob.source_job_id == envelope.source_job_id,
                    RawJob.payload_hash == digest,
                )
            )
            is not None
        )
        row = persist_raw_if_changed(session, source, envelope)
        if already_stored:
            updated += 1
        else:
            inserted += 1
        if connector is None:
            rejected += 1
            continue
        candidate = normalize_raw(connector, envelope, row)
        if candidate is None:
            rejected += 1
            continue
        if publish_candidate(session, source, candidate, row, checked_at=checked_at) == "rejected":
            rejected += 1
    return inserted, updated, rejected


def _fail(
    session: Session,
    source: Source,
    run: CrawlRun,
    checked_at: datetime,
    exc: Exception,
    *,
    disable: bool,
) -> None:
    run.finished_at = checked_at
    run.status = "failed"
    run.error_text = str(exc)
    source.consecutive_failures += 1
    source.last_crawled_at = checked_at
    if disable:
        source.enabled = False
        metadata = dict(source.source_metadata or {})
        metadata["disabled_reason"] = str(exc)
        source.source_metadata = metadata
    _schedule_next(source, checked_at, success=False)
    session.flush()


def _schedule_next(source: Source, checked_at: datetime, *, success: bool) -> None:
    minutes = interval_minutes(source.source_type, source.crawl_interval_minutes)
    source.crawl_interval_minutes = minutes
    source.last_crawled_at = checked_at
    source.next_crawl_at = checked_at + timedelta(minutes=minutes)
    if success:
        source.last_success_at = checked_at
        source.consecutive_failures = 0


def source_job_id(source_id: uuid.UUID) -> str:
    return f"source:{source_id}"
