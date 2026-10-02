"""Optional enrichment. Known ATS ingestion does not need this module.

An empty API key, a cache hit, or a spent daily budget never calls the model.
Invalid JSON is discarded. Deterministic India relevance is left unchanged.
"""

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.ai.budget import try_spend
from app.ai.cache import cache_key, get_cached, normalized_input_text
from app.ai.prompts import PROMPT_VERSION, SYSTEM_PROMPT, TASK_TYPE, user_prompt
from app.ai.provider import AIProvider
from app.models.ai_cache import AiCache
from app.models.job import Job
from app.schemas.enrichment import Enrichment

_AI_MAY_CLASSIFY = {"unknown", "unknown_remote"}


def parse_enrichment(raw: str) -> Enrichment | None:
    try:
        return Enrichment.model_validate_json(raw)
    except (ValidationError, ValueError):
        return None


def apply_enrichment(job: Job, enrichment: Enrichment) -> None:
    """Copy contract fields onto existing columns. Null means leave the job alone."""
    if enrichment.normalized_title:
        job.title_normalized = enrichment.normalized_title.strip()
    if enrichment.skills:
        job.skills = list(enrichment.skills)
    if enrichment.experience_min is not None:
        job.experience_min = Decimal(str(enrichment.experience_min))
    if enrichment.experience_max is not None:
        job.experience_max = Decimal(str(enrichment.experience_max))
    if enrichment.work_mode is not None:
        job.work_mode = enrichment.work_mode.value
    if job.india_relevance in _AI_MAY_CLASSIFY and enrichment.india_eligibility.value != "unknown":
        job.india_relevance = enrichment.india_eligibility.value
    job.ai_enriched = True
    job.enrichment_version = PROMPT_VERSION


def enrich_job(
    session: Session,
    job: Job,
    provider: AIProvider,
    *,
    api_key: str,
    model: str,
    budget: int,
    now: datetime | None = None,
) -> Enrichment | None:
    if not api_key or job.ai_enriched:
        return None

    now = now or datetime.now(timezone.utc)
    title = job.title_original
    location = ", ".join(part for part in (job.city, job.state, job.country_code) if part)
    description = job.description_text or ""
    text = normalized_input_text(title, location, description)
    key = cache_key(TASK_TYPE, PROMPT_VERSION, text)
    cached = get_cached(session, key)
    if cached is not None:
        try:
            enrichment = Enrichment.model_validate(cached)
        except ValidationError:
            enrichment = None
        if enrichment is not None:
            apply_enrichment(job, enrichment)
            return enrichment

    if not try_spend(session, now.date(), budget):
        return None

    try:
        raw = asyncio.run(
            provider.complete_json(
                system=SYSTEM_PROMPT,
                user=user_prompt(title, location, description),
            )
        )
    except Exception:
        return None

    enrichment = parse_enrichment(raw)
    if enrichment is None:
        return None

    session.add(
        AiCache(
            cache_key=key,
            task_type=TASK_TYPE,
            model=model,
            prompt_version=PROMPT_VERSION,
            response_json=enrichment.model_dump(mode="json"),
            created_at=now,
        )
    )
    apply_enrichment(job, enrichment)
    session.flush()
    return enrichment
