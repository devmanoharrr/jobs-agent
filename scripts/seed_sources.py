"""Seed sources that have been manually verified as public boards.

A seed stays disabled when the live check returns 404, malformed JSON, an
unexpected Greenhouse or Workable company name, or a Lever or Ashby posting
whose public URL is not on that site.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

from app.config import settings
from app.connectors.base import ConnectorError
from app.connectors.registry import build_connector
from app.db import SessionLocal
from app.models.source import Source
from app.pipeline.raw_store import normalize_raw, persist_raw_if_changed
from app.schemas.raw_job import RawJobEnvelope

SEEDS = [
    {
        "source_type": "greenhouse",
        "external_key": "groww",
        "company_name": "Groww",
        "name": "Groww",
        "base_url": "https://job-boards.eu.greenhouse.io/groww",
    },
    {
        "source_type": "lever",
        "external_key": "cred",
        "company_name": "CRED",
        "name": "CRED",
        "base_url": "https://jobs.lever.co/cred",
    },
    {
        "source_type": "ashby",
        "external_key": "temporal",
        "company_name": "Temporal",
        "name": "Temporal",
        "base_url": "https://jobs.ashbyhq.com/temporal",
    },
    {
        "source_type": "workable",
        "external_key": "epignosis",
        "company_name": "Epignosis",
        "name": "Epignosis",
        "base_url": "https://apply.workable.com/epignosis",
    },
    {
        "source_type": "greenhouse",
        "external_key": "cloudsek",
        "company_name": "CloudSEK",
        "name": "CloudSEK",
        "base_url": "https://job-boards.greenhouse.io/cloudsek",
    },
    {
        "source_type": "greenhouse",
        "external_key": "prodigal",
        "company_name": "Prodigal",
        "name": "Prodigal",
        "base_url": "https://job-boards.greenhouse.io/prodigal",
    },
    {
        "source_type": "greenhouse",
        "external_key": "druva",
        "company_name": "Druva",
        "name": "Druva",
        "base_url": "https://job-boards.greenhouse.io/druva",
    },
    {
        "source_type": "lever",
        "external_key": "meesho",
        "company_name": "Meesho",
        "name": "Meesho",
        "base_url": "https://jobs.lever.co/meesho",
    },
    {
        "source_type": "lever",
        "external_key": "nium",
        "company_name": "Nium",
        "name": "Nium",
        "base_url": "https://jobs.lever.co/nium",
    },
    {
        "source_type": "ashby",
        "external_key": "atlan",
        "company_name": "Atlan",
        "name": "Atlan",
        "base_url": "https://jobs.ashbyhq.com/atlan",
    },
    {
        "source_type": "ashby",
        "external_key": "gainsight",
        "company_name": "Gainsight",
        "name": "Gainsight",
        "base_url": "https://jobs.ashbyhq.com/gainsight",
    },
    {
        "source_type": "ashby",
        "external_key": "level-ai",
        "company_name": "Level AI",
        "name": "Level AI",
        "base_url": "https://jobs.ashbyhq.com/level-ai",
    },
    {
        "source_type": "workable",
        "external_key": "apna",
        "company_name": "Apna",
        "name": "Apna",
        "base_url": "https://apply.workable.com/apna",
    },
    {
        "source_type": "workable",
        "external_key": "blue-machines-ai",
        "company_name": "Blue Machines AI",
        "name": "Blue Machines AI",
        "base_url": "https://apply.workable.com/blue-machines-ai",
    },
    {
        "source_type": "workable",
        "external_key": "leucine",
        "company_name": "Leucine",
        "name": "Leucine",
        "base_url": "https://apply.workable.com/leucine",
    },
    {
        "source_type": "workable",
        "external_key": "virallens",
        "company_name": "Virallens",
        "name": "Virallens",
        "base_url": "https://apply.workable.com/virallens",
    },
    {
        "source_type": "workable",
        "external_key": "ai-accountant",
        "company_name": "AI Accountant",
        "name": "AI Accountant",
        "base_url": "https://apply.workable.com/ai-accountant",
    },
]


async def fetch_seed(seed: dict[str, str]) -> list[RawJobEnvelope]:
    source = Source(
        name=seed["name"],
        source_type=seed["source_type"],
        external_key=seed["external_key"],
        company_name=seed["company_name"],
        base_url=seed["base_url"],
        enabled=False,
    )
    timeout = httpx.Timeout(settings.request_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout) as client:
        connector = build_connector(seed["source_type"], client, seed["company_name"])
        return await connector.fetch(source)


def apply_seed(seed: dict[str, str], envelopes: list[RawJobEnvelope] | None, error: str | None) -> None:
    now = datetime.now(timezone.utc)
    session = SessionLocal()
    try:
        source = session.scalar(
            select(Source).where(
                Source.source_type == seed["source_type"],
                Source.external_key == seed["external_key"],
            )
        )
        if source is None:
            source = Source(
                name=seed["name"],
                source_type=seed["source_type"],
                external_key=seed["external_key"],
                company_name=seed["company_name"],
                base_url=seed["base_url"],
                enabled=False,
            )
            session.add(source)
            session.flush()

        source.name = seed["name"]
        source.company_name = seed["company_name"]
        source.base_url = seed["base_url"]
        source.last_crawled_at = now

        if error is not None or envelopes is None:
            source.enabled = False
            source.consecutive_failures += 1
            source.source_metadata = {"disabled_reason": error or "fetch failed"}
            session.commit()
            print(f"disabled {seed['external_key']}: {error}")
            return

        connector = build_connector(seed["source_type"], company_name=seed["company_name"])
        normalized = 0
        for envelope in envelopes:
            row = persist_raw_if_changed(session, source, envelope)
            if normalize_raw(connector, envelope, row) is not None:
                normalized += 1

        reason = rejection_reason(seed, envelopes)
        if reason is not None:
            source.enabled = False
            source.consecutive_failures += 1
            source.source_metadata = {"disabled_reason": reason, "job_count": len(envelopes)}
            session.commit()
            print(f"disabled {seed['external_key']}: {reason}")
            return

        source.enabled = True
        source.consecutive_failures = 0
        source.last_success_at = now
        source.next_crawl_at = now + timedelta(minutes=source.crawl_interval_minutes)
        source.source_metadata = {"verified": True, "job_count": len(envelopes)}
        session.commit()
        print(
            f"enabled {seed['external_key']}: fetched={len(envelopes)} "
            f"normalized={normalized} source_id={source.id}"
        )
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def rejection_reason(seed: dict[str, str], envelopes: list[RawJobEnvelope]) -> str | None:
    if not envelopes:
        return "no jobs returned"
    if seed["source_type"] == "greenhouse":
        companies = {str(item.payload.get("company_name") or "") for item in envelopes}
        expected = seed["company_name"]
        if companies != {expected}:
            return f"unexpected company name: {sorted(companies)}"
        return None
    if seed["source_type"] == "lever":
        prefix = f"https://jobs.lever.co/{seed['external_key']}/"
        unexpected = [item.source_url for item in envelopes if not (item.source_url or "").startswith(prefix)]
        if unexpected:
            return f"unexpected hostedUrl: {unexpected[:3]}"
        return None
    if seed["source_type"] == "ashby":
        prefix = f"https://jobs.ashbyhq.com/{seed['external_key']}/"
        unexpected = [item.source_url for item in envelopes if not (item.source_url or "").startswith(prefix)]
        if unexpected:
            return f"unexpected jobUrl: {unexpected[:3]}"
        return None
    if seed["source_type"] == "workable":
        unexpected = [
            item.source_url
            for item in envelopes
            if not (item.source_url or "").startswith("https://apply.workable.com/")
        ]
        if unexpected:
            return f"unexpected job url: {unexpected[:3]}"
        return None
    return None


def main() -> None:
    for seed in SEEDS:
        try:
            envelopes = asyncio.run(fetch_seed(seed))
        except ConnectorError as exc:
            apply_seed(seed, None, str(exc))
            continue
        apply_seed(seed, envelopes, None)


if __name__ == "__main__":
    main()
