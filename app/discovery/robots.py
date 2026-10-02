"""robots.txt checks for the generic JSON-LD fetcher.

A fresh file is kept for 24 hours. An older copy is used only when the origin
cannot be reached. 401 and 403 disallow the origin. 404 and 410 allow it.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

from app.connectors.base import ConnectorError

ROBOTS_MAX_AGE = timedelta(hours=24)


class _RobotsUnreachable(Exception):
    pass


@dataclass
class _Record:
    fetched_at: datetime
    mode: str
    parser: RobotFileParser | None


class RobotsCache:
    def __init__(self, max_age: timedelta = ROBOTS_MAX_AGE) -> None:
        self.max_age = max_age
        self._records: dict[str, _Record] = {}

    async def allows(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        now: datetime,
        user_agent: str,
    ) -> bool:
        origin = _origin(url)
        record = self._records.get(origin)
        fresh = record is not None and now - record.fetched_at <= self.max_age
        if not fresh:
            try:
                record = await _load(client, origin, now, user_agent)
            except _RobotsUnreachable as exc:
                if record is None:
                    raise ConnectorError(f"robots.txt for {origin} is unreachable") from exc
            else:
                self._records[origin] = record
        assert record is not None
        if record.mode == "allow":
            return True
        if record.mode == "disallow":
            return False
        assert record.parser is not None
        return bool(record.parser.can_fetch(user_agent, url))


async def _load(client: httpx.AsyncClient, origin: str, now: datetime, user_agent: str) -> _Record:
    try:
        response = await client.get(
            f"{origin}/robots.txt",
            headers={"User-Agent": user_agent, "Accept": "text/plain"},
        )
    except httpx.HTTPError as exc:
        raise _RobotsUnreachable(str(exc)) from exc
    if response.status_code in {404, 410}:
        return _Record(fetched_at=now, mode="allow", parser=None)
    if response.status_code in {401, 403}:
        return _Record(fetched_at=now, mode="disallow", parser=None)
    if response.status_code != 200:
        raise _RobotsUnreachable(f"HTTP {response.status_code}")
    parser = RobotFileParser()
    parser.parse(response.text.splitlines())
    return _Record(fetched_at=now, mode="rules", parser=parser)


def _origin(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConnectorError(f"Cannot read robots.txt for {url}")
    return f"{parsed.scheme}://{parsed.netloc}"
