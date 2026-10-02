import hashlib
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.ai_cache import AiCache

_WHITESPACE = re.compile(r"\s+")


def normalized_input_text(title: str, location: str, description: str) -> str:
    combined = "\n".join((title, location, description))
    return _WHITESPACE.sub(" ", combined).strip().lower()


def cache_key(task_type: str, prompt_version: str, normalized_text: str) -> str:
    raw = f"{task_type}|{prompt_version}|{normalized_text}"
    return hashlib.sha256(raw.encode()).hexdigest()


def get_cached(session: Session, key: str) -> dict | None:
    row = session.scalar(select(AiCache).where(AiCache.cache_key == key))
    if row is None:
        return None
    return row.response_json
