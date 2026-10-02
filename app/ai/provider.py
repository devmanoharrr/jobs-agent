from typing import Protocol


class AIProvider(Protocol):
    async def complete_json(self, *, system: str, user: str) -> str: ...
