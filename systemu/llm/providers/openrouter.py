"""v0.7-e: OpenRouter provider — catch-all for non-claimed models."""
from __future__ import annotations
from openai import AsyncOpenAI

from .base import BaseLLMProvider, LLMResponse

_TIMEOUT = 120


class OpenRouterProvider(BaseLLMProvider):
    """Catch-all provider that ships requests through OpenRouter.

    Matches everything except models other providers explicitly claim
    (Google catches ``gemini*``/``google/*``; Anthropic catches ``claude*``
    / ``anthropic/*``; OpenAI catches ``gpt-*`` / ``openai/*`` / ``o1-*`` /
    ``o3-*``; Ollama catches ``ollama/*``).
    """

    def __init__(self, api_key: str, base_url: str = "https://openrouter.ai/api/v1"):
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=_TIMEOUT)

    @classmethod
    def matches(cls, model_name: str) -> bool:
        m = model_name.lower()
        if m.startswith(("gemini", "google/")):
            return False
        if m.startswith(("claude-", "anthropic/")):
            return False
        if m.startswith(("gpt-", "openai/", "o1-", "o3-")):
            return False
        if m.startswith("ollama/"):
            return False
        return True

    async def call(self, *, messages, model, **kwargs) -> LLMResponse:
        resp = await self._client.chat.completions.create(
            model=model, messages=messages, **kwargs,
        )
        usage = {}
        if getattr(resp, "usage", None):
            usage = {
                "input": resp.usage.prompt_tokens or 0,
                "output": resp.usage.completion_tokens or 0,
            }
        return LLMResponse(
            content=resp.choices[0].message.content or "",
            model=resp.model,
            usage=usage,
            raw=resp,
        )
