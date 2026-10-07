"""Send published jobs to one Jobi target.

Companies go first. Jobs whose company Jobi skips stay waiting.
A run with active jobs reconciles that set. Deactivate is used only when
no published jobs remain, and only for keys this target already accepted.
"""

from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone

import httpx
from sqlalchemy import delete, func, select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from app.config import settings
from app.models.job import Job
from app.models.jobi_sync import JobiSync
from app.pipeline.structure import scrub_payload

TARGETS = ("local", "prod")
BASE_URLS = {
    "local": "http://localhost:7400",
    "prod": "https://www.jobi.ai",
}
_PUBLISHED = ("india", "remote_india")
_BATCH = 40
_TIMEOUT = 60.0
_lock = threading.Lock()
_posting = False


class JobiSyncError(Exception):
    """The post stopped before Jobi accepted the rest of the run."""


def base_url_for(target: str) -> str:
    url = BASE_URLS.get(target)
    if url is None:
        raise JobiSyncError("Choose Jobi local or prod.")
    return url


def waiting_count(session: Session, target: str) -> int:
    if target not in TARGETS:
        return 0
    try:
        synced = _synced_keys(session, target)
        return sum(1 for job in _postable(session) if job.exact_fingerprint not in synced)
    except ProgrammingError:
        session.rollback()
        return 0


def unstructured_count(session: Session) -> int:
    try:
        total = session.scalar(
            select(func.count())
            .select_from(Job)
            .where(
                Job.status == "active",
                Job.india_relevance.in_(_PUBLISHED),
                Job.structure_status != "ready",
            )
        )
    except ProgrammingError:
        session.rollback()
        return 0
    return int(total or 0)


def sync_table_ready(session: Session) -> bool:
    try:
        session.scalar(select(JobiSync.id).limit(1))
    except ProgrammingError:
        session.rollback()
        return False
    return True


def transfer(session: Session, *, target: str, publish: bool) -> str:
    """Post waiting companies and jobs, then reconcile. Returns a short notice."""
    global _posting
    url = base_url_for(target)
    token = settings.jobs_agent_token.strip()
    if not token:
        raise JobiSyncError("Set JOBS_AGENT_TOKEN before posting to Jobi.")
    with _lock:
        if _posting:
            raise JobiSyncError("A Jobi post is already running.")
        _posting = True
    try:
        try:
            return _transfer(session, target=target, base_url=url, token=token, publish=publish)
        except ProgrammingError as exc:
            session.rollback()
            raise JobiSyncError("Run alembic upgrade head before posting to Jobi.") from exc
    finally:
        with _lock:
            _posting = False


def _transfer(session: Session, *, target: str, base_url: str, token: str, publish: bool) -> str:
    postable = _postable(session)
    active_keys = sorted({job.exact_fingerprint for job in postable})
    synced = _synced_keys(session, target)
    waiting = [job for job in postable if job.exact_fingerprint not in synced]
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    company_counts = _empty_counts()
    job_counts = _empty_counts()
    deactivated = 0
    if not waiting and not active_keys and not synced:
        return _notice(target, base_url, company_counts, job_counts, deactivated, posted=False, publish=publish)
    try:
        with httpx.Client(timeout=_TIMEOUT, headers=headers, follow_redirects=False) as client:
            if waiting:
                company_counts = _post_companies(client, base_url, waiting, publish)
                job_counts = _post_jobs(client, session, target, base_url, waiting)
            if active_keys:
                deactivated = _reconcile(client, base_url, active_keys)
            elif synced:
                deactivated = _deactivate(client, base_url, sorted(synced))
    except httpx.HTTPError as exc:
        raise JobiSyncError(f"Could not reach Jobi: {exc}") from exc
    _forget_stale(session, target, active_keys)
    session.commit()
    return _notice(
        target,
        base_url,
        company_counts,
        job_counts,
        deactivated,
        posted=bool(waiting),
        publish=publish,
    )


def _post_companies(
    client: httpx.Client,
    base_url: str,
    waiting: Sequence[Job],
    publish: bool,
) -> dict[str, int]:
    counts = _empty_counts()
    payloads = _company_payloads(waiting, publish)
    for chunk in _chunks(payloads, _BATCH):
        results = _results(client, f"{base_url}/internal/companies", {"companies": chunk}, chunk)
        _add_counts(counts, results)
    return counts


