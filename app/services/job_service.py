"""Read published jobs and crawl totals.

The list is active India and remote-India jobs only. Order is posted_at descending,
then id, so a page does not shuffle between requests.
"""

import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.crawl_run import CrawlRun
from app.models.job import Job
from app.models.source import Source

PAGE_SIZE_DEFAULT = 20
_PUBLISHED = ("india", "remote_india")


def list_jobs(
    session: Session,
    *,
    query: str | None,
    city: str | None,
    state: str | None,
    work_mode: str | None,
    page: int,
    page_size: int,
    company: str | None = None,
) -> tuple[int, list[Job]]:
    statement = select(Job).where(Job.status == "active", Job.india_relevance.in_(_PUBLISHED))
    if company:
        statement = statement.where(func.lower(Job.company_name) == company.strip().lower())
    if query:
        pattern = f"%{_escape_like(query)}%"
        statement = statement.where(
            or_(
                Job.title_original.ilike(pattern, escape="\\"),
                Job.company_name.ilike(pattern, escape="\\"),
                Job.description_text.ilike(pattern, escape="\\"),
            )
        )
    if city:
        statement = statement.where(func.lower(Job.city) == city.strip().lower())
    if state:
        statement = statement.where(func.lower(Job.state) == state.strip().lower())
    if work_mode:
        statement = statement.where(func.lower(Job.work_mode) == work_mode.strip().lower())

    total = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
    rows = session.scalars(
        statement.order_by(Job.posted_at.desc().nulls_last(), Job.id.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return total, list(rows)


def get_published_job(session: Session, job_id: uuid.UUID) -> Job | None:
    return session.scalar(
        select(Job).where(
            Job.id == job_id,
            Job.status == "active",
            Job.india_relevance.in_(_PUBLISHED),
        )
    )


def stats(session: Session) -> dict:
    relevance_rows = session.execute(
        select(Job.india_relevance, func.count()).group_by(Job.india_relevance)
    ).all()
    status_rows = session.execute(select(Job.status, func.count()).group_by(Job.status)).all()
    crawl = session.execute(
        select(
            func.count().filter(CrawlRun.status == "success"),
            func.count().filter(CrawlRun.status == "failed"),
            func.coalesce(func.sum(CrawlRun.fetched_count), 0),
            func.coalesce(func.sum(CrawlRun.inserted_count), 0),
            func.coalesce(func.sum(CrawlRun.updated_count), 0),
            func.coalesce(func.sum(CrawlRun.rejected_count), 0),
        )
    ).one()
    enabled = session.scalar(select(func.count()).select_from(Source).where(Source.enabled.is_(True))) or 0
    disabled = session.scalar(select(func.count()).select_from(Source).where(Source.enabled.is_(False))) or 0
    return {
        "jobs": {
            "active": _count_for(status_rows, "active"),
            "expired": _count_for(status_rows, "expired"),
            "by_india_relevance": {str(name): int(count) for name, count in relevance_rows},
        },
        "sources": {"enabled": enabled, "disabled": disabled},
        "crawl_runs": {
            "success": int(crawl[0]),
            "failed": int(crawl[1]),
            "fetched": int(crawl[2]),
            "inserted": int(crawl[3]),
            "updated": int(crawl[4]),
            "rejected": int(crawl[5]),
        },
    }


def published_companies(session: Session) -> list[str]:
    rows = session.scalars(
        select(Job.company_name)
        .where(Job.status == "active", Job.india_relevance.in_(_PUBLISHED))
        .distinct()
        .order_by(Job.company_name.asc())
    )
    return [name for name in rows if name]


def list_sources(session: Session) -> list[Source]:
    return list(session.scalars(select(Source).order_by(Source.source_type.asc(), Source.external_key.asc())))


def recent_failures(session: Session, *, limit: int = 20) -> list[tuple[CrawlRun, Source]]:
    rows = session.execute(
        select(CrawlRun, Source)
        .join(Source, CrawlRun.source_id == Source.id)
        .where(CrawlRun.status == "failed")
        .order_by(CrawlRun.started_at.desc(), CrawlRun.id.asc())
        .limit(limit)
    ).all()
    return [(run, source) for run, source in rows]


def list_source_runs(session: Session, source_id: uuid.UUID, *, limit: int = 20) -> list[CrawlRun] | None:
    if session.get(Source, source_id) is None:
        return None
    return list(
        session.scalars(
            select(CrawlRun)
            .where(CrawlRun.source_id == source_id)
            .order_by(CrawlRun.started_at.desc(), CrawlRun.id.asc())
            .limit(limit)
        )
    )


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _count_for(rows: list[tuple[str, int]], key: str) -> int:
    for name, count in rows:
        if name == key:
            return int(count)
    return 0
