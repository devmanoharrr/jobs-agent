import json
import re
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from app.schemas.canonical_job import CanonicalJobCandidate

_DATA = Path(__file__).resolve().parents[2] / "data"

_REMOTE_INDIA_PHRASES = (
    "india only",
    "only in india",
    "candidates in india",
    "candidate in india",
    "based in india",
    "located in india",
    "residing in india",
    "residents of india",
    "within india",
    "anywhere in india",
    "open to india",
    "must be in india",
    "working from india",
    "remote india",
    "remote, india",
    "remote - india",
)

_INDIA_EXCLUSIONS = (
    "except india",
    "excluding india",
    "outside india",
    "outside of india",
    "not in india",
    "not available in india",
    "not open to india",
    "no candidates in india",
    "ineligible in india",
)

_US_ONLY_PHRASES = (
    "us only",
    "usa only",
    "u.s. only",
    "u.s.a. only",
    "united states only",
    "us-only",
    "usa-only",
)

_REMOTE_SIGNALS = (
    "remote",
    "worldwide",
    "work from anywhere",
    "work from home",
    "anywhere",
    "fully remote",
    "remote role",
    "remote position",
    "distributed",
)

_FOREIGN_COUNTRIES = (
    "united states of america",
    "united states",
    "united kingdom",
    "united arab emirates",
    "south africa",
    "south korea",
    "new zealand",
    "sri lanka",
    "hong kong",
    "saudi arabia",
    "united states",
    "usa",
    "u.s.a.",
    "u.s.",
    "uk",
    "england",
    "scotland",
    "wales",
    "canada",
    "australia",
    "germany",
    "france",
    "singapore",
    "netherlands",
    "ireland",
    "japan",
    "china",
    "uae",
    "dubai",
    "abu dhabi",
    "brazil",
    "mexico",
    "spain",
    "italy",
    "sweden",
    "norway",
    "denmark",
    "finland",
    "poland",
    "portugal",
    "switzerland",
    "austria",
    "belgium",
    "nigeria",
    "kenya",
    "indonesia",
    "philippines",
    "vietnam",
    "thailand",
    "malaysia",
    "korea",
    "taiwan",
    "israel",
    "romania",
    "ukraine",
    "turkey",
    "argentina",
    "chile",
    "colombia",
    "egypt",
    "qatar",
    "pakistan",
    "bangladesh",
    "nepal",
    "bhutan",
    "greece",
)

_FOREIGN_CITIES = (
    "san francisco",
    "new york",
    "los angeles",
    "seattle",
    "austin",
    "boston",
    "chicago",
    "denver",
    "atlanta",
    "dallas",
    "houston",
    "miami",
    "indianapolis",
    "london",
    "berlin",
    "munich",
    "paris",
    "amsterdam",
    "dublin",
    "toronto",
    "vancouver",
    "sydney",
    "melbourne",
    "singapore",
    "dubai",
    "tokyo",
    "athens",
    "heraklion",
)

_US_STATE_NAMES = (
    "alabama",
    "alaska",
    "arizona",
    "arkansas",
    "california",
    "colorado",
    "connecticut",
    "delaware",
    "florida",
    "georgia",
    "hawaii",
    "idaho",
    "illinois",
    "indiana",
    "iowa",
    "kansas",
    "kentucky",
    "louisiana",
    "maine",
    "maryland",
    "massachusetts",
    "michigan",
    "minnesota",
    "mississippi",
    "missouri",
    "montana",
    "nebraska",
    "nevada",
    "new hampshire",
    "new jersey",
    "new mexico",
    "new york",
    "north carolina",
    "north dakota",
    "ohio",
    "oklahoma",
    "oregon",
    "pennsylvania",
    "rhode island",
    "south carolina",
    "south dakota",
    "tennessee",
    "texas",
    "utah",
    "vermont",
    "virginia",
    "washington",
    "west virginia",
    "wisconsin",
    "wyoming",
    "district of columbia",
)


