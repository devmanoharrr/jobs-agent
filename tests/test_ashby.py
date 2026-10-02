import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import respx

from app.config import settings
from app.connectors.ashby import BOARD_URL, AshbyConnector
from app.connectors.base import MalformedPayload, SourceNotFound
from app.models.source import Source
from app.schemas.raw_job import RawJobEnvelope

FIXTURE = Path(__file__).parent / "fixtures" / "ashby_temporal.json"
TEMPORAL_URL = BOARD_URL.format(board_name="temporal")


def _source() -> Source:
    return Source(
        name="Temporal",
        source_type="ashby",
        external_key="temporal",
        company_name="Temporal",
        base_url="https://jobs.ashbyhq.com/temporal",
    )


def _body() -> dict:
    return json.loads(FIXTURE.read_text())


def _envelope(job: dict) -> RawJobEnvelope:
    return RawJobEnvelope(
        source_type="ashby",
        source_external_key="temporal",
        source_job_id=str(job["id"]),
        source_url=job["jobUrl"],
        fetched_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
        payload=job,
    )


@pytest.mark.asyncio
async def test_ashby_fetch_builds_envelopes_from_fixture() -> None:
    body = _body()
    with respx.mock(assert_all_mocked=True) as mock:
        route = mock.get(TEMPORAL_URL).mock(return_value=httpx.Response(200, json=body))
        async with httpx.AsyncClient() as client:
            envelopes = await AshbyConnector(client, company_name="Temporal").fetch(_source())

    assert route.call_count == 1
    assert route.calls[0].request.url.params["includeCompensation"] == "true"
    assert route.calls[0].request.headers["user-agent"] == settings.user_agent
    assert len(envelopes) == len(body["jobs"])
    first = envelopes[0]
    assert first.source_type == "ashby"
    assert first.source_external_key == "temporal"
    assert first.source_job_id == body["jobs"][0]["id"]
    assert first.source_url == body["jobs"][0]["jobUrl"]
    assert first.payload == body["jobs"][0]
    assert "compensation" in first.payload
    assert "company_name" not in first.payload


def test_ashby_normalizer_maps_fixture_job() -> None:
    job = _body()["jobs"][0]
    candidate = AshbyConnector(company_name="Temporal").normalize(_envelope(job))

    assert candidate.title_original == "Manager, India Partnerships"
    assert candidate.company_name == "Temporal"
    assert candidate.locations_raw == ["Bengaluru, Karnataka"]
    assert candidate.employment_type == "FullTime"
    assert candidate.apply_url == job["applyUrl"]
    assert candidate.apply_url is not None
    assert candidate.apply_url.endswith("/application")
    assert candidate.posted_at == datetime.fromisoformat("2026-09-01T23:05:33.027+00:00")
    assert candidate.description_html is not None
    assert candidate.description_html.startswith("<h2>")
    assert candidate.description_text is not None
    assert "ROLE SUMMARY" in candidate.description_text
    assert "<h2>" not in candidate.description_text
    assert candidate.source_job_id == job["id"]


def test_ashby_normalizer_includes_secondary_locations() -> None:
    raw = RawJobEnvelope(
        source_type="ashby",
        source_external_key="temporal",
        source_job_id="1",
        source_url="https://jobs.ashbyhq.com/temporal/1",
        fetched_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
        payload={
            "id": "1",
            "title": "Engineer",
            "location": "Bengaluru, Karnataka",
            "secondaryLocations": [
                {"location": "Mumbai", "address": {"addressCountry": "India"}},
                {"location": "Bengaluru, Karnataka"},
            ],
            "jobUrl": "https://jobs.ashbyhq.com/temporal/1",
            "applyUrl": "https://jobs.ashbyhq.com/temporal/1/application",
        },
    )
    candidate = AshbyConnector(company_name="Temporal").normalize(raw)
    assert candidate.locations_raw == ["Bengaluru, Karnataka", "Mumbai"]


def test_ashby_normalizer_requires_company_name() -> None:
    job = _body()["jobs"][0]
    with pytest.raises(ValueError, match="company_name"):
        AshbyConnector().normalize(_envelope(job))


@pytest.mark.asyncio
async def test_ashby_keeps_unlisted_jobs() -> None:
    body = {
        "apiVersion": "1",
        "jobs": [
            {
                "id": "hidden",
                "title": "Hidden role",
                "isListed": False,
                "jobUrl": "https://jobs.ashbyhq.com/temporal/hidden",
            }
        ],
    }
    with respx.mock(assert_all_mocked=True) as mock:
        mock.get(TEMPORAL_URL).mock(return_value=httpx.Response(200, json=body))
        async with httpx.AsyncClient() as client:
            envelopes = await AshbyConnector(client).fetch(_source())
    assert len(envelopes) == 1
    assert envelopes[0].payload["isListed"] is False


@pytest.mark.asyncio
async def test_ashby_retries_transient_5xx_then_reads_fixture() -> None:
    body = _body()
    with respx.mock(assert_all_mocked=True) as mock:
        route = mock.get(TEMPORAL_URL).mock(
            side_effect=[
                httpx.Response(503, text="busy"),
                httpx.Response(200, json=body),
            ]
        )
        async with httpx.AsyncClient() as client:
            envelopes = await AshbyConnector(client).fetch(_source())
    assert route.call_count == 2
    assert len(envelopes) == 2


@pytest.mark.asyncio
async def test_ashby_404_is_not_retried() -> None:
    with respx.mock(assert_all_mocked=True) as mock:
        route = mock.get(TEMPORAL_URL).mock(return_value=httpx.Response(404, json={"error": True}))
        async with httpx.AsyncClient() as client:
            with pytest.raises(SourceNotFound):
                await AshbyConnector(client).fetch(_source())
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_ashby_rejects_payload_without_jobs_array() -> None:
    with respx.mock(assert_all_mocked=True) as mock:
        mock.get(TEMPORAL_URL).mock(return_value=httpx.Response(200, json={"apiVersion": "1"}))
        async with httpx.AsyncClient() as client:
            with pytest.raises(MalformedPayload):
                await AshbyConnector(client).fetch(_source())
