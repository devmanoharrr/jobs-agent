from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.schemas import CanonicalJobCandidate, RawJobEnvelope


def test_raw_job_envelope_requires_payload_fields() -> None:
    fetched_at = datetime(2026, 10, 1, tzinfo=timezone.utc)
    envelope = RawJobEnvelope(
        source_type="greenhouse",
        source_external_key="example",
        source_job_id="123",
        source_url="https://boards.greenhouse.io/example/jobs/123",
        fetched_at=fetched_at,
        payload={"id": 123, "title": "Engineer"},
    )
    assert envelope.source_job_id == "123"
    assert envelope.payload["title"] == "Engineer"


def test_raw_job_envelope_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        RawJobEnvelope(
            source_type="greenhouse",
            source_external_key="example",
            source_job_id="123",
            source_url=None,
            fetched_at=datetime.now(timezone.utc),
            payload={},
            extra_field=True,
        )


def test_canonical_candidate_keeps_separate_location_lists() -> None:
    first = CanonicalJobCandidate(
        title_original="Software Engineer",
        company_name="Example",
        source_job_id="123",
        source_type="lever",
        source_external_key="example",
    )
    second = CanonicalJobCandidate(
        title_original="Software Engineer",
        company_name="Example",
        source_job_id="456",
        source_type="lever",
        source_external_key="example",
    )
    first.locations_raw.append("Bengaluru")
    assert second.locations_raw == []
    assert first.description_text is None
    assert first.apply_url is None
