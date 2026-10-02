import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import respx

from app.config import settings
from app.connectors.base import MalformedPayload, SourceNotFound
from app.connectors.greenhouse import JOBS_URL, GreenhouseConnector
from app.models.source import Source
from app.schemas.raw_job import RawJobEnvelope

FIXTURE = Path(__file__).parent / "fixtures" / "greenhouse_groww.json"
GROWW_URL = JOBS_URL.format(board_token="groww")


def _source() -> Source:
    return Source(
        name="Groww",
        source_type="greenhouse",
        external_key="groww",
        company_name="Groww",
        base_url="https://job-boards.eu.greenhouse.io/groww",
    )


def _payload() -> dict:
    return json.loads(FIXTURE.read_text())


@pytest.mark.asyncio
async def test_greenhouse_fetch_builds_envelopes_from_fixture() -> None:
    payload = _payload()
    with respx.mock(assert_all_mocked=True) as mock:
        route = mock.get(GROWW_URL).mock(return_value=httpx.Response(200, json=payload))
        async with httpx.AsyncClient() as client:
            envelopes = await GreenhouseConnector(client).fetch(_source())

    assert route.call_count == 1
    assert route.calls[0].request.url.params["content"] == "true"
    assert route.calls[0].request.headers["user-agent"] == settings.user_agent
    assert len(envelopes) == len(payload["jobs"])
    first = envelopes[0]
    assert first.source_type == "greenhouse"
    assert first.source_external_key == "groww"
    assert first.source_job_id == str(payload["jobs"][0]["id"])
    assert first.source_url == payload["jobs"][0]["absolute_url"]
    assert first.payload["requisition_id"] == payload["jobs"][0]["requisition_id"]
    assert "apply" not in str(route.calls[0].request.url)


def test_greenhouse_normalizer_maps_fixture_job() -> None:
    payload = _payload()
    job = payload["jobs"][0]
    raw = RawJobEnvelope(
        source_type="greenhouse",
        source_external_key="groww",
        source_job_id=str(job["id"]),
        source_url=job["absolute_url"],
        fetched_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
        payload=job,
    )
    candidate = GreenhouseConnector().normalize(raw)

    assert candidate.title_original == "Assistant Manager - Internal Audit"
    assert candidate.company_name == "Groww"
    assert candidate.locations_raw == ["Bengaluru-VTP, India"]
    assert candidate.apply_url == job["absolute_url"]
    assert candidate.posted_at == datetime.fromisoformat("2026-09-14T03:49:28-04:00")
    assert candidate.description_html is not None
    assert candidate.description_html.startswith("<div>")
    assert candidate.description_text is not None
    assert "About Groww" in candidate.description_text
    assert "&lt;" not in candidate.description_text
    assert candidate.source_job_id == "4970739101"


def test_greenhouse_normalizer_splits_semicolon_locations() -> None:
    raw = RawJobEnvelope(
        source_type="greenhouse",
        source_external_key="databricks",
        source_job_id="1",
        source_url="https://boards.greenhouse.io/databricks/jobs/1",
        fetched_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
        payload={
            "id": 1,
            "title": "Engineer",
            "company_name": "Databricks",
            "location": {"name": "Bengaluru, India; Mumbai, India"},
            "absolute_url": "https://boards.greenhouse.io/databricks/jobs/1",
        },
    )
    candidate = GreenhouseConnector().normalize(raw)
    assert candidate.locations_raw == ["Bengaluru, India", "Mumbai, India"]


@pytest.mark.asyncio
async def test_greenhouse_retries_transient_5xx_then_reads_fixture() -> None:
    payload = _payload()
    with respx.mock(assert_all_mocked=True) as mock:
        route = mock.get(GROWW_URL).mock(
            side_effect=[
                httpx.Response(503, text="busy"),
                httpx.Response(200, json=payload),
            ]
        )
        async with httpx.AsyncClient() as client:
            envelopes = await GreenhouseConnector(client).fetch(_source())
    assert route.call_count == 2
    assert len(envelopes) == 7


@pytest.mark.asyncio
async def test_greenhouse_404_is_not_retried() -> None:
    with respx.mock(assert_all_mocked=True) as mock:
        route = mock.get(GROWW_URL).mock(return_value=httpx.Response(404, json={"status": 404}))
        async with httpx.AsyncClient() as client:
            with pytest.raises(SourceNotFound):
                await GreenhouseConnector(client).fetch(_source())
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_greenhouse_rejects_payload_without_jobs_array() -> None:
    with respx.mock(assert_all_mocked=True) as mock:
        mock.get(GROWW_URL).mock(return_value=httpx.Response(200, json={"meta": {}}))
        async with httpx.AsyncClient() as client:
            with pytest.raises(MalformedPayload):
                await GreenhouseConnector(client).fetch(_source())
