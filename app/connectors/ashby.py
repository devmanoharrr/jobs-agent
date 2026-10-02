import asyncio
import html
import re
from datetime import datetime, timezone
from typing import Any

from dateutil.parser import isoparse
from httpx import AsyncClient, Response
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import settings
from app.connectors.base import (
    ConnectorError,
    MalformedPayload,
    RateLimited,
    SourceForbidden,
    SourceNotFound,
    TransientHTTPError,
)
from app.models.source import Source
from app.schemas.canonical_job import CanonicalJobCandidate
from app.schemas.raw_job import RawJobEnvelope
from app.utils.text import html_to_text

BOARD_URL = "https://api.ashbyhq.com/posting-api/job-board/{board_name}"
_MAX_RETRY_AFTER_SECONDS = 30


class AshbyConnector:
    """Currently published Ashby job postings.

    The public jobs do not include a company name. Pass it from the source
    record. The stored payload is the job object as returned, including fields
    this normalizer does not map.
    """

    source_type = "ashby"

    def __init__(self, client: AsyncClient | None = None, *, company_name: str | None = None) -> None:
        self._client = client
        self.company_name = company_name

    async def fetch(self, source: Source) -> list[RawJobEnvelope]:
        if source.company_name:
            self.company_name = source.company_name
        url = BOARD_URL.format(board_name=source.external_key)
        body = await self._get_json(url)
        fetched_at = datetime.now(timezone.utc)
        envelopes: list[RawJobEnvelope] = []
        for job in body["jobs"]:
            if not isinstance(job, dict) or job.get("id") is None:
                continue
            job_url = job.get("jobUrl")
            envelopes.append(
                RawJobEnvelope(
                    source_type=self.source_type,
                    source_external_key=source.external_key,
                    source_job_id=str(job["id"]),
                    source_url=job_url if isinstance(job_url, str) else None,
                    fetched_at=fetched_at,
                    payload=job,
                )
            )
        return envelopes

    def normalize(self, raw: RawJobEnvelope) -> CanonicalJobCandidate:
        job = raw.payload
        title = job.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ValueError("Ashby job is missing title")
        company = self.company_name.strip() if isinstance(self.company_name, str) else ""
        if not company:
            raise ValueError("Ashby job is missing company_name")

        description_html, description_text = _descriptions(job)
        apply_url = job.get("applyUrl")
        employment = job.get("employmentType")
        return CanonicalJobCandidate(
            title_original=title.strip(),
            company_name=company,
            description_text=description_text,
            description_html=description_html,
            locations_raw=_locations(job),
            employment_type=employment.strip() if isinstance(employment, str) and employment.strip() else None,
            posted_at=_published_at(job.get("publishedAt")),
            apply_url=apply_url if isinstance(apply_url, str) else raw.source_url,
            source_job_id=raw.source_job_id,
            source_type=raw.source_type,
            source_external_key=raw.source_external_key,
        )

    @retry(
        retry=retry_if_exception_type(TransientHTTPError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.2, min=0.2, max=2),
        reraise=True,
    )
    async def _get_json(self, url: str) -> dict[str, Any]:
        if self._client is None:
            raise ConnectorError("Ashby fetch requires an HTTP client")
        response = await self._client.get(
            url,
            params={"includeCompensation": "true"},
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "application/json",
            },
            timeout=settings.request_timeout_seconds,
        )
        body = await _read_body(url, response)
        if not isinstance(body, dict) or not isinstance(body.get("jobs"), list):
            raise MalformedPayload(f"Ashby response from {url} did not include a jobs array")
        return body


async def _read_body(url: str, response: Response) -> Any:
    if response.status_code == 429:
        delay = _retry_after_seconds(response.headers.get("Retry-After"))
        if delay is not None and delay <= _MAX_RETRY_AFTER_SECONDS:
            await asyncio.sleep(delay)
            raise TransientHTTPError(f"HTTP 429 from {url}")
        raise RateLimited(f"HTTP 429 from {url}")
    if response.status_code == 404:
        raise SourceNotFound(f"HTTP 404 from {url}")
    if response.status_code == 403:
        raise SourceForbidden(f"HTTP 403 from {url}")
    if response.status_code >= 500:
        raise TransientHTTPError(f"HTTP {response.status_code} from {url}")
    if response.status_code != 200:
        raise ConnectorError(f"HTTP {response.status_code} from {url}")
    try:
        return response.json()
    except ValueError as exc:
        raise MalformedPayload(f"Ashby response from {url} was not JSON") from exc


def _locations(job: dict[str, Any]) -> list[str]:
    found: list[str] = []
    location = job.get("location")
    if isinstance(location, str) and location.strip():
        found.append(location.strip())
    secondary = job.get("secondaryLocations")
    if isinstance(secondary, list):
        for item in secondary:
            name = item if isinstance(item, str) else item.get("location") if isinstance(item, dict) else None
            if isinstance(name, str) and name.strip() and name.strip() not in found:
                found.append(name.strip())
    return found


def _descriptions(job: dict[str, Any]) -> tuple[str | None, str | None]:
    html_value = job.get("descriptionHtml")
    plain = job.get("descriptionPlain")
    description_html = html.unescape(html_value) if isinstance(html_value, str) and html_value.strip() else None
    if isinstance(plain, str) and plain.strip():
        description_text = re.sub(r"\s+", " ", plain).strip() or None
    else:
        description_text = html_to_text(description_html)
    return description_html, description_text


def _published_at(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = isoparse(value)
    except (ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _retry_after_seconds(header: str | None) -> float | None:
    if header is None:
        return None
    try:
        return float(header)
    except ValueError:
        return None
