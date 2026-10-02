from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class RawJobEnvelope(BaseModel):
    """Unmodified source payload. Connectors return these; they do not write jobs."""

    model_config = ConfigDict(extra="forbid")

    source_type: str
    source_external_key: str
    source_job_id: str
    source_url: str | None
    fetched_at: datetime
    payload: dict[str, Any]
