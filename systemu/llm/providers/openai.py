"""v0.7-e: native OpenAI provider (api.openai.com, not via OpenRouter)."""
from __future__ import annotations
from openai import AsyncOpenAI

from .base import BaseLLMProvider, LLMResponse

_TIMEOUT = 120


class OpenAIProvider(BaseLLMProvider):
    def __init__(self, api_key: str):
        # Default base_url is api.openai.com when not specified.
        self._client = AsyncOpenAI(api_key=api_key, timeout=_TIMEOUT)

    @classmethod
    def matches(cls, model_name: str) -> bool:
        m = model_name.lower()
        return m.startswith(("gpt-", "openai/", "o1-", "o3-"))

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
