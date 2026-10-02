import pytest

from app.config import settings


@pytest.fixture(autouse=True)
def _do_not_call_openrouter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "openrouter_api_key", "")
