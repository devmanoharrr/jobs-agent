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

POSTINGS_URL = "https://api.lever.co/v0/postings/{site}"
_MAX_RETRY_AFTER_SECONDS = 30


class LeverConnector:
    """Published Lever postings only.

    Lever's public JSON does not include a company name. Pass the name from the
    source record; it is not written into the stored posting.
    """

    source_type = "lever"

    def __init__(self, client: AsyncClient | None = None, *, company_name: str | None = None) -> None:
        self._client = client
        self.company_name = company_name

    async def fetch(self, source: Source) -> list[RawJobEnvelope]:
        if source.company_name:
            self.company_name = source.company_name
        url = POSTINGS_URL.format(site=source.external_key)
        postings = await self._get_json(url)
        fetched_at = datetime.now(timezone.utc)
        envelopes: list[RawJobEnvelope] = []
        for posting in postings:
            if not isinstance(posting, dict) or posting.get("id") is None:
                continue
            hosted_url = posting.get("hostedUrl")
            envelopes.append(
                RawJobEnvelope(
                    source_type=self.source_type,
                    source_external_key=source.external_key,
                    source_job_id=str(posting["id"]),
                    source_url=hosted_url if isinstance(hosted_url, str) else None,
                    fetched_at=fetched_at,
                    payload=posting,
                )
            )
        return envelopes

    def normalize(self, raw: RawJobEnvelope) -> CanonicalJobCandidate:
        posting = raw.payload
        title = posting.get("text")
        if not isinstance(title, str) or not title.strip():
            raise ValueError("Lever posting is missing text")
        company = self.company_name.strip() if isinstance(self.company_name, str) else ""
        if not company:
            raise ValueError("Lever posting is missing company_name")

        description_html, description_text = _descriptions(posting)
        apply_url = posting.get("applyUrl")
        return CanonicalJobCandidate(
            title_original=title.strip(),
            company_name=company,
            description_text=description_text,
            description_html=description_html,
            locations_raw=_locations(posting.get("categories")),
            employment_type=_employment(posting.get("categories")),
            posted_at=_created_at(posting.get("createdAt")),
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
    async def _get_json(self, url: str) -> list[Any]:
        if self._client is None:
            raise ConnectorError("Lever fetch requires an HTTP client")
        response = await self._client.get(
            url,
            params={"mode": "json"},
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "application/json",
            },
            timeout=settings.request_timeout_seconds,
        )
        body = await _read_body(url, response)
        if not isinstance(body, list):
            raise MalformedPayload(f"Lever response from {url} was not a postings array")
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
        raise MalformedPayload(f"Lever response from {url} was not JSON") from exc


def _locations(categories: Any) -> list[str]:
    if not isinstance(categories, dict):
        return []
    found: list[str] = []
    all_locations = categories.get("allLocations")
    if isinstance(all_locations, list):
        for item in all_locations:
            if isinstance(item, str) and item.strip():
                found.append(item.strip())
    location = categories.get("location")
    if isinstance(location, str) and location.strip() and location.strip() not in found:
        found.insert(0, location.strip())
    return found


def _employment(categories: Any) -> str | None:
    if not isinstance(categories, dict):
        return None
    commitment = categories.get("commitment")
    if isinstance(commitment, str) and commitment.strip():
        return commitment.strip()
    return None


def _descriptions(posting: dict[str, Any]) -> tuple[str | None, str | None]:
    description = posting.get("description")
    plain = posting.get("descriptionPlain")
    description_html = html.unescape(description) if isinstance(description, str) and description.strip() else None
    if isinstance(plain, str) and plain.strip():
        description_text = re.sub(r"\s+", " ", plain).strip() or None
    else:
        description_text = html_to_text(description_html)
    return description_html, description_text


def _created_at(value: Any) -> datetime | None:
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 10_000_000_000 else value
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str) and value.strip():
        try:
            parsed = isoparse(value)
        except (ValueError, OverflowError):
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed
    return None


def _retry_after_seconds(header: str | None) -> float | None:
    if header is None:
        return None
    try:
        return float(header)
    except ValueError:
        return None
