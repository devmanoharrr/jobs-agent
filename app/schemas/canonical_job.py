from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class CanonicalJobCandidate(BaseModel):
    """Source-neutral job fields produced by a normalizer, before India filtering or dedupe."""

    model_config = ConfigDict(extra="forbid")

    title_original: str
    company_name: str
    description_text: str | None = None
    description_html: str | None = None
    locations_raw: list[str] = Field(default_factory=list)
    employment_type: str | None = None
    posted_at: datetime | None = None
    expires_at: datetime | None = None
    apply_url: str | None = None
    source_job_id: str
    source_type: str
    source_external_key: str
