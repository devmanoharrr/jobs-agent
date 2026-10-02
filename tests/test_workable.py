import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import respx

from app.config import settings
from app.connectors.base import ConnectorError, MalformedPayload, SourceNotFound
from app.connectors.workable import ACCOUNT_URL, WorkableConnector
from app.models.source import Source
from app.schemas.raw_job import RawJobEnvelope

FIXTURE = Path(__file__).parent / "fixtures" / "workable_epignosis.json"
EPIGNOSIS_URL = ACCOUNT_URL.format(subdomain="epignosis")


def _source() -> Source:
    return Source(
        name="Epignosis",
        source_type="workable",
        external_key="epignosis",
        company_name="Epignosis",
        base_url="https://apply.workable.com/epignosis",
    )


def _body() -> dict:
    return json.loads(FIXTURE.read_text())


def _envelope(job: dict) -> RawJobEnvelope:
    return RawJobEnvelope(
        source_type="workable",
        source_external_key="epignosis",
        source_job_id=job["shortcode"],
        source_url=job["url"],
        fetched_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
        payload=job,
    )


@pytest.mark.asyncio
async def test_workable_fetch_builds_envelopes_from_fixture() -> None:
    body = _body()
    with respx.mock(assert_all_mocked=True) as mock:
        route = mock.get(EPIGNOSIS_URL).mock(return_value=httpx.Response(200, json=body))
        async with httpx.AsyncClient() as client:
            envelopes = await WorkableConnector(client, company_name="Epignosis").fetch(_source())

    assert route.call_count == 1
    assert route.calls[0].request.url.params["details"] == "true"
    assert route.calls[0].request.headers["user-agent"] == settings.user_agent
    assert len(envelopes) == len(body["jobs"])
    first = envelopes[0]
    assert first.source_type == "workable"
    assert first.source_job_id == "E38DB16625"
    assert first.source_url == body["jobs"][0]["url"]
    assert first.payload == body["jobs"][0]
    assert "company_name" not in first.payload
    repeated = [item for item in envelopes if item.source_job_id == "7D027E513D"]
    assert len(repeated) == 2
    assert repeated[0].payload["city"] != repeated[1].payload["city"]


def test_workable_normalizer_maps_fixture_job() -> None:
    job = _body()["jobs"][0]
    candidate = WorkableConnector(company_name="Epignosis").normalize(_envelope(job))

    assert candidate.title_original == "Data Engineer"
    assert candidate.company_name == "Epignosis"
    assert candidate.locations_raw == ["Athens, Attica, Greece"]
    assert candidate.employment_type == "Full-time"
    assert candidate.apply_url == job["application_url"]
    assert candidate.apply_url is not None
    assert candidate.apply_url.endswith("/apply")
    assert candidate.posted_at == datetime(2026, 9, 7, tzinfo=timezone.utc)
    assert candidate.description_html is not None
    assert candidate.description_html.startswith("<p>At Epignosis")
    assert candidate.description_text is not None
    assert "At Epignosis" in candidate.description_text
    assert "<p>" not in candidate.description_text
    assert candidate.source_job_id == "E38DB16625"


def test_workable_normalizer_uses_flat_location_fields() -> None:
    raw = RawJobEnvelope(
        source_type="workable",
        source_external_key="epignosis",
        source_job_id="CODE1",
        source_url="https://apply.workable.com/j/CODE1",
        fetched_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
        payload={
            "title": "Engineer",
            "shortcode": "",
            "code": "CODE1",
            "city": "Bengaluru",
            "state": "Karnataka",
            "country": "India",
        },
    )
    candidate = WorkableConnector(company_name="Epignosis").normalize(raw)
    assert candidate.locations_raw == ["Bengaluru, Karnataka, India"]
    assert candidate.source_job_id == "CODE1"


def test_workable_normalizer_requires_company_name() -> None:
    job = _body()["jobs"][0]
    with pytest.raises(ValueError, match="company_name"):
        WorkableConnector().normalize(_envelope(job))


@pytest.mark.asyncio
async def test_workable_rejects_unexpected_account_name() -> None:
    source = _source()
    source.company_name = "Wrong"
    with respx.mock(assert_all_mocked=True) as mock:
        mock.get(EPIGNOSIS_URL).mock(return_value=httpx.Response(200, json=_body()))
        async with httpx.AsyncClient() as client:
            with pytest.raises(ConnectorError, match="unexpected company name"):
                await WorkableConnector(client).fetch(source)


@pytest.mark.asyncio
async def test_workable_skips_jobs_without_identifier() -> None:
    body = {"name": "Epignosis", "jobs": [{"title": "No id", "city": "Athens"}]}
    with respx.mock(assert_all_mocked=True) as mock:
        mock.get(EPIGNOSIS_URL).mock(return_value=httpx.Response(200, json=body))
        async with httpx.AsyncClient() as client:
            envelopes = await WorkableConnector(client).fetch(_source())
    assert envelopes == []


@pytest.mark.asyncio
async def test_workable_follows_redirect_to_public_widget() -> None:
    body = _body()
    widget = "https://apply.workable.com/api/v1/widget/accounts/epignosis"
    with respx.mock(assert_all_mocked=True) as mock:
        mock.get(EPIGNOSIS_URL).mock(
            return_value=httpx.Response(302, headers={"Location": f"{widget}?details=true"})
        )
        mock.get(widget).mock(return_value=httpx.Response(200, json=body))
        async with httpx.AsyncClient() as client:
            envelopes = await WorkableConnector(client).fetch(_source())
    assert len(envelopes) == 9


@pytest.mark.asyncio
async def test_workable_retries_transient_5xx_then_reads_fixture() -> None:
    body = _body()
    with respx.mock(assert_all_mocked=True) as mock:
        route = mock.get(EPIGNOSIS_URL).mock(
            side_effect=[
                httpx.Response(503, text="busy"),
                httpx.Response(200, json=body),
            ]
        )
        async with httpx.AsyncClient() as client:
            envelopes = await WorkableConnector(client).fetch(_source())
    assert route.call_count == 2
    assert len(envelopes) == 9


@pytest.mark.asyncio
async def test_workable_404_is_not_retried() -> None:
    with respx.mock(assert_all_mocked=True) as mock:
        route = mock.get(EPIGNOSIS_URL).mock(return_value=httpx.Response(404, json={"error": "not-found"}))
        async with httpx.AsyncClient() as client:
            with pytest.raises(SourceNotFound):
                await WorkableConnector(client).fetch(_source())
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_workable_rejects_payload_without_jobs_array() -> None:
    with respx.mock(assert_all_mocked=True) as mock:
        mock.get(EPIGNOSIS_URL).mock(return_value=httpx.Response(200, json={"name": "Epignosis"}))
        async with httpx.AsyncClient() as client:
            with pytest.raises(MalformedPayload):
                await WorkableConnector(client).fetch(_source())
