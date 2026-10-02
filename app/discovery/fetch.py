"""Fetch the single career URL the user supplied."""

import httpx

from app.config import settings


class DiscoveryError(Exception):
    """The career page could not be read. Do not try another identity."""


async def fetch_career_page(url: str) -> str:
    parsed = httpx.URL(url)
    if parsed.scheme not in {"http", "https"} or not parsed.host:
        raise DiscoveryError("career URL must be http or https")
    timeout = httpx.Timeout(settings.request_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        response = await client.get(url, headers={"User-Agent": settings.user_agent, "Accept": "text/html"})
    if response.status_code == 403:
        raise DiscoveryError(f"HTTP 403 from {url}. Not evading; leave this source for review.")
    if response.status_code >= 400:
        raise DiscoveryError(f"HTTP {response.status_code} from {url}")
    return response.text
