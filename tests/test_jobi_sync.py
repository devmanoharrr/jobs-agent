import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.config import settings
from app.api.dashboard import _jobi_block
from app.db import SessionLocal
from app.main import app
from app.models import Job, JobiSync
from app.services.jobi_sync import BASE_URLS, JobiSyncError, base_url_for, transfer, waiting_count

NOW = datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc)
BASE = "http://jobi.test"


def test_targets_use_fixed_jobi_hosts() -> None:
    assert base_url_for("local") == "http://localhost:7400"
    assert base_url_for("prod") == "https://www.jobi.ai"
    with pytest.raises(JobiSyncError):
        base_url_for("elsewhere")


def test_waiting_is_separate_for_local_and_prod() -> None:
    session = SessionLocal()
    posted = uuid.uuid4().hex
    foreign = uuid.uuid4().hex
    missing_url = uuid.uuid4().hex
    try:
        before_local = waiting_count(session, "local")
        before_prod = waiting_count(session, "prod")
        session.add(_job(exact_fingerprint=posted))
        session.add(
            _job(
                exact_fingerprint=foreign,
                india_relevance="not_india",
                canonical_apply_url="https://boards.example/foreign",
            )
        )
        session.add(_job(exact_fingerprint=missing_url, canonical_apply_url=None))
        session.commit()
        assert waiting_count(session, "local") == before_local + 1
        assert waiting_count(session, "prod") == before_prod + 1

        session.add(JobiSync(target="local", external_key=posted, synced_at=NOW))
        session.commit()
        assert waiting_count(session, "local") == before_local
        assert waiting_count(session, "prod") == before_prod + 1
    finally:
        _cleanup(session, [posted, foreign, missing_url])
        session.close()


def test_transfer_posts_companies_before_jobs_and_keeps_skipped_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    fingerprint = uuid.uuid4().hex
    job = _job(exact_fingerprint=fingerprint)
    session = SessionLocal()
    monkeypatch.setattr(settings, "jobs_agent_token", "test-token")
    monkeypatch.setitem(BASE_URLS, "local", BASE)
    monkeypatch.setitem(BASE_URLS, "prod", BASE)
    monkeypatch.setattr("app.services.jobi_sync._postable", lambda _session: [job])
    monkeypatch.setattr("app.services.jobi_sync._forget_stale", lambda *_args, **_kwargs: None)
    try:
        with respx.mock:
            respx.post(f"{BASE}/internal/companies").mock(side_effect=_created)
            respx.post(f"{BASE}/internal/job-postings").mock(
                return_value=httpx.Response(
                    200,
                    json={"results": [{"action": "skipped", "external_key": fingerprint, "reason": "company_not_found"}]},
                )
            )
            respx.post(f"{BASE}/internal/job-postings/reconcile").mock(
                return_value=httpx.Response(200, json={"deactivated": 0})
            )
            message = transfer(session, target="local", publish=True)

            paths = [call.request.url.path for call in respx.calls]
            assert paths == ["/internal/companies", "/internal/job-postings", "/internal/job-postings/reconcile"]
            assert respx.calls[0].request.headers["authorization"] == "Bearer test-token"
            companies = json.loads(respx.calls[0].request.content)["companies"]
            assert companies == [
                {
                    "external_key": "stellar labs",
                    "name": "Stellar Labs",
                    "website_url": None,
                    "linkedin_url": None,
                    "industry": None,
                    "company_type": None,
                    "company_size_label": None,
                    "primary_location": "Bengaluru, KA, IN",
                    "short_description": None,
                    "description": None,
                    "logo_url": None,
                    "publish": True,
                }
            ]
            jobs = json.loads(respx.calls[1].request.content)["jobs"]
            assert jobs[0]["external_key"] == fingerprint
            assert jobs[0]["company_external_key"] == "stellar labs"
            assert jobs[0]["company_name"] == "Stellar Labs"
            assert jobs[0]["title"] == "Backend Engineer"
            assert jobs[0]["apply_url"] == "https://boards.example/jobs/123"
            assert jobs[0]["work_mode"] == "hybrid"
            assert jobs[0]["employment_type"] == "full_time"
            assert jobs[0]["salary_min"] == 800000
            assert jobs[0]["salary_max"] == 1600000.5
            assert jobs[0]["salary_currency"] == "INR"
            assert jobs[0]["salary_period"] == "year"
            assert jobs[0]["posted_at"] == "2026-09-28T10:00:00Z"
            assert jobs[0]["description_text"] == "Full description text"
            assert jobs[0]["responsibilities"] == []
            assert "description" not in jobs[0]
            assert "<" not in json.dumps(jobs[0])
            reconcile = json.loads(respx.calls[2].request.content)
            assert reconcile == {"active_external_keys": [fingerprint]}
        assert "skipped 1 (company_not_found 1)" in message
        session.expire_all()
        assert session.scalar(select(JobiSync).where(JobiSync.external_key == fingerprint)) is None
    finally:
        _cleanup(session, [fingerprint])
        session.close()


