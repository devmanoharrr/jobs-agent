from typing import Protocol

from app.models.source import Source
from app.schemas.canonical_job import CanonicalJobCandidate
from app.schemas.raw_job import RawJobEnvelope


class ConnectorError(Exception):
    """A source request failed and should not be retried as a transient error."""


class TransientHTTPError(ConnectorError):
    """Timeout or 5xx. Safe to retry."""


class RateLimited(ConnectorError):
    """HTTP 429. The source run should stop rather than evade the limit."""


class SourceNotFound(ConnectorError):
    """HTTP 404. The board token or site name is not a public source."""


class SourceForbidden(ConnectorError):
    """HTTP 403. Do not evade; the source needs review."""


class MalformedPayload(ConnectorError):
    """The response was not the JSON shape this connector understands."""


class JobConnector(Protocol):
    source_type: str

    async def fetch(self, source: Source) -> list[RawJobEnvelope]: ...

    def normalize(self, raw: RawJobEnvelope) -> CanonicalJobCandidate: ...
