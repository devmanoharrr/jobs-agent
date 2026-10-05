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
            assert jobs[0]["apply_url"] == "https://boards.example/jobs/123"
            assert jobs[0]["work_mode"] == "hybrid"
            assert jobs[0]["salary_min"] == 800000
            assert jobs[0]["salary_max"] == 1600000.5
            assert jobs[0]["posted_at"] == "2026-09-28T10:00:00Z"
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
    return Job(**fields)


def _cleanup(session, fingerprints: list[str]) -> None:
    session.rollback()
    session.execute(delete(JobiSync).where(JobiSync.external_key.in_(fingerprints)))
    session.execute(delete(Job).where(Job.exact_fingerprint.in_(fingerprints)))
    session.commit()
