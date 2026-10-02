"""Start a crawl from the demo page, or add one verified public board.

A new board stays disabled when the live check fails. Fetching candidates and
scraping both run off the request thread so the page can refresh.
"""

import asyncio
import re
import threading
import uuid
from dataclasses import dataclass
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.base import ConnectorError
from app.connectors.registry import build_connector
from app.db import SessionLocal
from app.discovery.ats_detector import detect
from app.models.source import Source
from app.pipeline.india_filter import classify_india
from app.scheduler.tasks import fetch_source, run_source_crawl
from scripts.seed_sources import apply_seed, fetch_seed, rejection_reason

# Boards already checked in public. A click rechecks the ones not saved yet.
CANDIDATES = [
    {"source_type": "greenhouse", "external_key": "smartsheet", "company_name": "Smartsheet"},
    {"source_type": "greenhouse", "external_key": "turing", "company_name": "Turing"},
    {"source_type": "greenhouse", "external_key": "poppulo", "company_name": "Poppulo"},
    {"source_type": "greenhouse", "external_key": "commerceiq", "company_name": "CommerceIQ"},
    {"source_type": "greenhouse", "external_key": "thoughtworks", "company_name": "Thoughtworks"},
    {"source_type": "greenhouse", "external_key": "superapp", "company_name": "SuperApp"},
    {"source_type": "greenhouse", "external_key": "diligentcorporation", "company_name": "Diligent Corporation"},
    {"source_type": "ashby", "external_key": "avoca", "company_name": "Avoca"},
    {"source_type": "ashby", "external_key": "span", "company_name": "SPAN"},
    {"source_type": "ashby", "external_key": "genera", "company_name": "Genera"},
    {"source_type": "workable", "external_key": "xenon7", "company_name": "Xenon7"},
]

_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,80}$")
_TYPES = {"greenhouse", "lever", "ashby", "workable"}
_PUBLISHED = {"india", "remote_india"}
_lock = threading.Lock()
_running = False
_fetching = False
_fetched = False
_message = ""
_ready: list["ReadySource"] = []


@dataclass(frozen=True)
class ReadySource:
    source_type: str
    external_key: str
    company_name: str
    base_url: str
    job_count: int
    india_count: int

    @property
    def token(self) -> str:
        return f"{self.source_type}|{self.external_key}"


def demo_status() -> tuple[bool, str, list[ReadySource], bool]:
    with _lock:
        return _running or _fetching, _message, list(_ready), _fetched


def scrape_status() -> tuple[bool, str]:
    busy, message, _ready_now, _fetched_now = demo_status()
    return busy, message


def start_source_fetch() -> str:
    global _fetching, _message
    with _lock:
        if _running or _fetching:
            return "A fetch or scrape is already running."
        _fetching = True
        _message = "Checking public boards."
    threading.Thread(target=_fetch_worker, daemon=True).start()
    return _message


def start_seed(tokens: list[str]) -> str:
    global _running, _message, _ready
    chosen_tokens = [token for token in tokens if token]
    if not chosen_tokens:
        return "Select at least one source."
    with _lock:
        if _running or _fetching:
            return "A fetch or scrape is already running."
        chosen = [item for item in _ready if item.token in chosen_tokens]
        if not chosen:
            return "Those sources are no longer ready. Fetch sources again."
        chosen_ids = {item.token for item in chosen}
        _ready = [item for item in _ready if item.token not in chosen_ids]
        _running = True
        _message = f"Seeding {len(chosen)} source{'s' if len(chosen) != 1 else ''}."
    threading.Thread(target=_seed_worker, args=(chosen,), daemon=True).start()
    return _message


def probe_candidates(seeds: list[dict[str, str]]) -> list[ReadySource]:
    if not seeds:
        return []
    found = asyncio.run(_probe_all(seeds))
    return sorted(found, key=lambda item: (-item.india_count, item.company_name.lower()))


def start_scrape(source_ids: list[uuid.UUID]) -> str:
    global _running, _message
    if not source_ids:
        return "No sources to scrape."
    message = f"Scraping {len(source_ids)} source{'s' if len(source_ids) != 1 else ''}."
    with _lock:
        if _running or _fetching:
            return "A fetch or scrape is already running."
        _running = True
        _message = message
    threading.Thread(target=_scrape, args=(list(source_ids),), daemon=True).start()
    return message


def _scrape(source_ids: list[uuid.UUID]) -> None:
    global _running, _message
    lines: list[str] = []
    for source_id in source_ids:
        session = SessionLocal()
        try:
            source = session.get(Source, source_id)
            if source is None:
                lines.append("missing source")
                continue
            run = run_source_crawl(session, source, fetch_source)
            session.commit()
            error = f" {run.error_text}" if run.error_text else ""
            lines.append(
                f"{source.external_key}: {run.status} fetched={run.fetched_count} "
                f"rejected={run.rejected_count}{error}"
            )
        except Exception as exc:
            session.rollback()
            lines.append(f"stopped: {exc}")
        finally:
            session.close()
    with _lock:
        _running = False
        _message = " ".join(lines)[:1200]


