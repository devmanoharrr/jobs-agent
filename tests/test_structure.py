import json
import uuid
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.db import SessionLocal
from app.main import app
from app.models import Job, RawJob, Source
from app.pipeline.structure import parse_salary, structure_posting

NOW = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)

FACTS = {
    "summary": "Threat-model on-premises AI systems for a bank. The role sits in cybersecurity.",
    "role_category": "Engineering",
    "skills": ["Python", "Invented Skill"],
    "work_mode": "remote",
    "employment_type": "contract",
    "experience_min_years": 5,
    "experience_label": "5+ years",
    "responsibilities": ["<p>Lead threat modeling.</p>"],
    "requirements": ["5+ years in offensive security."],
    "nice_to_have": [],
    "benefits": ["Market salary."],
    "description_text": "Lead threat modeling. 5+ years in offensive security. Market salary.",
}

POSTING = (
    "Lead threat modeling. Strong Python. 5+ years in offensive security. "
    "This is a contract role in Hyderabad. Onsite. Market salary. 15-20 LPA."
)


class FakeProvider:
    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.calls = 0

    async def complete_json(self, *, system: str, user: str) -> str:
        self.calls += 1
        self.user = user
        return self.payload


def test_salary_parser_reads_explicit_inr_and_ignores_years() -> None:
    parsed = parse_salary("Offer is 15-20 LPA plus 5+ years of Python.")
    assert parsed == {
        "salary_min": 1500000,
        "salary_max": 2000000,
        "salary_currency": "INR",
        "salary_period": "year",
    }
    monthly = parse_salary("Pay is ₹80,000 per month.")
    assert monthly == {"salary_min": 80000, "salary_currency": "INR", "salary_period": "month"}
    assert parse_salary("Competitive salary and 5+ years.") is None


def test_gpt_structures_a_posting_and_records_the_trail() -> None:
    session = SessionLocal()
    provider = FakeProvider(json.dumps(FACTS))
    try:
        source, raw, job = _stored(session)
        status = structure_posting(
            session,
            job,
            raw,
            source,
            checked_at=NOW,
            provider=provider,
            api_key="test-key",
            model="gpt-5",
            budget=1000,
        )
        assert status == "ready"
        assert provider.calls == 1
        assert "<" not in provider.user
        payload = job.structured_payload
        assert payload["external_key"] == job.exact_fingerprint
        assert payload["company_external_key"] == job.company_normalized
        assert payload["title"] == "AI Cybersecurity Engineer"
        assert payload["apply_url"].startswith("https://")
        assert payload["skills"] == ["Python"]
        assert payload["work_mode"] == "onsite"
        assert payload["employment_type"] == "contract"
        assert payload["city"] == "Hyderabad"
        assert payload["state"] == "Telangana"
        assert payload["country_code"] == "IN"
        assert payload["experience_min_years"] == 5
        assert payload["experience_label"] == "5+ years"
        assert payload["responsibilities"] == ["Lead threat modeling."]
        assert payload["description_text"] is None
        assert payload["salary_min"] == 1500000
        assert payload["salary_max"] == 2000000
        assert payload["salary_currency"] == "INR"
        assert payload["salary_period"] == "year"
        assert "<" not in json.dumps(payload)
        steps = [item["step"] for item in job.pipeline_trace]
        assert steps == ["fetched", "normalized", "model", "checked", "saved"]
        assert job.pipeline_trace[0]["payload"] == {"id": "role-1"}
        assert "Invented Skill" in " ".join(job.pipeline_trace[3]["dropped"])

        again = structure_posting(
            session,
            job,
            raw,
            source,
            checked_at=NOW,
            provider=provider,
            api_key="test-key",
            model="gpt-5",
            budget=1000,
        )
        assert again == "ready"
        assert provider.calls == 1
    finally:
        _cleanup(session)
        session.close()


def test_missing_api_key_keeps_the_posting_pending() -> None:
    session = SessionLocal()
    provider = FakeProvider(json.dumps(FACTS))
    try:
        source, raw, job = _stored(session)
        status = structure_posting(
            session,
            job,
            raw,
            source,
            checked_at=NOW,
            provider=provider,
            api_key="",
            budget=5,
        )
        assert status == "pending"
        assert provider.calls == 0
        assert job.structured_payload is None
        assert job.pipeline_trace[2]["note"].startswith("OPENAI_API_KEY")
    finally:
        _cleanup(session)
        session.close()


def test_trace_endpoint_returns_the_saved_steps() -> None:
    session = SessionLocal()
    client = TestClient(app)
    fingerprint = uuid.uuid4().hex
    try:
        job = Job(
            title_original="Trail Engineer",
            company_name="Example",
            company_normalized="example",
            india_relevance="india",
            status="active",
            first_seen_at=NOW,
            last_seen_at=NOW,
            exact_fingerprint=fingerprint,
            canonical_apply_url="https://example.com/apply",
            structure_status="ready",
            pipeline_trace=[{"step": "fetched", "source_url": "https://example.com/job"}],
        )
        session.add(job)
        session.commit()
        response = client.get(f"/postings/{job.id}/trace")
        assert response.status_code == 200
        body = response.json()
        assert body["structure_status"] == "ready"
        assert body["steps"][0]["step"] == "fetched"
        page = client.get(f"/postings/{job.id}")
        assert page.status_code == 200
        assert "What happened" in page.text
    finally:
        session.execute(delete(Job).where(Job.exact_fingerprint == fingerprint))
        session.commit()
        session.close()


def _stored(session):
    source = Source(
        name="Xenon7",
        source_type="workable",
        external_key=f"xenon7-{uuid.uuid4().hex[:8]}",
        company_name="Xenon7",
    )
    session.add(source)
    session.flush()
    raw = RawJob(
        source_id=source.id,
        source_job_id="role-1",
        canonical_url="https://apply.workable.com/xenon7/j/1",
        payload={"id": "role-1"},
        payload_hash=uuid.uuid4().hex,
        fetched_at=NOW,
    )
    session.add(raw)
    session.flush()
    job = Job(
        title_original="AI Cybersecurity Engineer",
        company_name="Xenon7",
        company_normalized="xenon7",
        description_text=f"<p>{POSTING}</p>",
        city="Hyderabad",
        india_relevance="india",
        status="active",
        first_seen_at=NOW,
        last_seen_at=NOW,
        exact_fingerprint=uuid.uuid4().hex,
        canonical_apply_url="https://apply.workable.com/j/C4EEDB56AD/apply",
        canonical_source_id=source.id,
    )
    session.add(job)
    session.flush()
    return source, raw, job


def _cleanup(session) -> None:
    session.rollback()