def test_accepted_job_is_not_sent_again(monkeypatch: pytest.MonkeyPatch) -> None:
    fingerprint = uuid.uuid4().hex
    job = _job(exact_fingerprint=fingerprint)
    session = SessionLocal()
    monkeypatch.setattr(settings, "jobs_agent_token", "test-token")
    monkeypatch.setitem(BASE_URLS, "local", BASE)
    monkeypatch.setitem(BASE_URLS, "prod", BASE)
    monkeypatch.setattr("app.services.jobi_sync._postable", lambda _session: [job])
    monkeypatch.setattr("app.services.jobi_sync._forget_stale", lambda *_args, **_kwargs: None)
    try:
        with respx.mock:
            respx.post(f"{BASE}/internal/companies").mock(side_effect=_created)
            respx.post(f"{BASE}/internal/job-postings").mock(side_effect=_created)
            respx.post(f"{BASE}/internal/job-postings/reconcile").mock(
                return_value=httpx.Response(200, json={"deactivated": 2})
            )
            first = transfer(session, target="prod", publish=False)
            assert json.loads(respx.calls[0].request.content)["companies"][0]["publish"] is False
            respx.calls.clear()
            second = transfer(session, target="prod", publish=False)
            paths = [call.request.url.path for call in respx.calls]

        assert "Jobs created 1" in first
        assert "Deactivated 2" in first
        assert paths == ["/internal/job-postings/reconcile"]
        assert "Nothing new to post" in second
        assert "deactivated 2" in second
        session.expire_all()
        row = session.scalar(select(JobiSync).where(JobiSync.external_key == fingerprint, JobiSync.target == "prod"))
        assert row is not None
    finally:
        _cleanup(session, [fingerprint])
        session.close()


def test_dashboard_offers_local_or_prod_posting() -> None:
    body = _jobi_block((2, 1))
    assert "Post to Jobi" in body
    assert "2 new jobs are waiting to post to Jobi local, and 1 new job is waiting to post to Jobi prod." in body
    assert "Start posting" in body
    assert 'action="/jobi/post"' in body
    assert 'name="base_url"' not in body
    assert 'value="local"' in body
    assert 'value="prod"' in body
    assert "http://localhost:7400" in body
    assert "https://www.jobi.ai" in body
    missing = _jobi_block((0, 0), ready=False)
    assert "alembic upgrade head" in missing


def test_unstructured_banner_mentions_the_key_only_when_it_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "openai_api_key", "")
    missing = _jobi_block((0, 0), unstructured=340)
    assert "340 postings are not structured yet. Scrape again after OPENAI_API_KEY is set." in missing

    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    present = _jobi_block((0, 0), unstructured=340)
    assert "340 postings are not structured yet. Scrape again to structure them." in present
    assert "OPENAI_API_KEY" not in present


def test_post_button_reports_the_transfer(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(session, *, target: str, publish: bool) -> str:
        assert target == "prod"
        assert publish is True
        return "Posted to Jobi prod."

    monkeypatch.setattr("app.api.dashboard.transfer", fake)
    client = TestClient(app)
    response = client.post(
        "/jobi/post",
        data={"target": "prod", "publish": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "Posted%20to%20Jobi%20prod." in response.headers["location"]


def test_post_button_rejects_an_unknown_target() -> None:
    client = TestClient(app)
    response = client.post("/jobi/post", data={"target": "elsewhere"}, follow_redirects=False)
    assert response.status_code == 303
    assert "Choose" in response.headers["location"]


def _created(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    rows = body.get("companies") or body.get("jobs") or []
    return httpx.Response(
        200,
        json={"results": [{"action": "created", "external_key": row.get("external_key")} for row in rows]},
    )


def _job(**overrides: object) -> Job:
    fields: dict[str, object] = {
        "title_original": "Backend Engineer",
        "company_name": "Stellar Labs",
        "company_normalized": "stellar labs",
        "description_text": "Full description text",
        "city": "Bengaluru",
        "state": "KA",
        "country_code": "IN",
        "work_mode": "hybrid",
        "employment_type": "full_time",
        "salary_min": Decimal("800000"),
        "salary_max": Decimal("1600000.50"),
        "india_relevance": "india",
        "status": "active",
        "posted_at": NOW,
        "first_seen_at": NOW,
        "last_seen_at": NOW,
        "exact_fingerprint": uuid.uuid4().hex,
        "canonical_apply_url": "https://boards.example/jobs/123",
    }
    fields.update(overrides)
    job = Job(**fields)
    if "structure_status" not in overrides and job.india_relevance in {"india", "remote_india"} and job.canonical_apply_url:
        job.structure_status = "ready"
        job.structured_payload = _sendable(job)
    return job


def _sendable(job: Job) -> dict:
    payload: dict[str, object] = {
        "external_key": job.exact_fingerprint,
        "company_external_key": job.company_normalized,
        "company_name": job.company_name,
        "title": job.title_original,
        "summary": None,
        "description_text": job.description_text,
        "role_category": None,
        "skills": [],
        "country_code": job.country_code or "IN",
        "experience_min_years": None,
        "experience_label": None,
        "apply_url": job.canonical_apply_url,
        "responsibilities": [],
        "requirements": [],
        "nice_to_have": [],
        "benefits": [],
    }
    if job.city:
        payload["city"] = job.city
    if job.state:
        payload["state"] = job.state
    if job.work_mode:
        payload["work_mode"] = job.work_mode
    if job.employment_type:
        payload["employment_type"] = job.employment_type
    if job.posted_at is not None:
        payload["posted_at"] = job.posted_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if job.salary_min is not None:
        payload["salary_min"] = _amount(job.salary_min)
        payload["salary_currency"] = "INR"
        payload["salary_period"] = "year"
    if job.salary_max is not None:
        payload["salary_max"] = _amount(job.salary_max)
    return payload


def _amount(value: Decimal) -> int | float:
    number = float(value)
    if number.is_integer():
        return int(number)
    return number


def _cleanup(session, fingerprints: list[str]) -> None:
    session.rollback()
    session.execute(delete(JobiSync).where(JobiSync.external_key.in_(fingerprints)))
    session.execute(delete(Job).where(Job.exact_fingerprint.in_(fingerprints)))
    session.commit()
