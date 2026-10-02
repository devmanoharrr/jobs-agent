import hashlib
from dataclasses import dataclass

from rapidfuzz import fuzz

from app.pipeline.normalize import (
    GENERIC_LOCATION_TOKENS,
    fuzzy_title,
    normalize_company,
    normalize_location,
    normalize_requisition,
    normalize_title,
    normalize_url,
    token_fingerprint,
)

TITLE_THRESHOLD = 92
DESCRIPTION_THRESHOLD = 88

_EMPLOYER_ATS = {"greenhouse", "lever", "ashby", "workable"}
_AGGREGATORS = {"adzuna"}


@dataclass(frozen=True)
class JobIdentity:
    company_name: str
    title: str
    location: str
    description: str | None = None
    requisition_id: str | None = None
    canonical_url: str | None = None
    source_type: str = "greenhouse"
    source_id: str | None = None
    source_job_id: str | None = None


def exact_fingerprint(job: JobIdentity) -> str:
    raw = "|".join(
        (
            normalize_company(job.company_name),
            normalize_title(job.title),
            normalize_location(job.location),
            normalize_requisition(job.requisition_id),
        )
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def fuzzy_fingerprint(description: str | None) -> str:
    return token_fingerprint(description)


def source_rank(source_type: str) -> int:
    """Lower is better: employer ATS, public feed, aggregator, then generic copy."""
    if source_type in _EMPLOYER_ATS:
        return 0
    if source_type in _AGGREGATORS:
        return 2
    if source_type == "generic":
        return 3
    return 1


def find_duplicate(candidate: JobIdentity, existing: list[JobIdentity]) -> JobIdentity | None:
    """Exact keys first. Fuzzy comparison only inside the same company and location."""
    fingerprint = exact_fingerprint(candidate)
    url = normalize_url(candidate.canonical_url)
    requisition = _requisition_key(candidate)
    for item in existing:
        if exact_fingerprint(item) == fingerprint:
            return item
        if _same_source_posting(candidate, item):
            return item
        item_url = normalize_url(item.canonical_url)
        if url is not None and url == item_url:
            return item
        if requisition is not None and requisition == _requisition_key(item):
            return item
    for item in existing:
        if _fuzzy_match(candidate, item):
            return item
    return None


def collapse(current: JobIdentity, incoming: JobIdentity) -> JobIdentity:
    """Keep one record. An employer-owned source replaces an aggregator canonical."""
    if find_duplicate(incoming, [current]) is None:
        raise ValueError("records are not duplicates")
    if source_rank(incoming.source_type) < source_rank(current.source_type):
        return incoming
    return current


def _fuzzy_match(candidate: JobIdentity, item: JobIdentity) -> bool:
    if normalize_company(candidate.company_name) != normalize_company(item.company_name):
        return False
    if not _locations_match(candidate.location, item.location):
        return False
    if fuzz.ratio(fuzzy_title(candidate.title), fuzzy_title(item.title)) < TITLE_THRESHOLD:
        return False
    return _description_similarity(candidate.description, item.description) >= DESCRIPTION_THRESHOLD


def _description_similarity(left: str | None, right: str | None) -> float:
    left_fp = token_fingerprint(left)
    right_fp = token_fingerprint(right)
    if not left_fp or not right_fp:
        return 0.0
    return float(fuzz.token_set_ratio(left_fp, right_fp))


def _locations_match(left: str, right: str) -> bool:
    left_norm = normalize_location(left)
    right_norm = normalize_location(right)
    if left_norm == right_norm:
        return True
    left_tokens = set(left_norm.split())
    right_tokens = set(right_norm.split())
    if not left_tokens or not right_tokens:
        return False
    specific = (left_tokens & right_tokens) - GENERIC_LOCATION_TOKENS
    if not specific:
        return False
    smaller, larger = (
        (left_tokens, right_tokens) if len(left_tokens) <= len(right_tokens) else (right_tokens, left_tokens)
    )
    return smaller <= larger


def _requisition_key(job: JobIdentity) -> str | None:
    requisition = normalize_requisition(job.requisition_id)
    if not requisition:
        return None
    return f"{normalize_company(job.company_name)}|{requisition}"


def _same_source_posting(left: JobIdentity, right: JobIdentity) -> bool:
    return (
        left.source_id is not None
        and left.source_id == right.source_id
        and left.source_job_id is not None
        and left.source_job_id == right.source_job_id
    )