def _post_jobs(
    client: httpx.Client,
    session: Session,
    target: str,
    base_url: str,
    waiting: Sequence[Job],
) -> dict[str, int]:
    counts = _empty_counts()
    now = datetime.now(timezone.utc)
    chosen = _deduped(waiting)
    payloads = [_job_payload(job) for job in chosen]
    by_key = {job.exact_fingerprint: job for job in chosen}
    for chunk in _chunks(payloads, _BATCH):
        results = _results(client, f"{base_url}/internal/job-postings", {"jobs": chunk}, chunk)
        _add_counts(counts, results)
        accepted = []
        for row, result in zip(chunk, results, strict=True):
            if result.get("action") in {"created", "updated"}:
                accepted.append(row["external_key"])
            job = by_key.get(row.get("external_key"))
            if job is not None:
                _record_sent(session, job, target, row, result, now)
        _remember(session, target, accepted, now)
        session.commit()
    return counts


def _reconcile(client: httpx.Client, base_url: str, active_keys: list[str]) -> int:
    data = _request(client, f"{base_url}/internal/job-postings/reconcile", {"active_external_keys": active_keys})
    return _deactivated(data)


def _deactivate(client: httpx.Client, base_url: str, keys: list[str]) -> int:
    data = _request(client, f"{base_url}/internal/job-postings/deactivate", {"external_keys": keys})
    return _deactivated(data)


def _deactivated(data: dict) -> int:
    value = data.get("deactivated", 0)
    return int(value) if isinstance(value, int) else 0


def _results(client: httpx.Client, url: str, payload: dict, sent: Sequence[dict]) -> list[dict]:
    data = _request(client, url, payload)
    results = data.get("results")
    if not isinstance(results, list) or len(results) != len(sent):
        raise JobiSyncError("Jobi returned an unexpected result list.")
    if not all(isinstance(item, dict) for item in results):
        raise JobiSyncError("Jobi returned an unexpected result list.")
    return results


def _request(client: httpx.Client, url: str, payload: dict) -> dict:
    response = client.post(url, json=payload)
    if response.status_code == 401:
        raise JobiSyncError("Jobi rejected the token.")
    if response.status_code == 422:
        raise JobiSyncError(f"Jobi rejected the request: {_error_text(response)}")
    if response.status_code != 200:
        raise JobiSyncError(f"Jobi returned {response.status_code}.")
    try:
        data = response.json()
    except ValueError as exc:
        raise JobiSyncError("Jobi returned an unexpected response.") from exc
    if not isinstance(data, dict):
        raise JobiSyncError("Jobi returned an unexpected response.")
    return data


def _postable(session: Session) -> list[Job]:
    rows = session.scalars(
        select(Job)
        .where(Job.status == "active", Job.india_relevance.in_(_PUBLISHED))
        .order_by(Job.company_normalized.asc(), Job.posted_at.desc().nulls_last(), Job.id.asc())
    ).all()
    return [job for job in rows if _can_send(job)]


def _can_send(job: Job) -> bool:
    if job.structure_status != "ready" or not isinstance(job.structured_payload, dict):
        return False
    payload = job.structured_payload
    title = str(payload.get("title") or "").strip()
    company = str(payload.get("company_external_key") or payload.get("company_name") or "").strip()
    fingerprint = str(payload.get("external_key") or "").strip()
    url = str(payload.get("apply_url") or "").strip()
    if not title or not company or not fingerprint:
        return False
    if fingerprint != job.exact_fingerprint or len(fingerprint) > 255 or len(url) > 500:
        return False
    return url.startswith(("http://", "https://"))


def _company_payloads(waiting: Sequence[Job], publish: bool) -> list[dict]:
    grouped: dict[str, list[Job]] = {}
    for job in waiting:
        grouped.setdefault(job.company_normalized, []).append(job)
    payloads = []
    for key in sorted(grouped):
        jobs = grouped[key]
        names = Counter(job.company_name.strip() for job in jobs)
        name = sorted(names.items(), key=lambda item: (-item[1], item[0].casefold()))[0][0]
        location = next((_location(job) for job in jobs if _location(job)), None)
        payloads.append(
            {
                "external_key": key,
                "name": name,
                "website_url": None,
                "linkedin_url": None,
                "industry": None,
                "company_type": None,
                "company_size_label": None,
                "primary_location": location,
                "short_description": None,
                "description": None,
                "logo_url": None,
                "publish": publish,
            }
        )
    return payloads


