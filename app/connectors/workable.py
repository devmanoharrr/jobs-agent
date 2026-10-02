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

ACCOUNT_URL = "https://www.workable.com/api/accounts/{subdomain}"
_MAX_RETRY_AFTER_SECONDS = 30


class WorkableConnector:
    """Public Workable account jobs. This does not call the authenticated SPI.

    The documented account URL may redirect to Workable's public widget endpoint.
    ``details=true`` is required for descriptions. The account name is checked
    against the source record and is not copied into each job payload.
    """

    source_type = "workable"

    def __init__(self, client: AsyncClient | None = None, *, company_name: str | None = None) -> None:
        self._client = client
        self.company_name = company_name

    async def fetch(self, source: Source) -> list[RawJobEnvelope]:
        if source.company_name:
            self.company_name = source.company_name
        url = ACCOUNT_URL.format(subdomain=source.external_key)
        body = await self._get_json(url)
        self._check_account_name(source, body.get("name"))
        fetched_at = datetime.now(timezone.utc)
        envelopes: list[RawJobEnvelope] = []
        for job in body["jobs"]:
            if not isinstance(job, dict):
                continue
            job_id = _job_id(job)
            if job_id is None:
                continue
            job_url = job.get("url") if isinstance(job.get("url"), str) else job.get("shortlink")
            envelopes.append(
                RawJobEnvelope(
                    source_type=self.source_type,
                    source_external_key=source.external_key,
                    source_job_id=job_id,
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
            raise ValueError("Workable job is missing title")
        company = self.company_name.strip() if isinstance(self.company_name, str) else ""
        if not company:
            raise ValueError("Workable job is missing company_name")

        description = job.get("description")
        description_html = html.unescape(description) if isinstance(description, str) and description.strip() else None
        employment = job.get("employment_type")
        apply_url = job.get("application_url")
        return CanonicalJobCandidate(
            title_original=title.strip(),
            company_name=company,
            description_text=html_to_text(description_html),
            description_html=description_html,
            locations_raw=_locations(job),
            employment_type=employment.strip() if isinstance(employment, str) and employment.strip() else None,
            posted_at=_posted_at(job.get("published_on") or job.get("created_at")),
            apply_url=apply_url if isinstance(apply_url, str) else raw.source_url,
            source_job_id=raw.source_job_id,
            source_type=raw.source_type,
            source_external_key=raw.source_external_key,
        )

    def _check_account_name(self, source: Source, account_name: Any) -> None:
        expected = source.company_name.strip() if isinstance(source.company_name, str) else ""
        if not expected:
            return
        if not isinstance(account_name, str) or account_name.strip() != expected:
            raise ConnectorError(f"unexpected company name: {account_name!r}")

    @retry(
        retry=retry_if_exception_type(TransientHTTPError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.2, min=0.2, max=2),
        reraise=True,
    )
    async def _get_json(self, url: str) -> dict[str, Any]:
        if self._client is None:
            raise ConnectorError("Workable fetch requires an HTTP client")
        response = await self._client.get(
            url,
            params={"details": "true"},
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "application/json",
            },
            timeout=settings.request_timeout_seconds,
            follow_redirects=True,
        )
        body = await _read_body(url, response)
        if not isinstance(body, dict) or not isinstance(body.get("jobs"), list):
            raise MalformedPayload(f"Workable response from {url} did not include a jobs array")
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
        raise MalformedPayload(f"Workable response from {url} was not JSON") from exc


def _job_id(job: dict[str, Any]) -> str | None:
    shortcode = job.get("shortcode")
    code = job.get("code")
    if isinstance(shortcode, str) and shortcode.strip():
        return shortcode.strip()
    if isinstance(code, str) and code.strip():
        return code.strip()
    return None


def _locations(job: dict[str, Any]) -> list[str]:
    found: list[str] = []
    locations = job.get("locations")
    if isinstance(locations, list):
        for item in locations:
            if not isinstance(item, dict):
                continue
            label = _place(item.get("city"), item.get("region") or item.get("state"), item.get("country"))
            if label and label not in found:
                found.append(label)
    if found:
        return found
    label = _place(job.get("city"), job.get("state"), job.get("country"))
    return [label] if label else []


def _place(*parts: Any) -> str:
    values = [part.strip() for part in parts if isinstance(part, str) and part.strip()]
    return ", ".join(values)


def _posted_at(value: Any) -> datetime | None:
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
