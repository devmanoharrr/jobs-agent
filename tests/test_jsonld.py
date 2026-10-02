from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from app.config import settings
from app.connectors.base import ConnectorError
from app.connectors.jsonld import JsonLdConnector
from app.schemas.canonical_job import CanonicalJobCandidate
from app.discovery.robots import RobotsCache
from app.models.source import Source

PAGE = "https://jobs.example/careers"
ROBOTS = "https://jobs.example/robots.txt"
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
ALLOW = "User-agent: *\nAllow: /\n"
DISALLOW_CAREERS = "User-agent: *\nDisallow: /careers\n"
HTML = """
<html>
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "https://schema.org/JobPosting",
  "identifier": "role-9",
  "title": "Backend Engineer",
  "description": "<p>Build the ledger in Bengaluru.</p>",
  "datePosted": "2026-10-01",
  "validThrough": "2026-12-01T00:00:00Z",
  "employmentType": ["FULL_TIME", "CONTRACTOR"],
  "hiringOrganization": {"@type": "Organization", "name": "Example Labs"},
  "jobLocation": {
    "@type": "Place",
    "address": {
      "addressLocality": "Bengaluru",
      "addressRegion": "Karnataka",
      "addressCountry": "IN"
    }
  },
  "applicantLocationRequirements": {"@type": "Country", "name": "India"},
  "url": "https://jobs.example/careers/role-9",
  "directApply": true,
  "baseSalary": {"value": 100}
}
</script>
<script type="application/ld+json">
{"@type": "JobPosting", "title": "No URL", "directApply": true, "hiringOrganization": "Example Labs"}
</script>
</html>
"""


@pytest.mark.asyncio
async def test_jsonld_maps_posting_fields_and_leaves_salary_in_the_raw_payload() -> None:
    with respx.mock(assert_all_mocked=True) as mock:
        mock.get(ROBOTS).respond(200, text=ALLOW)
        page = mock.get(PAGE).respond(200, text=HTML)
        async with httpx.AsyncClient() as client:
            envelopes = await _connector(client).fetch(_source())
    assert page.call_count == 1
    assert page.calls[0].request.headers["user-agent"] == settings.user_agent
    posted, missing_url = envelopes
    assert posted.source_job_id == "role-9"
    assert posted.payload["baseSalary"] == {"value": 100}
    assert "company_name" not in posted.payload
    candidate = _connector(None).normalize(posted)
    assert candidate.title_original == "Backend Engineer"
    assert candidate.company_name == "Example Labs"
    assert candidate.description_text == "Build the ledger in Bengaluru."
    assert candidate.locations_raw == ["Bengaluru, Karnataka, IN", "India"]
    assert candidate.employment_type == "FULL_TIME, CONTRACTOR"
    assert candidate.posted_at == datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert candidate.expires_at == datetime(2026, 12, 1, tzinfo=timezone.utc)
    assert candidate.apply_url == "https://jobs.example/careers/role-9"
    assert "salary" not in CanonicalJobCandidate.model_fields
    bare = _connector(None).normalize(missing_url)
    assert bare.apply_url is None
    assert bare.title_original == "No URL"


@pytest.mark.asyncio
async def test_missing_organization_uses_the_source_name_without_editing_the_payload() -> None:
    html = """
    <script type="application/ld+json">
    {"@type": "JobPosting", "title": "Analyst", "description": "Plain text"}
    </script>
    """
    with respx.mock(assert_all_mocked=True) as mock:
        mock.get(ROBOTS).respond(404)
        mock.get(PAGE).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            envelopes = await _connector(client).fetch(_source())
    assert list(envelopes[0].payload) == ["@type", "title", "description"]
    candidate = _connector(None, company_name="Example").normalize(envelopes[0])
    assert candidate.company_name == "Example"
    assert candidate.description_html is None
    assert candidate.description_text == "Plain text"


@pytest.mark.asyncio
async def test_robots_disallow_does_not_fetch_the_page() -> None:
    with respx.mock(assert_all_mocked=True) as mock:
        mock.get(ROBOTS).respond(200, text=DISALLOW_CAREERS)
        async with httpx.AsyncClient() as client:
            with pytest.raises(ConnectorError, match="disallows"):
                await _connector(client).fetch(_source())


@pytest.mark.asyncio
async def test_robots_forbidden_disallows_the_origin() -> None:
    with respx.mock(assert_all_mocked=True) as mock:
        mock.get(ROBOTS).respond(403)
        async with httpx.AsyncClient() as client:
            with pytest.raises(ConnectorError, match="disallows"):
                await _connector(client).fetch(_source())


@pytest.mark.asyncio
async def test_unreachable_robots_without_a_cache_does_not_fetch_the_page() -> None:
    with respx.mock(assert_all_mocked=True) as mock:
        mock.get(ROBOTS).respond(503)
        async with httpx.AsyncClient() as client:
            with pytest.raises(ConnectorError, match="unreachable"):
                await _connector(client).fetch(_source())


@pytest.mark.asyncio
async def test_robots_cache_is_reused_until_it_is_a_day_old() -> None:
    moment = {"now": NOW}

    def clock() -> datetime:
        return moment["now"]

    cache = RobotsCache()
    with respx.mock(assert_all_mocked=True) as mock:
        robots = mock.get(ROBOTS).respond(200, text=ALLOW)
        mock.get(PAGE).respond(200, text="<html></html>")
        async with httpx.AsyncClient() as client:
            connector = _connector(client, robots=cache, clock=clock)
            first = await connector.fetch(_source())
            moment["now"] = NOW + timedelta(hours=1)
            second = await connector.fetch(_source())
            moment["now"] = NOW + timedelta(hours=25)
            mock.get(ROBOTS).respond(503)
            third = await connector.fetch(_source())
    assert robots.call_count == 2
    assert first == []
    assert second == []
    assert third == []


@pytest.mark.asyncio
async def test_sitemap_skips_urls_robots_disallows() -> None:
    sitemap = """<?xml version="1.0"?>
    <urlset>
      <url><loc>https://jobs.example/careers</loc></url>
      <url><loc>https://jobs.example/private</loc></url>
    </urlset>
    """
    rules = "User-agent: *\nDisallow: /private\nAllow: /\n"
    with respx.mock(assert_all_mocked=True) as mock:
        mock.get(ROBOTS).respond(200, text=rules)
        mock.get("https://jobs.example/sitemap.xml").respond(
            200, text=sitemap, headers={"content-type": "application/xml"}
        )
        mock.get(PAGE).respond(200, text=HTML)
        async with httpx.AsyncClient() as client:
            source = _source()
            source.base_url = "https://jobs.example/sitemap.xml"
            envelopes = await _connector(client).fetch(source)
    assert [item.source_job_id for item in envelopes][0] == "role-9"
    assert len(envelopes) == 2


def test_page_without_jobposting_does_not_invent_a_job() -> None:
    from app.discovery.jsonld import iter_job_postings

    assert iter_job_postings("<html><p>Careers</p></html>") == []


def _source() -> Source:
    return Source(
        name="Example",
        source_type="generic-jsonld",
        external_key="example",
        company_name="Example",
        base_url=PAGE,
    )


def _connector(
    client: httpx.AsyncClient | None,
    company_name: str | None = "Example",
    *,
    robots: RobotsCache | None = None,
    clock=None,
) -> JsonLdConnector:
    return JsonLdConnector(
        client,
        company_name=company_name,
        robots=robots,
        request_gap_seconds=0,
        clock=clock,
    )
