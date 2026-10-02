import json
from datetime import datetime, timezone
from pathlib import Path

from app.connectors.ashby import AshbyConnector
from app.connectors.greenhouse import GreenhouseConnector
from app.connectors.lever import LeverConnector
from app.connectors.workable import WorkableConnector
from app.pipeline.india_filter import IndiaRelevance, classify_india, is_publishable
from app.schemas.canonical_job import CanonicalJobCandidate
from app.schemas.raw_job import RawJobEnvelope

FETCHED_AT = datetime(2026, 10, 1, tzinfo=timezone.utc)

CORPUS = Path(__file__).parent / "fixtures" / "india_filter_corpus.json"
FIXTURES = Path(__file__).parent / "fixtures"


def _candidate(
    locations: list[str],
    *,
    title: str = "Engineer",
    description: str | None = None,
) -> CanonicalJobCandidate:
    return CanonicalJobCandidate(
        title_original=title,
        company_name="Example",
        description_text=description,
        locations_raw=locations,
        source_job_id="1",
        source_type="greenhouse",
        source_external_key="example",
    )


def test_location_aliases_cover_the_required_renames() -> None:
    aliases = json.loads((Path(__file__).parents[1] / "data" / "location_aliases.json").read_text())
    assert aliases == {"bangalore": "bengaluru", "bombay": "mumbai", "gurgaon": "gurugram"}


def test_india_filter_corpus() -> None:
    cases = json.loads(CORPUS.read_text())
    assert len({case["name"] for case in cases}) == len(cases)
    for case in cases:
        relevance = classify_india(
            _candidate(
                case["locations"],
                title=case.get("title", "Engineer"),
                description=case.get("description"),
            ),
            country_code=case.get("country_code"),
        )
        assert relevance == case["expected"], case["name"]
        assert is_publishable(relevance) is (
            relevance in {IndiaRelevance.INDIA, IndiaRelevance.REMOTE_INDIA}
        )


def test_india_filter_on_saved_source_jobs() -> None:
    groww = json.loads((FIXTURES / "greenhouse_groww.json").read_text())["jobs"][0]
    cred = json.loads((FIXTURES / "lever_cred.json").read_text())[1]
    temporal = json.loads((FIXTURES / "ashby_temporal.json").read_text())["jobs"][0]
    epignosis = json.loads((FIXTURES / "workable_epignosis.json").read_text())["jobs"][0]

    cases = [
        (
            GreenhouseConnector().normalize(
                RawJobEnvelope(
                    source_type="greenhouse",
                    source_external_key="groww",
                    source_job_id=str(groww["id"]),
                    source_url=groww["absolute_url"],
                    fetched_at=FETCHED_AT,
                    payload=groww,
                )
            ),
            IndiaRelevance.INDIA,
        ),
        (
            LeverConnector(company_name="CRED").normalize(
                RawJobEnvelope(
                    source_type="lever",
                    source_external_key="cred",
                    source_job_id=cred["id"],
                    source_url=cred["hostedUrl"],
                    fetched_at=FETCHED_AT,
                    payload=cred,
                )
            ),
            IndiaRelevance.INDIA,
        ),
        (
            AshbyConnector(company_name="Temporal").normalize(
                RawJobEnvelope(
                    source_type="ashby",
                    source_external_key="temporal",
                    source_job_id=temporal["id"],
                    source_url=temporal["jobUrl"],
                    fetched_at=FETCHED_AT,
                    payload=temporal,
                )
            ),
            IndiaRelevance.INDIA,
        ),
        (
            WorkableConnector(company_name="Epignosis").normalize(
                RawJobEnvelope(
                    source_type="workable",
                    source_external_key="epignosis",
                    source_job_id=epignosis["shortcode"],
                    source_url=epignosis["url"],
                    fetched_at=FETCHED_AT,
                    payload=epignosis,
                )
            ),
            IndiaRelevance.NOT_INDIA,
        ),
    ]
    for candidate, expected in cases:
        assert classify_india(candidate) == expected
