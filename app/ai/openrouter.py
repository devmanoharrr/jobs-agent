from openai import AsyncOpenAI

from app.config import settings

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
REQUEST_TIMEOUT_SECONDS = 30


class OpenRouterProvider:
    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self._model = model or settings.openrouter_model
        self._api_key = api_key if api_key is not None else settings.openrouter_api_key

    async def complete_json(self, *, system: str, user: str) -> str:
        client = AsyncOpenAI(
            base_url=OPENROUTER_BASE_URL,
            api_key=self._api_key,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        try:
            response = await client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format={"type": "json_object"},
                temperature=0,
            )
        finally:
            await client.close()
        content = response.choices[0].message.content
        if not isinstance(content, str) or not content.strip():
            raise ValueError("OpenRouter returned an empty completion")
        return content
