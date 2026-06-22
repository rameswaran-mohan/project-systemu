"""v0.7-e: Ollama provider (local REST, no SDK dep — uses httpx)."""
from __future__ import annotations
import httpx
from .base import BaseLLMProvider, LLMResponse

_TIMEOUT = 120


class OllamaProvider(BaseLLMProvider):
    def __init__(self, base_url: str = "http://localhost:11434"):
        self._base_url = base_url.rstrip("/")

    @classmethod
    def matches(cls, model_name: str) -> bool:
        return model_name.lower().startswith("ollama/")

    async def call(self, *, messages, model, **kwargs) -> LLMResponse:
        # Strip the ``ollama/`` prefix when calling the local API
        actual_model = model.split("/", 1)[1] if "/" in model else model
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{self._base_url}/api/chat",
                json={
                    "model": actual_model,
                    "messages": messages,
                    "stream": False,
                },
            )
            resp.raise_for_status()
            data = resp.json()
        return LLMResponse(
            content=data.get("message", {}).get("content", ""),
            model=data.get("model", actual_model),
            usage={
                "input": data.get("prompt_eval_count", 0),
                "output": data.get("eval_count", 0),
            },
            raw=data,
        )
