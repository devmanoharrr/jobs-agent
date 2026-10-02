"""JSON-LD JobPosting connector.

Fetches only a verified source URL, or the locations listed in a sitemap that
source points at, and only when robots.txt allows the URL. It does not ask an
LLM to read the page.
"""

import asyncio
import html
import re
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from dateutil.parser import isoparse
from httpx import AsyncClient, HTTPError

from app.config import settings
from app.connectors.base import ConnectorError, SourceForbidden, SourceNotFound, TransientHTTPError
from app.discovery.jsonld import iter_job_postings
from app.discovery.robots import RobotsCache
from app.models.source import Source
from app.schemas.canonical_job import CanonicalJobCandidate
from app.schemas.raw_job import RawJobEnvelope
from app.utils.hashing import hash_payload
from app.utils.text import html_to_text

_LOC = re.compile(r"<loc>\s*([^<]+?)\s*</loc>", re.IGNORECASE)
_DEFAULT_GAP_SECONDS = 1.0


class JsonLdConnector:
    source_type = "generic-jsonld"

    def __init__(
        self,
        client: AsyncClient | None = None,
        company_name: str | None = None,
        *,
        robots: RobotsCache | None = None,
        request_gap_seconds: float = _DEFAULT_GAP_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._company_name = company_name.strip() if isinstance(company_name, str) and company_name.strip() else None
        self._robots = robots or RobotsCache()
        self._gap = request_gap_seconds
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._locks: dict[str, asyncio.Lock] = {}

    async def fetch(self, source: Source) -> list[RawJobEnvelope]:
        if not source.base_url:
            raise ConnectorError("JSON-LD source requires base_url")
        if self._client is None:
            raise ConnectorError("JSON-LD fetch requires an HTTP client")
        self._company_name = source.company_name or self._company_name
        pages = await self._pages(source.base_url)
        if not pages:
            raise ConnectorError(f"robots.txt disallows {source.base_url}")
        fetched_at = datetime.now(timezone.utc)
        envelopes: list[RawJobEnvelope] = []
        for page_url, body in pages:
            for posting in iter_job_postings(body):
                job_url = _text(posting.get("url"))
                envelopes.append(
                    RawJobEnvelope(
                        source_type=self.source_type,
                        source_external_key=source.external_key,
                        source_job_id=_source_job_id(posting, job_url or page_url),
                        source_url=job_url or page_url,
                        fetched_at=fetched_at,
                        payload=posting,
                    )
                )
        return envelopes

    def normalize(self, raw: RawJobEnvelope) -> CanonicalJobCandidate:
        posting = raw.payload
        title = _text(posting.get("title"))
        if title is None:
            raise ValueError("JSON-LD job is missing title")
        company = _organization_name(posting.get("hiringOrganization")) or self._company_name
        if company is None:
            raise ValueError("JSON-LD job is missing hiringOrganization")
        description = _text(posting.get("description"))
        description_html = description if description and "<" in description else None
        return CanonicalJobCandidate(
            title_original=title,
            company_name=company,
            description_text=html_to_text(description) if description_html else description,
            description_html=description_html,
            locations_raw=_locations(posting),
            employment_type=_employment_type(posting.get("employmentType")),
            posted_at=_timestamp(posting.get("datePosted")),
            expires_at=_timestamp(posting.get("validThrough")),
            apply_url=_text(posting.get("url")),
            source_job_id=raw.source_job_id,
            source_type=raw.source_type,
            source_external_key=raw.source_external_key,
        )

    async def _pages(self, url: str) -> list[tuple[str, str]]:
        body, content_type = await self._read(url, primary=True)
        if body is None or not _is_sitemap(url, body, content_type):
            return [(url, body or "")]
        pages: list[tuple[str, str]] = []
        for loc in _locs(body):
            if loc.lower().split("?", 1)[0].endswith(".xml"):
                child_body, child_type = await self._read(loc, primary=False)
                if child_body is None or not _is_sitemap(loc, child_body, child_type):
                    continue
                for page_url in _locs(child_body):
                    page = await self._read(page_url, primary=False)
                    if page[0] is not None:
                        pages.append((page_url, page[0]))
                continue
            page = await self._read(loc, primary=False)
            if page[0] is not None:
                pages.append((loc, page[0]))
        if not pages:
            raise ConnectorError(f"robots.txt disallows every URL from {url}")
        return pages

    async def _read(self, url: str, *, primary: bool) -> tuple[str | None, str]:
        assert self._client is not None
        host = urlparse(url).netloc.lower()
        async with self._locks.setdefault(host, asyncio.Lock()):
            now = self._clock()
            try:
                allowed = await self._robots.allows(
                    self._client,
                    url,
                    now=now,
                    user_agent=settings.user_agent,
                )
            except ConnectorError:
                if primary:
                    raise
                return None, ""
            if not allowed:
                if primary:
                    raise ConnectorError(f"robots.txt disallows {url}")
                return None, ""
            if self._gap:
                await asyncio.sleep(self._gap)
            try:
                response = await self._client.get(
                    url,
                    headers={"User-Agent": settings.user_agent, "Accept": "text/html,application/xml"},
                )
            except HTTPError as exc:
                raise TransientHTTPError(f"Could not fetch {url}") from exc
        if response.status_code == 403:
            if primary:
                raise SourceForbidden(f"HTTP 403 from {url}")
            return None, ""
        if response.status_code == 404:
            if primary:
                raise SourceNotFound(f"HTTP 404 from {url}")
            return None, ""
        if response.status_code >= 500:
            raise TransientHTTPError(f"HTTP {response.status_code} from {url}")
        if response.status_code != 200:
            raise ConnectorError(f"HTTP {response.status_code} from {url}")
        return response.text, response.headers.get("content-type", "")


def _is_sitemap(url: str, body: str, content_type: str) -> bool:
    sample = body.lstrip()[:200].lower()
    if "<urlset" in sample or "<sitemapindex" in sample:
        return True
    return "xml" in content_type.lower() and urlparse(url).path.lower().endswith(".xml") and "<loc" in body.lower()


def _locs(body: str) -> list[str]:
    return [html.unescape(match).strip() for match in _LOC.findall(body)]


def _source_job_id(posting: dict[str, Any], fallback: str) -> str:
    identifier = posting.get("identifier")
    if isinstance(identifier, str) and identifier.strip():
        return identifier.strip()
    if isinstance(identifier, dict):
        value = identifier.get("value")
        if value is not None and str(value).strip():
            return str(value).strip()
    at_id = _text(posting.get("@id"))
    if at_id:
        return at_id
    url = _text(posting.get("url"))
    if url:
        return url
    digest = hash_payload(posting)
    return digest or fallback


def _organization_name(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        return _text(value.get("name"))
    return None


def _locations(posting: dict[str, Any]) -> list[str]:
    found: list[str] = []
    for key in ("jobLocation", "applicantLocationRequirements"):
        _append_locations(posting.get(key), found)
    return found


def _append_locations(value: Any, found: list[str]) -> None:
    if isinstance(value, list):
        for item in value:
            _append_locations(item, found)
        return
    if isinstance(value, str):
        text = value.strip()
        if text and text not in found:
            found.append(text)
        return
    if not isinstance(value, dict):
        return
    address = value.get("address")
    if isinstance(address, dict):
        parts = [
            _place_name(address.get("addressLocality")),
            _place_name(address.get("addressRegion")),
            _place_name(address.get("addressCountry")),
        ]
        text = ", ".join(part for part in parts if part)
        if text and text not in found:
            found.append(text)
        return
    text = _text(value.get("name"))
    if text and text not in found:
        found.append(text)


def _place_name(value: Any) -> str | None:
    return _organization_name(value)


def _employment_type(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, list):
        parts = [item.strip() for item in value if isinstance(item, str) and item.strip()]
        return ", ".join(parts) or None
    return None


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = isoparse(value)
    except (ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _text(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None
