import uuid
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.db import SessionLocal
from app.main import app
from app.models import Job, Source

TOKEN = "api-search-token"
NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
NEWEST_ID = uuid.UUID("00000000-0000-4000-8000-0000000000a1")
TWIN_ID = uuid.UUID("00000000-0000-4000-8000-0000000000a2")
MIDDLE_ID = uuid.UUID("00000000-0000-4000-8000-0000000000a3")
OLDEST_ID = uuid.UUID("00000000-0000-4000-8000-0000000000a4")


def test_pagination_and_filters_are_stable() -> None:
    session = SessionLocal()
    client = TestClient(app)
    try:
        _insert_catalog(session)
        session.commit()

        first = client.get("/jobs", params={"query": TOKEN, "page_size": 1, "page": 1})
        second = client.get("/jobs", params={"query": TOKEN, "page_size": 1, "page": 2})
        first_again = client.get("/jobs", params={"query": TOKEN, "page_size": 1, "page": 1})
        assert first.status_code == 200
        assert first.json()["total"] == 4
        assert first.json()["jobs"][0]["id"] == str(NEWEST_ID)
        assert second.json()["jobs"][0]["id"] == str(TWIN_ID)
        assert first_again.json() == first.json()

        bengaluru = client.get("/jobs", params={"query": TOKEN, "city": "bengaluru", "page_size": 10})
        assert [job["id"] for job in bengaluru.json()["jobs"]] == [
            str(NEWEST_ID),
            str(TWIN_ID),
            str(MIDDLE_ID),
        ]

        remote = client.get("/jobs", params={"query": TOKEN, "work_mode": "Remote", "state": "Karnataka"})
        assert [job["id"] for job in remote.json()["jobs"]] == [str(MIDDLE_ID)]

        percent = client.get("/jobs", params={"query": f"{TOKEN} 100%", "page_size": 10})
        assert [job["id"] for job in percent.json()["jobs"]] == [str(TWIN_ID)]

        detail = client.get(f"/jobs/{NEWEST_ID}")
        assert detail.status_code == 200
        assert detail.json()["title"] == f"Backend Engineer {TOKEN}"
        assert detail.json()["city"] == "Bengaluru"
        assert "Builds the ledger" in detail.json()["description_text"]
    finally:
        _cleanup(session)
        session.close()


def test_unpublished_jobs_stay_out_of_search() -> None:
    session = SessionLocal()
    client = TestClient(app)
    try:
        expired = _job(
            title=f"Expired {TOKEN}",
            city="Bengaluru",
            state="Karnataka",
            work_mode="onsite",
            india_relevance="india",
            status="expired",
            posted_at=NOW,
        )
        foreign = _job(
            title=f"Athens {TOKEN}",
            city="Athens",
            state="Attica",
            work_mode="onsite",
            india_relevance="not_india",
            status="active",
            posted_at=NOW,
        )
        session.add_all([expired, foreign])
        session.commit()

        listed = client.get("/jobs", params={"query": TOKEN, "page_size": 20})
        assert listed.json()["jobs"] == []
        assert client.get(f"/jobs/{expired.id}").status_code == 404
        assert client.get(f"/jobs/{foreign.id}").status_code == 404
        assert client.get("/jobs/not-a-uuid").status_code == 422
        missing = uuid.uuid4()
        assert client.get(f"/jobs/{missing}").status_code == 404
    finally:
        _cleanup(session)
        session.close()


def test_stats_count_jobs_and_sources() -> None:
    session = SessionLocal()
    client = TestClient(app)
    try:
        before = client.get("/stats")
        assert before.status_code == 200
        session.add(
            _job(
                title=f"Counted {TOKEN}",
                city="Bengaluru",
                state="Karnataka",
                work_mode="onsite",
                india_relevance="india",
                status="active",
                posted_at=NOW,
            )
        )
        session.commit()
        after = client.get("/stats").json()
        assert after["jobs"]["active"] == before.json()["jobs"]["active"] + 1
        assert after["jobs"]["by_india_relevance"]["india"] == before.json()["jobs"]["by_india_relevance"].get(
            "india", 0
        ) + 1
        assert after["sources"]["enabled"] >= 4
        assert "success" in after["crawl_runs"]
    finally:
        _cleanup(session)
        session.close()


def test_sources_and_runs_are_readable() -> None:
    client = TestClient(app)
    response = client.get("/sources")
    assert response.status_code == 200
    keys = {item["external_key"] for item in response.json()}
    assert {"groww", "cred", "temporal", "epignosis"} <= keys

    session = SessionLocal()
    try:
        source = session.scalar(select(Source).where(Source.external_key == "groww"))
        assert source is not None
        runs = client.get(f"/sources/{source.id}/runs")
        assert runs.status_code == 200
        assert isinstance(runs.json(), list)
        assert client.get(f"/sources/{uuid.uuid4()}/runs").status_code == 404
    finally:
        session.close()


def test_page_zero_is_rejected() -> None:
    client = TestClient(app)
    assert client.get("/jobs", params={"page": 0}).status_code == 422


def _insert_catalog(session) -> None:
    session.add_all(
        [
            _job(
                job_id=NEWEST_ID,
                title=f"Backend Engineer {TOKEN}",
                city="Bengaluru",
                state="Karnataka",
                work_mode="onsite",
                india_relevance="india",
                status="active",
                posted_at=NOW,
                description="Builds the ledger",
            ),
            _job(
                job_id=TWIN_ID,
                title=f"Backend Engineer {TOKEN} 100%",
                city="Bengaluru",
                state="Karnataka",
                work_mode="onsite",
                india_relevance="remote_india",
                status="active",
                posted_at=NOW,
                description="Same day, later id",
            ),
            _job(
                job_id=MIDDLE_ID,
                title=f"Data Engineer {TOKEN}",
                city="Bengaluru",
                state="Karnataka",
                work_mode="remote",
                india_relevance="india",
                status="active",
                posted_at=NOW - timedelta(days=1),
            ),
            _job(
                job_id=OLDEST_ID,
                title=f"Designer {TOKEN}",
                city="Mumbai",
                state="Maharashtra",
                work_mode="onsite",
                india_relevance="india",
                status="active",
                posted_at=NOW - timedelta(days=2),
            ),
        ]
    )


def _job(
    *,
    title: str,
    city: str,
    state: str,
    work_mode: str,
    india_relevance: str,
    status: str,
    posted_at: datetime,
    job_id: uuid.UUID | None = None,
    description: str = "Role description",
) -> Job:
    return Job(
        id=job_id,
        title_original=title,
        company_name="Example",
        company_normalized="example",
        description_text=description,
        city=city,
        state=state,
        work_mode=work_mode,
        india_relevance=india_relevance,
        status=status,
        posted_at=posted_at,
        first_seen_at=NOW,
        last_seen_at=NOW,
        exact_fingerprint=uuid.uuid4().hex,
        canonical_apply_url="https://example.com/apply",
    )


def _cleanup(session) -> None:
    session.execute(delete(Job).where(Job.title_original.contains(TOKEN)))
    session.commit()
