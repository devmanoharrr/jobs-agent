import json
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

_DATA = Path(__file__).resolve().parents[2] / "data"
_SUFFIXES = {
    "inc",
    "incorporated",
    "llc",
    "ltd",
    "limited",
    "pvt",
    "private",
    "llp",
    "plc",
    "corp",
    "corporation",
    "co",
    "company",
}
_TITLE_ABBREVIATIONS = {"sr": "senior", "jr": "junior"}
_TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "gclid",
    "fbclid",
    "gh_src",
    "lever-source",
}
GENERIC_LOCATION_TOKENS = {"india", "remote", "hybrid", "onsite", "office", "hq"}


def normalize_company(name: str) -> str:
    tokens = _tokens(name)
    while len(tokens) > 1 and tokens[-1] in _SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def normalize_title(title: str) -> str:
    return " ".join(_tokens(title))


def fuzzy_title(title: str) -> str:
    """Title used only for similarity. Abbreviations are expanded here, not in the exact key."""
    return " ".join(_TITLE_ABBREVIATIONS.get(token, token) for token in _tokens(title))


def normalize_location(location: str) -> str:
    text = location.lower()
    text = _alias_pattern().sub(lambda match: _aliases()[match.group(0)], text)
    return " ".join(_tokens(text))


def normalize_requisition(requisition_id: str | None) -> str:
    if requisition_id is None:
        return ""
    return " ".join(_tokens(requisition_id))


def normalize_url(url: str | None) -> str | None:
    if url is None or not url.strip():
        return None
    parsed = urlparse(url.strip())
    if not parsed.scheme or not parsed.netloc:
        return None
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in _TRACKING_PARAMS
    ]
    cleaned = parsed._replace(query=urlencode(query), fragment="")
    return urlunparse(cleaned).rstrip("/").lower()


def token_fingerprint(text: str | None) -> str:
    if text is None or not text.strip():
        return ""
    tokens = {token for token in re.findall(r"[a-z0-9]+", text.lower()) if len(token) > 2}
    return " ".join(sorted(tokens))


def _tokens(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", value.lower())


@lru_cache(maxsize=1)
def _aliases() -> dict[str, str]:
    raw = json.loads((_DATA / "location_aliases.json").read_text())
    return {key.lower(): value.lower() for key, value in raw.items()}


@lru_cache(maxsize=1)
def _alias_pattern() -> re.Pattern[str]:
    keys = sorted(_aliases(), key=len, reverse=True)
    return re.compile(r"\b(?:" + "|".join(re.escape(key) for key in keys) + r")\b")