class IndiaRelevance(StrEnum):
    INDIA = "india"
    REMOTE_INDIA = "remote_india"
    NOT_INDIA = "not_india"
    UNKNOWN_REMOTE = "unknown_remote"
    UNKNOWN = "unknown"


def is_publishable(relevance: IndiaRelevance) -> bool:
    return relevance in {IndiaRelevance.INDIA, IndiaRelevance.REMOTE_INDIA}


def classify_india(
    candidate: CanonicalJobCandidate,
    *,
    country_code: str | None = None,
) -> IndiaRelevance:
    """Deterministic India cascade. Ambiguous jobs stay unknown and are not guessed."""
    code = _normalize_country_code(country_code)
    if code == "IN":
        return IndiaRelevance.INDIA

    location_text = _prepare(" ".join(candidate.locations_raw))
    narrative = _prepare(
        " ".join(
            part
            for part in (location_text, candidate.title_original, candidate.description_text or "")
            if part
        )
    )
    patterns = _patterns()
    excluded = _search(patterns["exclusions"], narrative)
    india_city = _search(patterns["cities"], location_text)
    india_token = _search(patterns["india_word"], location_text)
    india_state = _search(patterns["states"], location_text)
    foreign = code not in {None, "IN"} or _search(patterns["foreign"], location_text)

    if india_city or (india_token and not excluded):
        return IndiaRelevance.INDIA
    if india_state and not foreign and not excluded:
        return IndiaRelevance.INDIA
    if not excluded and _search(patterns["remote_india"], narrative):
        return IndiaRelevance.REMOTE_INDIA
    if excluded or foreign or _search(patterns["us_only"], narrative):
        return IndiaRelevance.NOT_INDIA
    if _search(patterns["remote"], location_text) or _search(patterns["remote"], narrative):
        return IndiaRelevance.UNKNOWN_REMOTE
    return IndiaRelevance.UNKNOWN


def _prepare(value: str) -> str:
    text = value.lower()
    aliases = _aliases()

    def replace(match: re.Match[str]) -> str:
        return aliases[match.group(0)]

    return _alias_pattern().sub(replace, text)


def _normalize_country_code(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    code = value.strip().upper()
    code = {"IND": "IN", "USA": "US", "GBR": "GB", "UK": "GB"}.get(code, code)
    if code == "IN":
        return "IN"
    if re.fullmatch(r"[A-Z]{2}", code):
        return code
    return None


def _search(pattern: re.Pattern[str], text: str) -> bool:
    return pattern.search(text) is not None


@lru_cache(maxsize=1)
def _aliases() -> dict[str, str]:
    raw = json.loads((_DATA / "location_aliases.json").read_text())
    return {key.lower(): value.lower() for key, value in raw.items()}


@lru_cache(maxsize=1)
def _alias_pattern() -> re.Pattern[str]:
    keys = sorted(_aliases(), key=len, reverse=True)
    return re.compile(r"\b(?:" + "|".join(re.escape(key) for key in keys) + r")\b")


@lru_cache(maxsize=1)
def _patterns() -> dict[str, re.Pattern[str]]:
    locations = json.loads((_DATA / "india_locations.json").read_text())
    return {
        "cities": _phrase_pattern(locations["cities"]),
        "states": _phrase_pattern(locations["states"]),
        "india_word": re.compile(r"\bindia\b"),
        "exclusions": _phrase_pattern(_INDIA_EXCLUSIONS),
        "remote_india": _phrase_pattern(_REMOTE_INDIA_PHRASES),
        "us_only": _phrase_pattern(_US_ONLY_PHRASES),
        "remote": _phrase_pattern(_REMOTE_SIGNALS),
        "foreign": _phrase_pattern((*_FOREIGN_COUNTRIES, *_FOREIGN_CITIES, *_US_STATE_NAMES)),
    }


def _phrase_pattern(phrases: tuple[str, ...] | list[str]) -> re.Pattern[str]:
    ordered = sorted({phrase.lower() for phrase in phrases}, key=len, reverse=True)
    body = "|".join(re.escape(phrase) for phrase in ordered)
    return re.compile(rf"\b(?:{body})\b")
