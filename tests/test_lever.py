import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import respx

from app.config import settings
from app.connectors.base import MalformedPayload, SourceNotFound
from app.connectors.lever import POSTINGS_URL, LeverConnector
from app.models.source import Source
from app.schemas.raw_job import RawJobEnvelope

FIXTURE = Path(__file__).parent / "fixtures" / "lever_cred.json"
CRED_URL = POSTINGS_URL.format(site="cred")


def _source() -> Source:
    return Source(
        name="CRED",
        source_type="lever",
        external_key="cred",
        company_name="CRED",
        base_url="https://jobs.lever.co/cred",
    )


def _postings() -> list[dict]:
    return json.loads(FIXTURE.read_text())


def _envelope(posting: dict) -> RawJobEnvelope:
    return RawJobEnvelope(
        source_type="lever",
        source_external_key="cred",
        source_job_id=str(posting["id"]),
        source_url=posting["hostedUrl"],
        fetched_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
        payload=posting,
    )


@pytest.mark.asyncio
async def test_lever_fetch_builds_envelopes_from_fixture() -> None:
    postings = _postings()
    with respx.mock(assert_all_mocked=True) as mock:
        route = mock.get(CRED_URL).mock(return_value=httpx.Response(200, json=postings))
        async with httpx.AsyncClient() as client:
            envelopes = await LeverConnector(client, company_name="CRED").fetch(_source())

    assert route.call_count == 1
    assert route.calls[0].request.url.params["mode"] == "json"
    assert route.calls[0].request.headers["user-agent"] == settings.user_agent
    assert len(envelopes) == len(postings)
    first = envelopes[0]
    assert first.source_type == "lever"
    assert first.source_external_key == "cred"
    assert first.source_job_id == postings[0]["id"]
    assert first.source_url == postings[0]["hostedUrl"]
    assert first.payload == postings[0]
    assert "company_name" not in first.payload


def test_lever_normalizer_maps_fixture_posting() -> None:
    posting = _postings()[1]
    candidate = LeverConnector(company_name="CRED").normalize(_envelope(posting))

    assert candidate.title_original == "business development & partnerships"
    assert candidate.company_name == "CRED"
    assert candidate.locations_raw == ["bengaluru"]
    assert candidate.employment_type == "full time"
    assert candidate.apply_url == posting["applyUrl"]
    assert candidate.apply_url is not None
    assert candidate.apply_url.endswith("/apply")
    assert candidate.posted_at == datetime.fromtimestamp(posting["createdAt"] / 1000, tz=timezone.utc)
    assert candidate.posted_at is not None
    assert (candidate.posted_at.year, candidate.posted_at.month, candidate.posted_at.day) == (2026, 5, 25)
    assert candidate.description_html is not None
    assert candidate.description_html.startswith("<div>what is CRED?</div>")
    assert candidate.description_text is not None
    assert candidate.description_text.startswith("what is CRED?")
    assert "<div>" not in candidate.description_text
    assert candidate.source_job_id == posting["id"]


def test_lever_normalizer_allows_empty_description() -> None:
    posting = _postings()[0]
    candidate = LeverConnector(company_name="CRED").normalize(_envelope(posting))
    assert candidate.title_original == "accounts payable manager"
    assert candidate.locations_raw == ["hyderabad"]
    assert candidate.description_text is None
    assert candidate.description_html is None
    assert posting["lists"][0]["content"]


def test_lever_normalizer_requires_company_name() -> None:
    posting = _postings()[1]
    with pytest.raises(ValueError, match="company_name"):
        LeverConnector().normalize(_envelope(posting))


def test_lever_normalizer_keeps_distinct_locations() -> None:
    raw = RawJobEnvelope(
        source_type="lever",
        source_external_key="example",
        source_job_id="1",
        source_url="https://jobs.lever.co/example/1",
        fetched_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
        payload={
            "id": "1",
            "text": "Engineer",
            "categories": {
                "location": "Bengaluru",
                "allLocations": ["Bengaluru", "Mumbai"],
            },
            "hostedUrl": "https://jobs.lever.co/example/1",
            "applyUrl": "https://jobs.lever.co/example/1/apply",
        },
    )
    candidate = LeverConnector(company_name="Example").normalize(raw)
    assert candidate.locations_raw == ["Bengaluru", "Mumbai"]


@pytest.mark.asyncio
async def test_lever_retries_transient_5xx_then_reads_fixture() -> None:
    postings = _postings()
    with respx.mock(assert_all_mocked=True) as mock:
        route = mock.get(CRED_URL).mock(
            side_effect=[
                httpx.Response(503, text="busy"),
                httpx.Response(200, json=postings),
            ]
        )
        async with httpx.AsyncClient() as client:
            envelopes = await LeverConnector(client).fetch(_source())
    assert route.call_count == 2
    assert len(envelopes) == 8


@pytest.mark.asyncio
async def test_lever_404_is_not_retried() -> None:
    with respx.mock(assert_all_mocked=True) as mock:
        route = mock.get(CRED_URL).mock(return_value=httpx.Response(404, json={"ok": False}))
        async with httpx.AsyncClient() as client:
            with pytest.raises(SourceNotFound):
                await LeverConnector(client).fetch(_source())
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_lever_rejects_non_list_payload() -> None:
    with respx.mock(assert_all_mocked=True) as mock:
        mock.get(CRED_URL).mock(return_value=httpx.Response(200, json={"ok": False}))
        async with httpx.AsyncClient() as client:
            with pytest.raises(MalformedPayload):
                await LeverConnector(client).fetch(_source())
