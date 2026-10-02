"""OpenAI-compatible asynchronous chat client (including OpenRouter)."""

from copy import deepcopy
import os

from openai import AsyncOpenAI

from .client import Backend


class OpenRouterClient(Backend):
    def __init__(self, settings: dict):
        self.model = os.path.expandvars(settings["model_id"])
        if not self.model or "$" in self.model:
            raise ValueError("Set the environment variable referenced by model_id")
        key_env = settings.get("api_key_env", "OPENROUTER_API_KEY")
        key = os.environ.get(key_env) if key_env else "EMPTY"
        if not key:
            raise ValueError(f"Missing API credential: {key_env}")
        self.sampling = deepcopy(settings.get("sampling", {}))
        if any(key in self.sampling for key in ("model", "messages", "stream")):
            raise ValueError("sampling cannot override model, messages, or stream")
        self.client = AsyncOpenAI(
            api_key=key,
            base_url=settings.get("base_url", "https://openrouter.ai/api/v1"),
            timeout=settings.get("timeout", 60.0),
            max_retries=settings.get("max_retries", 0),
        )

    async def generate(self, messages: list[dict]) -> str:
        result = await self.client.chat.completions.create(
            model=self.model, messages=messages, **self.sampling,
        )
        if not result.choices or not isinstance(result.choices[0].message.content, str):
            raise ValueError("Chat backend returned no text response")
        return result.choices[0].message.content

    async def close(self) -> None:
        await self.client.close()
