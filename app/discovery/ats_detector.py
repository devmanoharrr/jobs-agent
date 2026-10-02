"""Identify the ATS behind one career URL.

The detector reads the URL and that page's HTML. It does not follow links,
insert a source, or enable crawling.
"""

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

from app.discovery.jsonld import contains_job_posting

_URL = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_GREENHOUSE_HOST = re.compile(
    r"^(?:job-boards(?:\.eu)?|boards-api|boards)\.greenhouse\.io$",
    re.IGNORECASE,
)
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,80}$")


@dataclass(frozen=True)
class Detection:
    status: str
    source_type: str | None
    external_key: str | None
    evidence: str


@dataclass(frozen=True)
class _Hit:
    source_type: str
    external_key: str | None
    evidence: str


def detect(page_url: str, html: str = "") -> Detection:
    hits = _ats_hits(page_url, html)
    kinds = sorted({hit.source_type for hit in hits})
    if len(kinds) > 1:
        return Detection(
            status="needs-review",
            source_type=None,
            external_key=None,
            evidence="multiple ATS matches: " + ", ".join(kinds),
        )
    if len(kinds) == 1:
        keys = sorted({hit.external_key for hit in hits if hit.external_key})
        if len(keys) > 1:
            return Detection(
                status="needs-review",
                source_type=None,
                external_key=None,
                evidence=f"multiple {kinds[0]} keys: " + ", ".join(keys),
            )
        return Detection(
            status="detected",
            source_type=kinds[0],
            external_key=keys[0] if keys else None,
            evidence=hits[0].evidence,
        )
    if contains_job_posting(html):
        return Detection(
            status="detected",
            source_type="generic-jsonld",
            external_key=None,
            evidence="schema.org JobPosting JSON-LD",
        )
    return Detection(
        status="needs-review",
        source_type=None,
        external_key=None,
        evidence="no supported ATS or JobPosting JSON-LD",
    )


def _ats_hits(page_url: str, html: str) -> list[_Hit]:
    hits: list[_Hit] = []
    seen: set[tuple[str, str | None, str]] = set()
    for candidate in [page_url, *_URL.findall(html)]:
        hit = _classify(candidate)
        if hit is None:
            continue
        identity = (hit.source_type, hit.external_key, hit.evidence)
        if identity in seen:
            continue
        seen.add(identity)
        hits.append(hit)
    return hits


def _classify(candidate: str) -> _Hit | None:
    parsed = urlparse(candidate)
    host = parsed.netloc.lower().split("@")[-1].split(":")[0]
    path = [part for part in parsed.path.split("/") if part]
    if _GREENHOUSE_HOST.match(host):
        return _Hit("greenhouse", _greenhouse_key(path, parsed.query), candidate)
    if host == "jobs.lever.co":
        return _Hit("lever", _slug(path[0] if path else None), candidate)
    if host == "jobs.ashbyhq.com":
        return _Hit("ashby", _slug(path[0] if path else None), candidate)
    if host == "apply.workable.com" or host.endswith(".workable.com") or host == "workable.com":
        return _Hit("workable", _workable_key(host, path), candidate)
    return None


def _greenhouse_key(path: list[str], query: str) -> str | None:
    if "boards" in path:
        index = path.index("boards")
        if index + 1 < len(path):
            return _slug(path[index + 1])
    for_token = parse_qs(query).get("for", [None])[0]
    if for_token:
        return _slug(for_token)
    if path and path[0] not in {"v1", "embed", "jobs"}:
        return _slug(path[0])
    return None


def _workable_key(host: str, path: list[str]) -> str | None:
    if host == "apply.workable.com":
        return _slug(path[0] if path else None)
    labels = host.split(".")
    if len(labels) > 2 and labels[0] not in {"www", "apply", "api"}:
        return _slug(labels[0])
    return _slug(path[0] if path else None)


def _slug(value: str | None) -> str | None:
    if value is None:
        return None
    token = value.strip()
    if _SLUG.fullmatch(token):
        return token
    return None