def _fetch_worker() -> None:
    global _fetching, _fetched, _message, _ready
    session = SessionLocal()
    try:
        existing = {(row.source_type, row.external_key) for row in session.scalars(select(Source))}
    finally:
        session.close()
    pending = [
        _candidate_seed(item)
        for item in CANDIDATES
        if (item["source_type"], item["external_key"]) not in existing
    ]
    try:
        ready = probe_candidates(pending)
        message = (
            f"{len(ready)} boards are ready to seed."
            if ready
            else "No new boards passed the live check."
        )
    except Exception as exc:
        ready = []
        message = f"Fetch failed: {exc}"
    with _lock:
        _fetching = False
        _fetched = True
        _ready = ready
        _message = message


def _seed_worker(chosen: list[ReadySource]) -> None:
    global _running, _message
    lines: list[str] = []
    for item in chosen:
        session = SessionLocal()
        try:
            lines.append(add_board(session, item.external_key, item.source_type, item.company_name))
        except Exception as exc:
            session.rollback()
            lines.append(f"{item.external_key}: {exc}")
        finally:
            session.close()
    with _lock:
        _running = False
        _message = " ".join(lines)[:1200]


async def _probe_all(seeds: list[dict[str, str]]) -> list[ReadySource]:
    semaphore = asyncio.Semaphore(5)

    async def one(seed: dict[str, str]) -> ReadySource | None:
        async with semaphore:
            try:
                envelopes = await fetch_seed(seed)
            except ConnectorError:
                return None
            if rejection_reason(seed, envelopes) is not None:
                return None
            return ReadySource(
                source_type=seed["source_type"],
                external_key=seed["external_key"],
                company_name=seed["company_name"],
                base_url=seed["base_url"],
                job_count=len(envelopes),
                india_count=_india_count(seed, envelopes),
            )

    found = await asyncio.gather(*(one(seed) for seed in seeds))
    return [item for item in found if item is not None]


def _india_count(seed: dict[str, str], envelopes: list) -> int:
    connector = build_connector(seed["source_type"], company_name=seed["company_name"])
    count = 0
    for envelope in envelopes:
        try:
            candidate = connector.normalize(envelope)
        except Exception:
            continue
        if classify_india(candidate).value in _PUBLISHED:
            count += 1
    return count


def _candidate_seed(item: dict[str, str]) -> dict[str, str]:
    company = item["company_name"]
    kind = item["source_type"]
    key = item["external_key"]
    return {
        "source_type": kind,
        "external_key": key,
        "company_name": company,
        "name": company,
        "base_url": _base_url(kind, key),
    }


def add_board(session: Session, board: str, source_type: str, company_name: str) -> str:
    try:
        kind, key = _parse_board(board, source_type)
    except ValueError as exc:
        return str(exc)
    company = " ".join(company_name.split())
    if not company or len(company) > 200:
        return "Enter the company name as it appears on the board."

    existing = session.scalar(
        select(Source).where(Source.source_type == kind, Source.external_key == key)
    )
    if existing is not None:
        return f"{existing.company_name or existing.name} is already a source. Use Scrape to fetch it again."

    seed = {
        "source_type": kind,
        "external_key": key,
        "company_name": company,
        "name": company,
        "base_url": _base_url(kind, key),
    }
    try:
        envelopes = asyncio.run(fetch_seed(seed))
    except ConnectorError as exc:
        apply_seed(seed, None, str(exc))
        return f"Saved {key} and left it disabled: {exc}"
    apply_seed(seed, envelopes, None)

    source = session.scalar(
        select(Source).where(Source.source_type == kind, Source.external_key == key)
    )
    if source is None:
        return "The board could not be saved."
    session.refresh(source)
    if not source.enabled:
        reason = (source.source_metadata or {}).get("disabled_reason") or "the live check failed"
        return f"Saved {key} and left it disabled: {reason}"

    run = run_source_crawl(session, source, fetch_source)
    session.commit()
    return (
        f"Added {company}. Crawl {run.status}, fetched {run.fetched_count}, "
        f"rejected {run.rejected_count}."
    )


def _parse_board(board: str, source_type: str) -> tuple[str, str]:
    text = board.strip()
    if not text:
        raise ValueError("Enter a board key or a board URL.")
    if text.startswith(("http://", "https://")):
        parsed = urlparse(text)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Enter an http or https board URL.")
        found = detect(text, "")
        if found.status != "detected" or found.source_type not in _TYPES or not found.external_key:
            raise ValueError("Use a Greenhouse, Lever, Ashby, or Workable board URL.")
        return found.source_type, found.external_key
    kind = source_type.strip().lower()
    if kind not in _TYPES:
        raise ValueError("Choose a board type, or paste the board URL.")
    if _SLUG.match(text) is None:
        raise ValueError("The board key has unsupported characters.")
    return kind, text


def _base_url(source_type: str, key: str) -> str:
    if source_type == "greenhouse":
        return f"https://job-boards.greenhouse.io/{key}"
    if source_type == "lever":
        return f"https://jobs.lever.co/{key}"
    if source_type == "ashby":
        return f"https://jobs.ashbyhq.com/{key}"
    return f"https://apply.workable.com/{key}"
