from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.base import JobConnector
from app.models.raw_job import RawJob
from app.models.source import Source
from app.schemas.canonical_job import CanonicalJobCandidate
from app.schemas.raw_job import RawJobEnvelope
from app.utils.hashing import hash_payload


def persist_raw_if_changed(session: Session, source: Source, raw: RawJobEnvelope) -> RawJob:
    """Insert a raw payload when its hash is new. Never overwrite an existing payload."""
    digest = hash_payload(raw.payload)
    existing = session.scalar(
        select(RawJob).where(
            RawJob.source_id == source.id,
            RawJob.source_job_id == raw.source_job_id,
            RawJob.payload_hash == digest,
        )
    )
    if existing is not None:
        return existing

    row = RawJob(
        source_id=source.id,
        source_job_id=raw.source_job_id,
        canonical_url=raw.source_url,
        payload=raw.payload,
        payload_hash=digest,
        fetched_at=raw.fetched_at,
    )
    session.add(row)
    session.flush()
    return row


def normalize_raw(connector: JobConnector, raw: RawJobEnvelope, row: RawJob) -> CanonicalJobCandidate | None:
    try:
        candidate = connector.normalize(raw)
    except Exception as exc:
        row.processing_status = "error"
        row.processing_error = str(exc)
        return None
    return candidate


def persisted_raw_ids(session: Session, source_id: UUID) -> list[UUID]:
    return list(session.scalars(select(RawJob.id).where(RawJob.source_id == source_id)))
