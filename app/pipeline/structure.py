"""Turn one published posting into the JSON Jobi accepts.

The board supplies identity, location, dates, and salary figures.
GPT splits the prose into the card summary and the lists.
Every step is appended to the posting trail.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import re
from datetime import datetime, timezone
from decimal import Decimal

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.ai.budget import try_spend
from app.ai.cache import cache_key, get_cached
from app.ai.provider import AIProvider
from app.config import settings
from app.models.ai_cache import AiCache
from app.models.job import Job
from app.models.raw_job import RawJob
from app.models.source import Source
from app.schemas.structured_job import EMPLOYMENT_TYPES, WORK_MODES, ModelJobFacts
from app.utils.text import html_to_text

STRUCTURE_VERSION = "v1"
TASK_TYPE = "job_structure"
_DESCRIPTION_LIMIT = 14000

_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)

_EMPLOYMENT = {
    "fulltime": "full_time",
    "permanent": "full_time",
    "parttime": "part_time",
    "contract": "contract",
    "contractor": "contract",
    "freelance": "contract",
    "intern": "internship",
    "internship": "internship",
    "temporary": "temporary",
    "temp": "temporary",
}

_STATE_CODES = {
    "ap": "Andhra Pradesh",
    "ar": "Arunachal Pradesh",
    "as": "Assam",
    "br": "Bihar",
    "cg": "Chhattisgarh",
    "ga": "Goa",
    "gj": "Gujarat",
    "hr": "Haryana",
    "hp": "Himachal Pradesh",
    "jh": "Jharkhand",
    "jk": "Jammu and Kashmir",
    "ka": "Karnataka",
    "kl": "Kerala",
    "la": "Ladakh",
    "mh": "Maharashtra",
    "ml": "Meghalaya",
    "mn": "Manipur",
    "mp": "Madhya Pradesh",
    "mz": "Mizoram",
    "nl": "Nagaland",
    "od": "Odisha",
    "or": "Odisha",
    "pb": "Punjab",
    "py": "Puducherry",
    "rj": "Rajasthan",
    "sk": "Sikkim",
    "tg": "Telangana",
    "tn": "Tamil Nadu",
    "tr": "Tripura",
    "ts": "Telangana",
    "uk": "Uttarakhand",
    "up": "Uttar Pradesh",
    "wb": "West Bengal",
    "dl": "Delhi",
    "ch": "Chandigarh",
}

_CITY_STATES = {
    "ahmedabad": "Gujarat",
    "bangalore": "Karnataka",
    "bengaluru": "Karnataka",
    "bhopal": "Madhya Pradesh",
    "bhubaneswar": "Odisha",
    "chandigarh": "Chandigarh",
    "chennai": "Tamil Nadu",
    "coimbatore": "Tamil Nadu",
    "delhi": "Delhi",
    "faridabad": "Haryana",
    "ghaziabad": "Uttar Pradesh",
    "goa": "Goa",
    "gurugram": "Haryana",
    "gurgaon": "Haryana",
    "hyderabad": "Telangana",
    "indore": "Madhya Pradesh",
    "jaipur": "Rajasthan",
    "kochi": "Kerala",
    "kolkata": "West Bengal",
    "lucknow": "Uttar Pradesh",
    "mohali": "Punjab",
    "mumbai": "Maharashtra",
    "mysore": "Karnataka",
    "mysuru": "Karnataka",
    "nagpur": "Maharashtra",
    "nashik": "Maharashtra",
    "new delhi": "Delhi",
    "noida": "Uttar Pradesh",
    "pune": "Maharashtra",
    "surat": "Gujarat",
    "thiruvananthapuram": "Kerala",
    "trivandrum": "Kerala",
    "vadodara": "Gujarat",
    "visakhapatnam": "Andhra Pradesh",
}

_LPA_RANGE = re.compile(
    r"(?i)(?:₹|rs\.?|inr)?\s*(\d+(?:\.\d+)?)\s*(?:-|–|to)\s*(?:₹|rs\.?|inr)?\s*(\d+(?:\.\d+)?)\s*"
    r"(?:lpa|lacs?|lakhs?)(?:\s*(?:per|/)\s*(?:annum|year|pa|p\.a\.))?"
)
_LPA_SINGLE = re.compile(
    r"(?i)(?:₹|rs\.?|inr)?\s*(\d+(?:\.\d+)?)\s*(?:lpa|lacs?|lakhs?)"
    r"(?:\s*(?:per|/)\s*(?:annum|year|pa|p\.a\.))?"
)
_RUPEE_RANGE = re.compile(
    r"(?i)(?:₹|rs\.?|inr)\s*([\d,]{4,})\s*(?:-|–|to)\s*(?:₹|rs\.?|inr)?\s*([\d,]{4,})"
    r"(?:\s*(?:per|/)\s*(month|year|annum))?"
)
_RUPEE_SINGLE = re.compile(
    r"(?i)(?:₹|rs\.?|inr)\s*([\d,]{4,})(?:\s*(?:per|/)\s*(month|year|annum))?"
)

SYSTEM_PROMPT = (
    "You split one job posting into JSON fields.\n"
    "Use only facts written in the posting. Do not invent skills, years, or locations.\n"
    "Return plain strings and arrays. Do not return HTML.\n"
    "summary is two or three sentences for a card.\n"
    "description_text is leftover prose that could not be split into the lists. "
    "Use null when the lists cover the posting.\n"
    "skills, responsibilities, requirements, nice_to_have, and benefits are arrays of plain strings. "
    "Use [] when the posting has none.\n"
    "work_mode is remote, hybrid, onsite, or null.\n"
    "employment_type is full_time, part_time, contract, internship, temporary, or null.\n"
    "experience_min_years is a number or null.\n"
    "experience_label is a short phrase copied from the posting, such as \"5+ years\", or null."
)


def plain_text(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = html.unescape(value)
    text = _TAG.sub(" ", text)
    text = _SPACE.sub(" ", text).strip()
    return text or None


def normalize_employment(value: str | None) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    if value in EMPLOYMENT_TYPES:
        return value
    token = re.sub(r"[^a-z]", "", value.lower())
    return _EMPLOYMENT.get(token)


def infer_work_mode(current: str | None, text: str, *, city: str | None, relevance: str) -> str | None:
    if current in WORK_MODES:
        return current
    lowered = text.lower()
    if "hybrid" in lowered:
        return "hybrid"
    if "remote" in lowered or "work from home" in lowered or relevance == "remote_india":
        return "remote"
    if any(phrase in lowered for phrase in ("on-site", "onsite", "on site", "in-office", "in office")):
        return "onsite"
    if city and relevance == "india":
        return "onsite"
    return None


def fill_state(city: str | None, state: str | None) -> str | None:
    if isinstance(state, str) and state.strip():
        code = state.strip()
        if len(code) <= 3 and code.lower() in _STATE_CODES:
            return _STATE_CODES[code.lower()]
        return code
    if not isinstance(city, str) or not city.strip():
        return None
    return _CITY_STATES.get(city.strip().lower())


def parse_salary(text: str) -> dict | None:
    """Read an explicit INR amount. Competitive pay and bare years stay empty."""
    if not text:
        return None
    range_lpa = _LPA_RANGE.search(text)
    if range_lpa:
        low = _lakhs(range_lpa.group(1))
        high = _lakhs(range_lpa.group(2))
        return _salary(min(low, high), max(low, high), "year")
    single_lpa = _LPA_SINGLE.search(text)
    if single_lpa:
        return _salary(_lakhs(single_lpa.group(1)), None, "year")
    range_rupee = _RUPEE_RANGE.search(text)
    if range_rupee:
        low = _rupees(range_rupee.group(1))
        high = _rupees(range_rupee.group(2))
        period = _period(range_rupee.group(3))
        return _salary(min(low, high), max(low, high), period)
    single = _RUPEE_SINGLE.search(text)
    if single:
        amount = _rupees(single.group(1))
        return _salary(amount, None, _period(single.group(2)))
    return None


def scrub_payload(payload: dict) -> dict:
    """Drop HTML from the object that will be posted."""
    cleaned: dict = {}
    for key, value in payload.items():
        if isinstance(value, str):
            text = plain_text(value)
            cleaned[key] = text if text else None
        elif isinstance(value, list):
            items = []
            for item in value:
                if not isinstance(item, str):
                    continue
                text = plain_text(item)
                if text:
                    items.append(text)
            cleaned[key] = items
        else:
            cleaned[key] = value
    if cleaned.get("description_text") is None:
        cleaned["description_text"] = None
    return cleaned


def structure_posting(
    session: Session,
    job: Job,
    raw_row: RawJob,
    source: Source,
    *,
    checked_at: datetime,
    provider: AIProvider | None = None,
    api_key: str | None = None,
    model: str | None = None,
    budget: int | None = None,
) -> str:
    """Fill structured_payload and pipeline_trace. Returns pending, ready, or failed."""
    description = _posting_text(job)
    digest = _hash(job, description)
    if job.structure_status == "ready" and job.structure_hash == digest and isinstance(job.structured_payload, dict):
        return "ready"

    key = settings.openai_api_key if api_key is None else api_key
    key = key.strip()
    model_name = model or settings.openai_model
    limit = settings.openai_daily_budget if budget is None else budget
    now = checked_at if checked_at.tzinfo else checked_at.replace(tzinfo=timezone.utc)

    steps = [_fetched(raw_row, source), _normalized(job, description, now)]
    sent = [item for item in (job.pipeline_trace or []) if isinstance(item, dict) and item.get("step") == "sent"]

    if not (job.canonical_apply_url or "").startswith(("http://", "https://")):
        _finish(job, steps, sent, "failed", "This posting has no apply link, so it was not structured.", None, None)
        session.flush()
        return "failed"

    if not key:
        _finish(job, steps, sent, "pending", "OPENAI_API_KEY is empty, so this posting was not sent to GPT.", None, None)
        session.flush()
        return "pending"

    if provider is None:
        from app.ai.openai_provider import OpenAIProvider

        provider = OpenAIProvider(api_key=key, model=model_name)

    source_text = f"{job.title_original}\n{description}".strip()
    facts, model_step, failed = _model_facts(
        session,
        provider,
        job,
        description,
        model_name=model_name,
        budget=limit,
        now=now,
    )
    steps.append(model_step)
    if failed or facts is None:
        note = failed or "The model response was not usable."
        status = "pending" if note == "Daily OpenAI budget is spent." else "failed"
        _finish(job, steps, sent, status, note, None, None)
        session.flush()
        return status

    grounded, drops = _ground(facts, source_text)
    mode = infer_work_mode(job.work_mode, source_text, city=job.city, relevance=job.india_relevance)
    if mode is None and grounded.work_mode in WORK_MODES and _mode_is_written(grounded.work_mode, description):
        mode = grounded.work_mode
    elif grounded.work_mode and grounded.work_mode != mode:
        drops.append(f"Ignored model work_mode {grounded.work_mode}.")

    employment = normalize_employment(job.employment_type)
    if (
        employment is None
        and grounded.employment_type in EMPLOYMENT_TYPES
        and _employment_is_written(grounded.employment_type, source_text)
    ):
        employment = grounded.employment_type
    elif grounded.employment_type and grounded.employment_type != employment:
        drops.append(f"Ignored model employment_type {grounded.employment_type}.")

    state = fill_state(job.city, job.state)
    salary = parse_salary(source_text)
    lists = {
        "responsibilities": _lines(grounded.responsibilities),
        "requirements": _lines(grounded.requirements),
        "nice_to_have": _lines(grounded.nice_to_have),
        "benefits": _lines(grounded.benefits),
    }
    leftover = _leftover(grounded.description_text, lists)
    payload = build_sendable(
        external_key=job.exact_fingerprint,
        company_external_key=job.company_normalized,
        company_name=job.company_name.strip(),
        title=job.title_original.strip(),
        apply_url=job.canonical_apply_url.strip(),
        summary=plain_text(grounded.summary),
        description_text=leftover,
        role_category=_short(grounded.role_category, 60),
        skills=_lines(grounded.skills),
        city=plain_text(job.city),
        state=state,
        work_mode=mode,
        employment_type=employment,
        experience_min_years=_years(grounded.experience_min_years),
        experience_label=_short(grounded.experience_label, 40),
        posted_at=_iso(job.posted_at),
        responsibilities=lists["responsibilities"],
        requirements=lists["requirements"],
        nice_to_have=lists["nice_to_have"],
        benefits=lists["benefits"],
        salary=salary,
    )
    _apply_structured(job, payload, salary, digest)
    steps.append({"step": "checked", "at": _iso(now), "status": "ready", "dropped": drops, "payload": payload})
    steps.append({"step": "saved", "at": _iso(now), "job_id": str(job.id), "structure_status": "ready"})
    job.pipeline_trace = steps + sent
    session.flush()
    return "ready"


def build_sendable(
    *,
    external_key: str,
    company_external_key: str,
    company_name: str,
    title: str,
    apply_url: str,
    summary: str | None,
    description_text: str | None,
    role_category: str | None,
    skills: list[str],
    city: str | None,
    state: str | None,
    work_mode: str | None,
    employment_type: str | None,
    experience_min_years: int | float | None,
    experience_label: str | None,
    posted_at: str | None,
    responsibilities: list[str],
    requirements: list[str],
    nice_to_have: list[str],
    benefits: list[str],
    salary: dict | None,
) -> dict:
    payload: dict = {
        "external_key": external_key,
        "company_external_key": company_external_key,
        "company_name": company_name,
        "title": title,
        "summary": summary,
        "description_text": description_text,
        "role_category": role_category,
        "skills": skills,
        "country_code": "IN",
        "experience_min_years": experience_min_years,
        "experience_label": experience_label,
        "apply_url": apply_url,
        "responsibilities": responsibilities,
        "requirements": requirements,
        "nice_to_have": nice_to_have,
        "benefits": benefits,
    }
    if city:
        payload["city"] = city
    if state:
        payload["state"] = state
    if work_mode in WORK_MODES:
        payload["work_mode"] = work_mode
    if employment_type in EMPLOYMENT_TYPES:
        payload["employment_type"] = employment_type
    if posted_at:
        payload["posted_at"] = posted_at
    if salary:
        payload.update(salary)
    return scrub_payload(payload)


def _model_facts(
    session: Session,
    provider: AIProvider,
    job: Job,
    description: str,
    *,
    model_name: str,
    budget: int,
    now: datetime,
) -> tuple[ModelJobFacts | None, dict, str | None]:
    user = _user_prompt(job, description)
    text_key = cache_key(TASK_TYPE, STRUCTURE_VERSION, f"{job.title_original}\n{description}".lower())
    cached = get_cached(session, text_key)
    if isinstance(cached, dict):
        facts = _facts_from_dict(cached)
        step = {
            "step": "model",
            "at": _iso(now),
            "model": model_name,
            "cached": True,
            "prompt": user,
            "response": cached,
            "error": None if facts is not None else "Cached model JSON was not usable.",
        }
        if facts is not None:
            return facts, step, None
        return None, step, "Cached model JSON was not usable."

    response_text = None
    error = None
    for _attempt in range(2):
        if not try_spend(session, now.date(), budget):
            error = "Daily OpenAI budget is spent."
            break
        try:
            response_text = asyncio.run(provider.complete_json(system=SYSTEM_PROMPT, user=user))
        except Exception as exc:
            error = str(exc)
            response_text = None
            continue
        facts = _facts_from_text(response_text)
        if facts is not None:
            session.add(
                AiCache(
                    cache_key=text_key,
                    task_type=TASK_TYPE,
                    model=model_name,
                    prompt_version=STRUCTURE_VERSION,
                    response_json=facts.model_dump(mode="json"),
                    created_at=now,
                )
            )
            step = {
                "step": "model",
                "at": _iso(now),
                "model": model_name,
                "cached": False,
                "prompt": user,
                "response": response_text,
                "error": None,
            }
            return facts, step, None
        error = "The model response was not usable JSON."

    step = {
        "step": "model",
        "at": _iso(now),
        "model": model_name,
        "cached": False,
        "prompt": user,
        "response": response_text,
        "error": error,
    }
    return None, step, error


def _ground(facts: ModelJobFacts, source: str) -> tuple[ModelJobFacts, list[str]]:
    drops: list[str] = []
    skills = []
    for skill in facts.skills:
        text = plain_text(skill)
        if not text:
            continue
        if _mentioned(text, source):
            skills.append(text)
        else:
            drops.append(f"Dropped skill {text!r} because it is not in the posting.")
    years = facts.experience_min_years
    label = plain_text(facts.experience_label)
    if years is not None and not _number_in_text(years, source):
        drops.append(f"Dropped experience {years} because that number is not in the posting.")
        years = None
        label = None
    elif label and not _label_is_grounded(label, source):
        drops.append(f"Dropped experience label {label!r} because it is not in the posting.")
        label = None
    data = facts.model_dump()
    data["skills"] = skills[:20]
    data["experience_min_years"] = years
    data["experience_label"] = label
    return ModelJobFacts.model_validate(data), drops


def _finish(
    job: Job,
    steps: list[dict],
    sent: list[dict],
    status: str,
    note: str,
    payload: dict | None,
    digest: str | None,
) -> None:
    now = datetime.now(timezone.utc)
    steps.append({"step": "checked", "at": _iso(now), "status": status, "dropped": [], "note": note, "payload": payload})
    steps.append({"step": "saved", "at": _iso(now), "job_id": str(job.id), "structure_status": status})
    job.structure_status = status
    job.structure_hash = digest
    job.structured_payload = payload
    job.pipeline_trace = steps + sent


def _apply_structured(job: Job, payload: dict, salary: dict | None, digest: str) -> None:
    job.summary = payload.get("summary")
    job.role_category = payload.get("role_category")
    job.skills = list(payload.get("skills") or [])
    job.experience_label = payload.get("experience_label")
    years = payload.get("experience_min_years")
    job.experience_min = None if years is None else Decimal(str(years))
    job.responsibilities = list(payload.get("responsibilities") or [])
    job.requirements = list(payload.get("requirements") or [])
    job.nice_to_have = list(payload.get("nice_to_have") or [])
    job.benefits = list(payload.get("benefits") or [])
    if payload.get("work_mode"):
        job.work_mode = payload["work_mode"]
    if payload.get("employment_type"):
        job.employment_type = payload["employment_type"]
    if payload.get("state"):
        job.state = payload["state"]
    if not job.country_code:
        job.country_code = "IN"
    if salary:
        job.salary_min = Decimal(str(salary["salary_min"]))
        job.salary_max = None if salary.get("salary_max") is None else Decimal(str(salary["salary_max"]))
        job.salary_currency = "INR"
        job.salary_period = salary["salary_period"]
    else:
        job.salary_min = None
        job.salary_max = None
        job.salary_currency = None
        job.salary_period = None
    job.structured_payload = payload
    job.structure_status = "ready"
    job.structure_hash = digest


def _facts_from_text(raw: str) -> ModelJobFacts | None:
    text = _FENCE.sub("", raw.strip()).strip()
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    return _facts_from_dict(data)


def _facts_from_dict(data: dict) -> ModelJobFacts | None:
    coerced = {
        "summary": _maybe_str(data.get("summary")),
        "role_category": _maybe_str(data.get("role_category")),
        "skills": _str_list(data.get("skills")),
        "work_mode": data.get("work_mode") if data.get("work_mode") in WORK_MODES else None,
        "employment_type": data.get("employment_type") if data.get("employment_type") in EMPLOYMENT_TYPES else None,
        "experience_min_years": _maybe_number(data.get("experience_min_years")),
        "experience_label": _maybe_str(data.get("experience_label")),
        "responsibilities": _str_list(data.get("responsibilities")),
        "requirements": _str_list(data.get("requirements")),
        "nice_to_have": _str_list(data.get("nice_to_have")),
        "benefits": _str_list(data.get("benefits")),
        "description_text": _maybe_str(data.get("description_text")),
    }
    try:
        return ModelJobFacts.model_validate(coerced)
    except ValidationError:
        return None


def _user_prompt(job: Job, description: str) -> str:
    shown = description
    if len(shown) > _DESCRIPTION_LIMIT:
        shown = shown[:_DESCRIPTION_LIMIT]
    location = ", ".join(part for part in (job.city, job.state, job.country_code) if part)
    return (
        f"TITLE: {job.title_original}\n"
        f"COMPANY: {job.company_name}\n"
        f"LOCATION: {location}\n"
        f"EMPLOYMENT: {job.employment_type or ''}\n"
        f"WORK MODE: {job.work_mode or ''}\n"
        "POSTING:\n"
        f"{shown}\n"
    )


def _posting_text(job: Job) -> str:
    text = plain_text(job.description_text)
    if text:
        return text
    return html_to_text(job.description_html) or ""


def _hash(job: Job, description: str) -> str:
    raw = "\n".join(
        (
            STRUCTURE_VERSION,
            job.title_original or "",
            description,
            job.city or "",
            job.canonical_apply_url or "",
        )
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _fetched(raw_row: RawJob, source: Source) -> dict:
    return {
        "step": "fetched",
        "at": _iso(raw_row.fetched_at),
        "source_type": source.source_type,
        "source_name": source.company_name or source.name,
        "source_url": raw_row.canonical_url,
        "source_job_id": raw_row.source_job_id,
        "payload": raw_row.payload,
    }


def _normalized(job: Job, description: str, now: datetime) -> dict:
    return {
        "step": "normalized",
        "at": _iso(now),
        "title": job.title_original,
        "company_name": job.company_name,
        "city": job.city,
        "state": job.state,
        "country_code": job.country_code,
        "employment_type": job.employment_type,
        "work_mode": job.work_mode,
        "apply_url": job.canonical_apply_url,
        "posted_at": _iso(job.posted_at),
        "description_text": description,
    }


def _leftover(text: str | None, lists: dict[str, list[str]]) -> str | None:
    cleaned = plain_text(text)
    if not cleaned:
        return None
    blob = " ".join(item for items in lists.values() for item in items)
    if not blob:
        return cleaned
    words = cleaned.casefold().split()
    if not words:
        return None
    covered = sum(1 for word in words if word in blob.casefold())
    if covered / len(words) >= 0.75:
        return None
    return cleaned


def _lines(items: list[str]) -> list[str]:
    lines = []
    for item in items:
        text = plain_text(item)
        if text:
            lines.append(text[:500])
    return lines[:20]


def _mentioned(skill: str, source: str) -> bool:
    needle = re.sub(r"[^a-z0-9]+", " ", skill.casefold()).strip()
    haystack = re.sub(r"[^a-z0-9]+", " ", source.casefold())
    return bool(needle) and f" {needle} " in f" {haystack} "


def _number_in_text(years: float, source: str) -> bool:
    if float(years).is_integer():
        token = str(int(years))
    else:
        token = str(years)
    return re.search(rf"(?<!\d){re.escape(token)}(?!\d)", source) is not None


def _label_is_grounded(label: str, source: str) -> bool:
    for number in re.findall(r"\d+(?:\.\d+)?", label):
        if not _number_in_text(float(number), source):
            return False
    return True


def _employment_is_written(employment: str, source: str) -> bool:
    words = {
        "full_time": ("full time", "full-time", "fulltime", "permanent"),
        "part_time": ("part time", "part-time", "parttime"),
        "contract": ("contract", "contractor", "freelance"),
        "internship": ("intern",),
        "temporary": ("temporary", "temp role", "temp position"),
    }
    lowered = source.lower()
    return any(word in lowered for word in words[employment])


def _mode_is_written(mode: str, source: str) -> bool:
    lowered = source.lower()
    if mode == "hybrid":
        return "hybrid" in lowered
    if mode == "remote":
        return "remote" in lowered or "work from home" in lowered
    return any(phrase in lowered for phrase in ("on-site", "onsite", "on site", "in-office", "in office"))


def _salary(low: int, high: int | None, period: str) -> dict:
    payload = {"salary_min": low, "salary_currency": "INR", "salary_period": period}
    if high is not None:
        payload["salary_max"] = high
    return payload


def _lakhs(token: str) -> int:
    return int(round(float(token) * 100000))


def _rupees(token: str) -> int:
    return int(token.replace(",", ""))


def _period(token: str | None) -> str:
    if token and token.lower().startswith("month"):
        return "month"
    return "year"


def _years(value: float | None) -> int | float | None:
    if value is None:
        return None
    if float(value).is_integer():
        return int(value)
    return float(value)


def _short(value: str | None, limit: int) -> str | None:
    text = plain_text(value)
    if not text:
        return None
    return text[:limit]


def _maybe_str(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return value


def _maybe_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)][:20]


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    current = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
