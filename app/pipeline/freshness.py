"""Seen and missing bookkeeping for one source crawl.

A source association expires only after two successful crawls omit its
source_job_id. Failed, partial, and incomplete crawls leave every association
and job status untouched.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.job import Job
from app.models.job_source import JobSource

MISS_THRESHOLD = 2
SUCCESS_STATUS = "success"


@dataclass(frozen=True)
class SourceLink:
    source_job_id: str
    missing_count: int = 0
    active: bool = True


def counts_as_successful_crawl(crawl_status: str) -> bool:
    return crawl_status == SUCCESS_STATUS


def apply_source_crawl(
    links: Sequence[SourceLink],
    seen_ids: set[str],
    *,
    crawl_status: str,
) -> tuple[SourceLink, ...]:
    """Return updated associations. Unsuccessful crawls are a no-op."""
    if not counts_as_successful_crawl(crawl_status):
        return tuple(links)

    updated: list[SourceLink] = []
    for link in links:
        if link.source_job_id in seen_ids:
            updated.append(SourceLink(source_job_id=link.source_job_id, missing_count=0, active=True))
        elif link.active:
            missing_count = link.missing_count + 1
            updated.append(
                SourceLink(
                    source_job_id=link.source_job_id,
                    missing_count=missing_count,
                    active=missing_count < MISS_THRESHOLD,
                )
            )
        else:
            updated.append(link)
    return tuple(updated)


def status_for_associations(links: Sequence[SourceLink]) -> str:
    """A job stays active while any source association is still active."""
    if any(link.active for link in links):
        return "active"
    return "expired"


def finalize_source_freshness(
    session: Session,
    source_id: uuid.UUID,
    seen_ids: set[str],
    *,
    crawl_status: str,
    checked_at: datetime,
) -> None:
    """Apply one crawl to stored source associations and refresh job status.

    Rows are updated in place. Raw history and source associations are never deleted.
    """
    if not counts_as_successful_crawl(crawl_status):
        return

    rows = session.scalars(select(JobSource).where(JobSource.source_id == source_id)).all()
    for row in rows:
        updated = apply_source_crawl(
            [SourceLink(row.source_job_id, row.missing_count, row.active)],
            seen_ids,
            crawl_status=crawl_status,
        )[0]
        row.missing_count = updated.missing_count
        row.active = updated.active
        if row.source_job_id in seen_ids:
            row.last_seen_at = checked_at

    affected_job_ids = {row.job_id for row in rows}
    for job_id in affected_job_ids:
        job = session.get(Job, job_id)
        if job is None:
            continue
        associations = session.scalars(select(JobSource).where(JobSource.job_id == job_id)).all()
        job.status = status_for_associations(
            [SourceLink(item.source_job_id, item.missing_count, item.active) for item in associations]
        )
        job.last_checked_at = checked_at
        if any(item.source_id == source_id and item.source_job_id in seen_ids for item in associations):
            job.last_seen_at = checked_at
    session.flush()
