import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
import respx
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.api.dashboard import _page
from app.services.demo_actions import probe_candidates
from app.connectors.greenhouse import JOBS_URL
from app.db import SessionLocal
from app.main import app
from app.models import CrawlRun, Job, Source

TOKEN = "dashboard-token"
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


def test_dashboard_shows_sources_counts_jobs_and_failures() -> None:
    session = SessionLocal()
    client = TestClient(app)
    try:
        source = session.scalar(select(Source).where(Source.external_key == "groww"))
        assert source is not None
        session.add(
            Job(
                title_original=f"<script>{TOKEN}</script>",
                company_name="Example",
                company_normalized="example",
                city="Bengaluru",
                work_mode="onsite",
                india_relevance="india",
                status="active",
                posted_at=NOW,
                first_seen_at=NOW,
                last_seen_at=NOW,
                exact_fingerprint=uuid.uuid4().hex,
                canonical_apply_url="https://example.com/apply",
            )
        )
        session.add(
            Job(
                title_original=f"Hidden {TOKEN}",
                company_name="Example",
                company_normalized="example",
                city="Athens",
                india_relevance="not_india",
                status="active",
                posted_at=NOW,
                first_seen_at=NOW,
                last_seen_at=NOW,
                exact_fingerprint=uuid.uuid4().hex,
            )
        )
        session.add(
            CrawlRun(
                source_id=source.id,
                started_at=NOW,
                status="failed",
                error_text=f"{TOKEN} boom",
            )
        )
        session.commit()

        response = client.get("/")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        body = response.text
        assert "Groww" in body
        assert "CRED" in body
        assert "Temporal" in body
        assert "Epignosis" in body
        assert "Sources enabled" in body
        assert f"{TOKEN} boom" in body
        assert "&lt;script&gt;" in body
        assert "<script>" not in body.split("<style>", 1)[-1]
        assert "Hidden" not in body
        assert 'href="https://example.com/apply"' in body
        assert "Add source" in body
        assert "Fetch sources" in body
        assert "Next" in body
        page_two = client.get("/?page=2")
        assert page_two.status_code == 200
        assert "Previous" in page_two.text
        assert "&lt;script&gt;" not in page_two.text

        published = session.scalar(
            select(Job).where(Job.title_original.contains(TOKEN), Job.india_relevance == "india")
        )
        hidden = session.scalar(
            select(Job).where(Job.title_original.contains(TOKEN), Job.india_relevance == "not_india")
        )
        assert published is not None and hidden is not None
        detail = client.get(f"/postings/{published.id}")
        assert detail.status_code == 200
        assert "No description stored." in detail.text
        assert "&lt;script&gt;" in detail.text
        assert "<script>" not in detail.text.split("<style>", 1)[-1]
        assert client.get(f"/postings/{hidden.id}").status_code == 404
    finally:
        session.execute(delete(Job).where(Job.title_original.contains(TOKEN)))
        session.execute(delete(CrawlRun).where(CrawlRun.error_text.contains(TOKEN)))
        session.commit()
        session.close()


def test_empty_dashboard_states_are_explicit() -> None:
    body = _page(
        {
            "jobs": {"active": 0, "expired": 0, "by_india_relevance": {}},
            "sources": {"enabled": 0, "disabled": 0},
            "crawl_runs": {"failed": 0, "success": 0, "fetched": 0, "inserted": 0, "rejected": 0},
        },
        [],
        [],
        [],
    )
    assert "No published jobs yet." in body
    assert "No failed crawls." in body

    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 200
    assert "No failed crawls." in response.text


def test_unknown_source_scrape_is_not_found() -> None:
    client = TestClient(app)
    response = client.post("/scrape", data={"source_id": str(uuid.uuid4())})
    assert response.status_code == 404


def test_rejected_board_is_saved_disabled() -> None:
    key = f"missing{uuid.uuid4().hex[:10]}"
    session = SessionLocal()
    client = TestClient(app)
    try:
        with respx.mock:
            respx.get(JOBS_URL.format(board_token=key)).mock(return_value=httpx.Response(404))
            response = client.post(
                "/sources/add",
                data={"board": key, "source_type": "greenhouse", "company_name": "Missing Co"},
                follow_redirects=False,
            )
        assert response.status_code == 303
        assert "disabled" in response.headers["location"]
        source = session.scalar(select(Source).where(Source.external_key == key))
        assert source is not None
        assert source.enabled is False
    finally:
        session.execute(delete(Source).where(Source.external_key == key))
        session.commit()
        session.close()


def test_seed_without_a_choice_asks_for_one() -> None:
    client = TestClient(app)
    response = client.post("/sources/seed", data={}, follow_redirects=False)
    assert response.status_code == 303
    assert "Select" in response.headers["location"]


def test_probe_keeps_a_board_that_passes_the_live_check() -> None:
    payload = json.loads((Path(__file__).parent / "fixtures" / "greenhouse_groww.json").read_text())
    ok = {
        "source_type": "greenhouse",
        "external_key": "probe-ok",
        "company_name": "Groww",
        "name": "Groww",
        "base_url": "https://job-boards.greenhouse.io/probe-ok",
    }
    missing = {**ok, "external_key": "probe-missing", "company_name": "Nope", "name": "Nope"}
    with respx.mock:
        respx.get(JOBS_URL.format(board_token="probe-ok")).mock(return_value=httpx.Response(200, json=payload))
        respx.get(JOBS_URL.format(board_token="probe-missing")).mock(return_value=httpx.Response(404))
        ready = probe_candidates([ok, missing])
    assert [item.external_key for item in ready] == ["probe-ok"]
    assert ready[0].job_count == len(payload["jobs"])
    assert ready[0].india_count >= 1
