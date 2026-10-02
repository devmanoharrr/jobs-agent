"""Turn a normalized posting into one canonical job.

not_india postings are recorded on the raw row and are not published.
A repeated source_job_id updates the same job. Exact and fuzzy duplicates
attach another source instead of inserting a second row.
"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.connectors.base import JobConnector
from app.models.job import Job
from app.models.job_source import JobSource
from app.models.raw_job import RawJob
from app.models.source import Source
from app.pipeline.dedupe import (
    JobIdentity,
    exact_fingerprint,
    find_duplicate,
    fuzzy_fingerprint,
    source_rank,
)
from app.pipeline.enrichment import enrich_job
from app.pipeline.india_filter import IndiaRelevance, classify_india
from app.pipeline.normalize import normalize_company, normalize_title, normalize_url
from app.pipeline.raw_store import normalize_raw
from app.schemas.canonical_job import CanonicalJobCandidate
from app.schemas.raw_job import RawJobEnvelope

_INDIA_COUNTRY = {"india", "in", "bharat"}


def publish_candidate(
    session: Session,
    source: Source,
    candidate: CanonicalJobCandidate,
    raw_row: RawJob,
    *,
    checked_at: datetime,
) -> str:
    """Return created, updated, or rejected. The caller commits."""
    relevance = classify_india(candidate, country_code=_country_code(candidate.locations_raw))
    if relevance is IndiaRelevance.NOT_INDIA:
        raw_row.processing_status = "rejected"
        raw_row.processing_error = "not_india"
        return "rejected"

    identity = _identity(candidate, source)
    job = _find_existing(session, source, candidate, identity)
    created = job is None
    if job is None:
        job = Job(
            title_original=candidate.title_original,
            company_name=candidate.company_name,
            company_normalized=normalize_company(candidate.company_name),
            india_relevance=relevance.value,
            first_seen_at=checked_at,
            last_seen_at=checked_at,
            exact_fingerprint=exact_fingerprint(identity),
        )
        session.add(job)
        session.flush()

    if created or _should_replace(session, job, source):
        _apply_content(job, candidate, source, identity, relevance, checked_at)
    else:
        job.last_seen_at = checked_at
        job.status = "active"
    _attach_source(session, job, source, candidate, raw_row, checked_at)
    raw_row.processing_status = "processed"
    raw_row.processing_error = None
    session.flush()
    _enrich_unknown(session, job, relevance, checked_at)
    return "created" if created else "updated"


def publish_latest_raw(session: Session, source: Source, connector: JobConnector, *, checked_at: datetime) -> dict[str, int]:
    """Publish the newest stored payload for each source job id. Does not expire anything."""
    counts = {"created": 0, "updated": 0, "rejected": 0}
    seen: set[str] = set()
    rows = session.scalars(
        select(RawJob)
        .where(RawJob.source_id == source.id)
        .order_by(RawJob.fetched_at.desc(), RawJob.id.desc())
    ).all()
    for row in rows:
        if row.source_job_id in seen:
            continue
        seen.add(row.source_job_id)
        envelope = RawJobEnvelope(
            source_type=source.source_type,
            source_external_key=source.external_key,
            source_job_id=row.source_job_id,
            source_url=row.canonical_url,
            fetched_at=row.fetched_at,
            payload=row.payload,
        )
        candidate = normalize_raw(connector, envelope, row)
        if candidate is None:
            counts["rejected"] += 1
            continue
        counts[publish_candidate(session, source, candidate, row, checked_at=checked_at)] += 1
    return counts


def _find_existing(
    session: Session,
    source: Source,
    candidate: CanonicalJobCandidate,
    identity: JobIdentity,
) -> Job | None:
    link = session.scalar(
        select(JobSource).where(
            JobSource.source_id == source.id,
            JobSource.source_job_id == candidate.source_job_id,
        )
    )
    if link is not None:
        return session.get(Job, link.job_id)

    fingerprint = exact_fingerprint(identity)
    existing = session.scalar(select(Job).where(Job.exact_fingerprint == fingerprint))
    if existing is not None:
        return existing

    target_url = normalize_url(candidate.apply_url)
    if target_url is not None:
        for job in session.scalars(select(Job).where(Job.canonical_apply_url.is_not(None))).all():
            if normalize_url(job.canonical_apply_url) == target_url:
                return job

    company = normalize_company(candidate.company_name)
    rows = list(session.scalars(select(Job).where(Job.company_normalized == company)).all())
    identities = [_identity_from_job(job) for job in rows]
    match = find_duplicate(identity, identities)
    if match is None:
        return None
    for job, item in zip(rows, identities, strict=True):
        if item == match:
            return job
    return None


def _apply_content(
    job: Job,
    candidate: CanonicalJobCandidate,
    source: Source,
    identity: JobIdentity,
    relevance: IndiaRelevance,
    checked_at: datetime,
) -> None:
    city, state, country = _place(candidate.locations_raw)
    job.title_original = candidate.title_original
    job.title_normalized = normalize_title(candidate.title_original) or None
    job.company_name = candidate.company_name
    job.company_normalized = normalize_company(candidate.company_name)
    job.description_text = candidate.description_text
    job.description_html = candidate.description_html
    job.city = city
    job.state = state
    job.country_code = country
    job.work_mode = _work_mode(identity.location)
    job.india_relevance = relevance.value
    job.employment_type = candidate.employment_type
    job.posted_at = candidate.posted_at
    job.expires_at = candidate.expires_at
    job.last_seen_at = checked_at
    job.status = "active"
    job.canonical_source_id = source.id
    job.canonical_apply_url = candidate.apply_url
    job.exact_fingerprint = exact_fingerprint(identity)
    description_fp = fuzzy_fingerprint(candidate.description_text)
    job.fuzzy_fingerprint = description_fp or None


def _attach_source(
    session: Session,
    job: Job,
    source: Source,
    candidate: CanonicalJobCandidate,
    raw_row: RawJob,
    checked_at: datetime,
) -> None:
    link = session.scalar(
        select(JobSource).where(
            JobSource.job_id == job.id,
            JobSource.source_id == source.id,
            JobSource.source_job_id == candidate.source_job_id,
        )
    )
    if link is None:
        session.add(
            JobSource(
                job_id=job.id,
                source_id=source.id,
                source_job_id=candidate.source_job_id,
                source_url=candidate.apply_url,
                raw_job_id=raw_row.id,
                first_seen_at=checked_at,
                last_seen_at=checked_at,
            )
        )
        return
    link.source_url = candidate.apply_url
    link.raw_job_id = raw_row.id
    link.last_seen_at = checked_at
    link.missing_count = 0
    link.active = True


def _should_replace(session: Session, job: Job, source: Source) -> bool:
    if job.canonical_source_id is None or job.canonical_source_id == source.id:
        return True
    current = session.get(Source, job.canonical_source_id)
    current_type = current.source_type if current is not None else "generic"
    return source_rank(source.source_type) < source_rank(current_type)


def _enrich_unknown(session: Session, job: Job, relevance: IndiaRelevance, checked_at: datetime) -> None:
    if relevance not in {IndiaRelevance.UNKNOWN, IndiaRelevance.UNKNOWN_REMOTE}:
        return
    key = settings.openrouter_api_key.strip()
    if not key:
        return
    from app.ai.openrouter import OpenRouterProvider

    enrich_job(
        session,
        job,
        OpenRouterProvider(api_key=key),
        api_key=key,
        model=settings.openrouter_model,
        budget=settings.openrouter_daily_budget,
        now=checked_at,
    )


def _identity(candidate: CanonicalJobCandidate, source: Source) -> JobIdentity:
    return JobIdentity(
        company_name=candidate.company_name,
        title=candidate.title_original,
        location=" | ".join(candidate.locations_raw),
        description=candidate.description_text,
        canonical_url=candidate.apply_url,
        source_type=source.source_type,
        source_id=str(source.id),
        source_job_id=candidate.source_job_id,
    )


def _identity_from_job(job: Job) -> JobIdentity:
    location = ", ".join(part for part in (job.city, job.state, job.country_code) if part)
    return JobIdentity(
        company_name=job.company_name,
        title=job.title_original,
        location=location,
        description=job.description_text,
        canonical_url=job.canonical_apply_url,
        source_type="greenhouse",
        source_id=str(job.canonical_source_id) if job.canonical_source_id else None,
    )


def _place(locations: list[str]) -> tuple[str | None, str | None, str | None]:
    if not locations:
        return None, None, None
    parts = [part.strip() for part in locations[0].split(",") if part.strip()]
    country = "IN" if parts and parts[-1].lower() in _INDIA_COUNTRY else None
    if country is not None:
        parts = parts[:-1]
    city = parts[0] if parts else None
    state = parts[1] if len(parts) > 1 else None
    return city, state, country


def _country_code(locations: list[str]) -> str | None:
    return _place(locations)[2]


def _work_mode(location: str) -> str | None:
    text = location.lower()
    if "hybrid" in text:
        return "hybrid"
    if "remote" in text:
        return "remote"
    return None
