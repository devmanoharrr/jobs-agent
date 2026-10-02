import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.job import Job
from app.services.job_service import PAGE_SIZE_DEFAULT, get_published_job, list_jobs, stats

router = APIRouter()


class JobSummary(BaseModel):
    id: uuid.UUID
    title: str
    company_name: str
    city: str | None
    state: str | None
    work_mode: str | None
    india_relevance: str
    employment_type: str | None
    posted_at: datetime | None
    apply_url: str | None
    status: str


class JobDetail(JobSummary):
    description_text: str | None
    country_code: str | None
    last_seen_at: datetime


class JobListResponse(BaseModel):
    page: int
    page_size: int
    total: int
    jobs: list[JobSummary]


def _summary(job: Job) -> JobSummary:
    return JobSummary(
        id=job.id,
        title=job.title_original,
        company_name=job.company_name,
        city=job.city,
        state=job.state,
        work_mode=job.work_mode,
        india_relevance=job.india_relevance,
        employment_type=job.employment_type,
        posted_at=job.posted_at,
        apply_url=job.canonical_apply_url,
        status=job.status,
    )


@router.get("/jobs", response_model=JobListResponse)
def search_jobs(
    query: str | None = None,
    city: str | None = None,
    state: str | None = None,
    work_mode: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=PAGE_SIZE_DEFAULT, ge=1, le=100),
    db: Session = Depends(get_db),
) -> JobListResponse:
    total, rows = list_jobs(
        db,
        query=_blank_to_none(query),
        city=_blank_to_none(city),
        state=_blank_to_none(state),
        work_mode=_blank_to_none(work_mode),
        page=page,
        page_size=page_size,
    )
    return JobListResponse(
        page=page,
        page_size=page_size,
        total=total,
        jobs=[_summary(job) for job in rows],
    )


@router.get("/jobs/{job_id}", response_model=JobDetail)
def read_job(job_id: uuid.UUID, db: Session = Depends(get_db)) -> JobDetail:
    job = get_published_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    summary = _summary(job)
    return JobDetail(
        **summary.model_dump(),
        description_text=job.description_text,
        country_code=job.country_code,
        last_seen_at=job.last_seen_at,
    )


@router.get("/stats")
def read_stats(db: Session = Depends(get_db)) -> dict:
    return stats(db)


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None
