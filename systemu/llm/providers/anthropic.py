"""v0.7-e: native Anthropic provider with OpenAI-message bridge.

The Anthropic Messages API splits ``system`` off from the messages list
and requires ``max_tokens``.  This provider accepts standard OpenAI-shape
messages and bridges them at the boundary so the rest of Systemu can
keep using one message format."""
from __future__ import annotations
from typing import Any
from .base import BaseLLMProvider, LLMResponse

_TIMEOUT = 120.0
_DEFAULT_MAX_TOKENS = 4096


class AnthropicProvider(BaseLLMProvider):
    def __init__(self, api_key: str):
        from anthropic import AsyncAnthropic  # local import — extras gate
        self._client = AsyncAnthropic(api_key=api_key, timeout=_TIMEOUT)

    @classmethod
    def matches(cls, model_name: str) -> bool:
        m = model_name.lower()
        return m.startswith(("claude-", "anthropic/"))

    async def call(self, *, messages, model, **kwargs) -> LLMResponse:
        # OpenAI message shape: [{role: system|user|assistant, content: str}]
        # Anthropic shape:      system=<str>, messages=[{role: user|assistant, content: str}]
        system = ""
        bridged = []
        for m in messages:
            if m.get("role") == "system":
                # Concatenate multiple system messages with newlines
                system = (system + "\n" + (m.get("content") or "")).strip()
            else:
                bridged.append(m)

        anthropic_kwargs: dict[str, Any] = {
            "max_tokens": kwargs.get("max_tokens", _DEFAULT_MAX_TOKENS),
        }
        if system:
            anthropic_kwargs["system"] = system

        msg = await self._client.messages.create(
            model=model,
            messages=bridged,
            **anthropic_kwargs,
        )

        # Anthropic response.content is a list of content blocks; concatenate text
        text = "".join(getattr(c, "text", "") for c in msg.content)
        usage = {}
        if getattr(msg, "usage", None):
            usage = {
                "input": msg.usage.input_tokens,
                "output": msg.usage.output_tokens,
            }
        return LLMResponse(content=text, model=msg.model, usage=usage, raw=msg)