def _job_payload(job: Job) -> dict:
    payload = job.structured_payload if isinstance(job.structured_payload, dict) else {}
    return scrub_payload(dict(payload))


def _record_sent(
    session: Session,
    job: Job,
    target: str,
    payload: dict,
    result: dict,
    now: datetime,
) -> None:
    if job.id is None:
        return
    stored = session.get(Job, job.id)
    if stored is None:
        return
    trace = [dict(item) for item in (stored.pipeline_trace or []) if isinstance(item, dict)]
    trace.append(
        {
            "step": "sent",
            "at": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "target": target,
            "request": payload,
            "result": result,
        }
    )
    stored.pipeline_trace = trace


def _deduped(waiting: Sequence[Job]) -> list[Job]:
    seen: set[str] = set()
    chosen: list[Job] = []
    for job in waiting:
        if job.exact_fingerprint in seen:
            continue
        seen.add(job.exact_fingerprint)
        chosen.append(job)
    return chosen


def _location(job: Job) -> str | None:
    parts = [part.strip() for part in (job.city, job.state, job.country_code) if part and part.strip()]
    if not parts:
        return None
    return ", ".join(parts)[:180]


def _synced_keys(session: Session, target: str) -> set[str]:
    return set(session.scalars(select(JobiSync.external_key).where(JobiSync.target == target)).all())


def _remember(session: Session, target: str, keys: Iterable[str], now: datetime) -> None:
    existing = _synced_keys(session, target)
    for key in keys:
        if key in existing:
            continue
        existing.add(key)
        session.add(JobiSync(target=target, external_key=key, synced_at=now))


def _forget_stale(session: Session, target: str, active_keys: list[str]) -> None:
    statement = delete(JobiSync).where(JobiSync.target == target)
    if active_keys:
        statement = statement.where(JobiSync.external_key.not_in(active_keys))
    session.execute(statement)


def _empty_counts() -> dict[str, int]:
    return {"created": 0, "updated": 0, "skipped": 0}


def _add_counts(counts: dict[str, int], results: list[dict]) -> None:
    for result in results:
        action = result.get("action")
        if action in counts:
            counts[action] += 1
        else:
            counts["skipped"] += 1
        reason = result.get("reason")
        if action == "skipped" and isinstance(reason, str) and reason:
            counts[reason] = counts.get(reason, 0) + 1


def _notice(
    target: str,
    base_url: str,
    companies: dict[str, int],
    jobs: dict[str, int],
    deactivated: int,
    *,
    posted: bool,
    publish: bool,
) -> str:
    label = "local" if target == "local" else "prod"
    if not posted:
        if deactivated:
            return f"Nothing new to post. Jobi {label} deactivated {deactivated} postings that are no longer active here."
        return f"Nothing new to post to Jobi {label}."
    company_reasons = _reasons(companies)
    job_reasons = _reasons(jobs)
    text = (
        f"Posted to Jobi {label} at {base_url}. "
        f"Companies created {companies['created']}, updated {companies['updated']}, skipped {companies['skipped']}"
        f"{company_reasons}. "
        f"Jobs created {jobs['created']}, updated {jobs['updated']}, skipped {jobs['skipped']}"
        f"{job_reasons}. "
        f"Deactivated {deactivated}."
    )
    if not publish:
        text += " New companies stay unpublished on Jobi until you publish them."
    return text[:1200]


def _reasons(counts: dict[str, int]) -> str:
    parts = [f"{name} {count}" for name, count in sorted(counts.items()) if name not in {"created", "updated", "skipped"}]
    if not parts:
        return ""
    return " (" + ", ".join(parts) + ")"


def _error_text(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return "the body was rejected"
    if isinstance(data, dict) and isinstance(data.get("error"), str):
        return data["error"]
    return "the body was rejected"


def _chunks(items: Sequence[dict], size: int) -> Iterable[Sequence[dict]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]
