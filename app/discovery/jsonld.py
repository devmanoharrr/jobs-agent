"""Find schema.org JobPosting objects in JSON-LD script blocks."""

import json
import re
from typing import Any

_SCRIPT = re.compile(r"<script\b([^>]*)>(.*?)</script>", re.IGNORECASE | re.DOTALL)
_TYPE_ATTR = re.compile(r"""type\s*=\s*["']([^"']+)["']""", re.IGNORECASE)


def contains_job_posting(html: str) -> bool:
    return bool(iter_job_postings(html))


def iter_job_postings(html: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    seen: set[int] = set()
    for payload in _payloads(html):
        _collect(payload, found, seen)
    return found


def _payloads(html: str) -> list[Any]:
    payloads: list[Any] = []
    for attrs, body in _SCRIPT.findall(html):
        match = _TYPE_ATTR.search(attrs)
        if match is None or match.group(1).lower() != "application/ld+json":
            continue
        try:
            payloads.append(json.loads(body))
        except json.JSONDecodeError:
            continue
    return payloads


def _collect(value: Any, found: list[dict[str, Any]], seen: set[int]) -> None:
    if isinstance(value, dict):
        marker = id(value)
        if marker in seen:
            return
        seen.add(marker)
        if _type_is_job_posting(value.get("@type")):
            found.append(value)
        for item in value.values():
            _collect(item, found, seen)
        return
    if isinstance(value, list):
        for item in value:
            _collect(item, found, seen)


def _type_is_job_posting(value: Any) -> bool:
    if isinstance(value, str):
        return value.rstrip("/").rsplit("/", 1)[-1] == "JobPosting"
    if isinstance(value, list):
        return any(_type_is_job_posting(item) for item in value)
    return False
