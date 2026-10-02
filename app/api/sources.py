import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.services.job_service import list_source_runs, list_sources

router = APIRouter()


class SourceSummary(BaseModel):
    id: uuid.UUID
    name: str
    source_type: str
    external_key: str
    company_name: str | None
    enabled: bool
    last_crawled_at: datetime | None
    last_success_at: datetime | None
    next_crawl_at: datetime | None
    consecutive_failures: int


class CrawlRunSummary(BaseModel):
    id: uuid.UUID
    source_id: uuid.UUID
    started_at: datetime
    finished_at: datetime | None
    status: str
    fetched_count: int
    inserted_count: int
    updated_count: int
    rejected_count: int
    error_text: str | None


@router.get("/sources", response_model=list[SourceSummary])
def read_sources(db: Session = Depends(get_db)) -> list[SourceSummary]:
    return [
        SourceSummary(
            id=source.id,
            name=source.name,
            source_type=source.source_type,
            external_key=source.external_key,
            company_name=source.company_name,
            enabled=source.enabled,
            last_crawled_at=source.last_crawled_at,
            last_success_at=source.last_success_at,
            next_crawl_at=source.next_crawl_at,
            consecutive_failures=source.consecutive_failures,
        )
        for source in list_sources(db)
    ]


@router.get("/sources/{source_id}/runs", response_model=list[CrawlRunSummary])
def read_source_runs(source_id: uuid.UUID, db: Session = Depends(get_db)) -> list[CrawlRunSummary]:
    runs = list_source_runs(db, source_id)
    if runs is None:
        raise HTTPException(status_code=404, detail="Source not found")
    return [
        CrawlRunSummary(
            id=run.id,
            source_id=run.source_id,
            started_at=run.started_at,
            finished_at=run.finished_at,
            status=run.status,
            fetched_count=run.fetched_count,
            inserted_count=run.inserted_count,
            updated_count=run.updated_count,
            rejected_count=run.rejected_count,
            error_text=run.error_text,
        )
        for run in runs
    ]
