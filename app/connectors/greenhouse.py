import asyncio
import html
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

JOBS_URL = "https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs"
_MAX_RETRY_AFTER_SECONDS = 30


class GreenhouseConnector:
    source_type = "greenhouse"

    def __init__(self, client: AsyncClient | None = None) -> None:
        self._client = client

    async def fetch(self, source: Source) -> list[RawJobEnvelope]:
        url = JOBS_URL.format(board_token=source.external_key)
        body = await self._get_json(url)
        fetched_at = datetime.now(timezone.utc)
        envelopes: list[RawJobEnvelope] = []
        for job in body["jobs"]:
            if not isinstance(job, dict) or job.get("id") is None:
                continue
            absolute_url = job.get("absolute_url")
            envelopes.append(
                RawJobEnvelope(
                    source_type=self.source_type,
                    source_external_key=source.external_key,
                    source_job_id=str(job["id"]),
                    source_url=absolute_url if isinstance(absolute_url, str) else None,
                    fetched_at=fetched_at,
                    payload=job,
                )
            )
        return envelopes

    def normalize(self, raw: RawJobEnvelope) -> CanonicalJobCandidate:
        job = raw.payload
        title = job.get("title")
        company = job.get("company_name")
        if not isinstance(title, str) or not title.strip():
            raise ValueError("Greenhouse job is missing title")
        if not isinstance(company, str) or not company.strip():
            raise ValueError("Greenhouse job is missing company_name")

        content = job.get("content")
        decoded = html.unescape(content) if isinstance(content, str) else None
        return CanonicalJobCandidate(
            title_original=title.strip(),
            company_name=company.strip(),
            description_text=html_to_text(decoded),
            description_html=decoded,
            locations_raw=_locations(job.get("location")),
            posted_at=_posted_at(job),
            apply_url=raw.source_url,
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
            raise ConnectorError("Greenhouse fetch requires an HTTP client")
        response = await self._client.get(
            url,
            params={"content": "true"},
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "application/json",
            },
            timeout=settings.request_timeout_seconds,
        )
        return await _interpret_response(url, response)


async def _interpret_response(url: str, response: Response) -> dict[str, Any]:
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
        body = response.json()
    except ValueError as exc:
        raise MalformedPayload(f"Greenhouse response from {url} was not JSON") from exc
    if not isinstance(body, dict) or not isinstance(body.get("jobs"), list):
        raise MalformedPayload(f"Greenhouse response from {url} did not include a jobs array")
    return body


def _locations(location: Any) -> list[str]:
    if not isinstance(location, dict):
        return []
    name = location.get("name")
    if not isinstance(name, str):
        return []
    return [part.strip() for part in name.split(";") if part.strip()]


def _posted_at(job: dict[str, Any]) -> datetime | None:
    raw_value = job.get("first_published") or job.get("updated_at")
    if not isinstance(raw_value, str) or not raw_value.strip():
        return None
    try:
        parsed = isoparse(raw_value)
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
