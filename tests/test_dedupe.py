from app.pipeline.dedupe import (
    DESCRIPTION_THRESHOLD,
    TITLE_THRESHOLD,
    JobIdentity,
    collapse,
    exact_fingerprint,
    find_duplicate,
    source_rank,
)
from app.pipeline.normalize import normalize_company, normalize_location, normalize_url

ENGINEERING = (
    "Build backend services in Python. Design APIs, review code, and mentor engineers "
    "on the payments platform."
)
MARKETING = (
    "Plan brand campaigns, write social copy, and coordinate photoshoots for the retail "
    "marketing calendar."
)


def _job(**overrides: str | None) -> JobIdentity:
    values: dict[str, str | None] = {
        "company_name": "Acme",
        "title": "Backend Engineer",
        "location": "Bengaluru",
        "description": ENGINEERING,
        "requisition_id": None,
        "canonical_url": None,
        "source_type": "greenhouse",
        "source_id": None,
        "source_job_id": None,
    }
    values.update(overrides)
    return JobIdentity(**values)  # type: ignore[arg-type]


def test_exact_fingerprint_ignores_company_suffix_and_location_alias() -> None:
    left = _job(company_name="Acme Pvt. Ltd.", location="Bangalore", requisition_id="REQ-9")
    right = _job(company_name="Acme", location="Bengaluru", requisition_id="req 9")
    assert exact_fingerprint(left) == exact_fingerprint(right)
    assert find_duplicate(right, [left]) == left


def test_same_requisition_from_aggregator_and_employer_collapses_to_one_job() -> None:
    employer = _job(
        title="Backend Engineer",
        requisition_id="REQ-100",
        source_type="greenhouse",
        canonical_url="https://boards.greenhouse.io/acme/jobs/100",
    )
    aggregator = _job(
        company_name="Acme Inc",
        title="Account Executive",
        requisition_id="REQ-100",
        source_type="adzuna",
        canonical_url="https://www.adzuna.com/details/999?utm_source=partner",
    )
    assert exact_fingerprint(employer) != exact_fingerprint(aggregator)
    assert find_duplicate(aggregator, [employer]) == employer
    assert collapse(aggregator, employer) == employer
    assert source_rank("greenhouse") < source_rank("adzuna")


def test_fuzzy_sr_and_senior_merge_only_when_description_threshold_passes() -> None:
    senior = _job(title="Senior Software Engineer", description=ENGINEERING)
    abbreviated = _job(title="Sr Software Engineer", description=ENGINEERING)
    different = _job(title="Sr Software Engineer", description=MARKETING)
    other_city = _job(title="Sr Software Engineer", description=ENGINEERING, location="Mumbai")

    assert find_duplicate(abbreviated, [senior]) == senior
    assert find_duplicate(different, [senior]) is None
    assert find_duplicate(other_city, [senior]) is None
    assert TITLE_THRESHOLD == 92
    assert DESCRIPTION_THRESHOLD == 88


def test_same_title_at_two_companies_never_merges() -> None:
    acme = _job(company_name="Acme", title="Sr Software Engineer", requisition_id="REQ-1")
    globex = _job(
        company_name="Globex",
        title="Sr Software Engineer",
        requisition_id="REQ-1",
        canonical_url="https://example.com/jobs/1?utm_source=board",
    )
    assert find_duplicate(globex, [acme]) is None


def test_tracking_parameters_do_not_split_one_url() -> None:
    posted = _job(
        title="Backend Engineer",
        canonical_url="https://jobs.example.com/roles/1?utm_source=linkedin&gh_src=abc",
    )
    repeat = _job(title="Different title", canonical_url="https://jobs.example.com/roles/1")
    assert normalize_url(posted.canonical_url) == normalize_url(repeat.canonical_url)
    assert exact_fingerprint(posted) != exact_fingerprint(repeat)
    assert find_duplicate(repeat, [posted]) == posted


def test_company_and_location_normalization() -> None:
    assert normalize_company("Acme, Inc.") == "acme"
    assert normalize_location("Gurgaon, Haryana") == "gurugram haryana"
