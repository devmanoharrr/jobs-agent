from openai import AsyncOpenAI

from app.config import settings
from app.schemas.structured_job import MODEL_JSON_SCHEMA

REQUEST_TIMEOUT_SECONDS = 60


class OpenAIProvider:
    """GPT chat completions that must match the job-facts schema."""

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self._model = model or settings.openai_model
        self._api_key = api_key if api_key is not None else settings.openai_api_key
        self.model = self._model

    async def complete_json(self, *, system: str, user: str) -> str:
        client = AsyncOpenAI(api_key=self._api_key, timeout=REQUEST_TIMEOUT_SECONDS)
        try:
            response = await client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format={"type": "json_schema", "json_schema": MODEL_JSON_SCHEMA},
                max_completion_tokens=4000,
            )
        finally:
            await client.close()
        content = response.choices[0].message.content
        if not isinstance(content, str) or not content.strip():
            raise ValueError("OpenAI returned an empty completion")
        return content
