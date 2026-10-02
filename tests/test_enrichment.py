import json
import uuid
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.db import SessionLocal
from app.models import AiBudget, AiCache, Job
from app.pipeline.enrichment import apply_enrichment, enrich_job, parse_enrichment
from app.schemas.enrichment import Enrichment

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)

VALID = {
    "normalized_title": "Backend Engineer",
    "role_category": "engineering",
    "seniority": "mid",
    "skills": ["Python"],
    "experience_min": 3,
    "experience_max": 5,
    "work_mode": "hybrid",
    "india_eligibility": "not_india",
}


def test_invalid_or_invented_enrichment_is_rejected() -> None:
    invented = {**VALID, "salary_min": 100000, "visa_sponsorship": True}
    with pytest.raises(ValidationError):
        Enrichment.model_validate(invented)
    assert parse_enrichment(json.dumps(invented)) is None
    assert parse_enrichment("not json") is None
    assert parse_enrichment(json.dumps({**VALID, "india_eligibility": "global"})) is None
    assert parse_enrichment(json.dumps({**VALID, "skills": "Python"})) is None
    assert parse_enrichment(json.dumps({**VALID, "experience_min": "five years"})) is None
    assert parse_enrichment(json.dumps({**VALID, "skills": [f"skill-{index}" for index in range(21)]})) is None
    assert parse_enrichment(json.dumps({**VALID, "experience_min": 8, "experience_max": 2})) is None


def test_empty_api_key_skips_the_provider() -> None:
    session = SessionLocal()
    provider = FakeProvider(json.dumps(VALID))
    try:
        job = _job(india_relevance="india")
        session.add(job)
        session.flush()
        assert enrich_job(session, job, provider, api_key="", model="openrouter/free", budget=40, now=NOW) is None
        assert provider.calls == 0
        assert job.ai_enriched is False
        assert job.salary_min is None
    finally:
        session.rollback()
        session.close()


def test_valid_enrichment_does_not_override_deterministic_india() -> None:
    session = SessionLocal()
    provider = FakeProvider(json.dumps(VALID))
    try:
        job = _job(india_relevance="india", work_mode="onsite")
        session.add(job)
        session.flush()
        result = enrich_job(
            session, job, provider, api_key="test-key", model="openrouter/free", budget=40, now=NOW
        )
        assert result is not None
        assert provider.calls == 1
        assert job.india_relevance == "india"
        assert job.title_normalized == "Backend Engineer"
        assert job.skills == ["Python"]
        assert job.work_mode == "hybrid"
        assert int(job.experience_min) == 3
        assert job.salary_min is None
        assert job.ai_enriched is True
        assert session.get(AiCache, _cache_key_for(job)) is not None
        assert session.get(AiBudget, NOW.date()).request_count == 1
    finally:
        session.rollback()
        session.close()


def test_unknown_relevance_can_be_filled_from_the_contract() -> None:
    session = SessionLocal()
    payload = {**VALID, "india_eligibility": "remote_india"}
    try:
        job = _job(india_relevance="unknown")
        session.add(job)
        session.flush()
        apply_enrichment(job, Enrichment.model_validate(payload))
        assert job.india_relevance == "remote_india"
    finally:
        session.rollback()
        session.close()


def test_cache_hit_does_not_spend_another_request() -> None:
    session = SessionLocal()
    provider = FakeProvider(json.dumps(VALID))
    try:
        first = _job(title="Backend Engineer", india_relevance="india")
        second = _job(title="backend   engineer", india_relevance="india")
        session.add_all([first, second])
        session.flush()
        enrich_job(session, first, provider, api_key="test-key", model="openrouter/free", budget=1, now=NOW)
        enrich_job(session, second, provider, api_key="test-key", model="openrouter/free", budget=1, now=NOW)
        assert provider.calls == 1
        assert second.ai_enriched is True
        assert session.get(AiBudget, NOW.date()).request_count == 1
    finally:
        session.rollback()
        session.close()


def test_spent_budget_and_invalid_response_leave_the_job_unchanged() -> None:
    session = SessionLocal()
    invalid = FakeProvider(json.dumps({**VALID, "salary_min": 100000}))
    try:
        job = _job(india_relevance="india")
        session.add(job)
        session.flush()
        assert (
            enrich_job(session, job, invalid, api_key="test-key", model="openrouter/free", budget=1, now=NOW)
            is None
        )
        assert job.ai_enriched is False
        assert job.skills == []
        assert session.get(AiBudget, NOW.date()).request_count == 1
        other = _job(title="Other role", india_relevance="unknown")
        session.add(other)
        session.flush()
        assert (
            enrich_job(session, other, invalid, api_key="test-key", model="openrouter/free", budget=1, now=NOW)
            is None
        )
        assert invalid.calls == 1
        assert other.india_relevance == "unknown"
    finally:
        session.rollback()
        session.close()


class FakeProvider:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = 0

    async def complete_json(self, *, system: str, user: str) -> str:
        self.calls += 1
        return self.content


def _job(
    *,
    title: str = "Backend Engineer",
    india_relevance: str,
    work_mode: str | None = None,
) -> Job:
    return Job(
        title_original=title,
        company_name="Example",
        company_normalized="example",
        description_text="Build services in Python.",
        city="Bengaluru",
        state="Karnataka",
        work_mode=work_mode,
        india_relevance=india_relevance,
        first_seen_at=NOW,
        last_seen_at=NOW,
        exact_fingerprint=uuid.uuid4().hex,
    )


def _cache_key_for(job: Job) -> str:
    from app.ai.cache import cache_key, normalized_input_text
    from app.ai.prompts import PROMPT_VERSION, TASK_TYPE

    text = normalized_input_text(job.title_original, "Bengaluru, Karnataka", job.description_text or "")
    return cache_key(TASK_TYPE, PROMPT_VERSION, text)
