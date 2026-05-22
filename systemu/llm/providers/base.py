"""provider plugin interface.

Every provider claims model names via ``matches(model_name)`` and implements
``async call(messages, model, **kwargs) -> LLMResponse``.  The registry uses
``matches`` to dispatch; env override (``SYSTEMU_TIER{1,2,3}_PROVIDER``) wins
over auto-detection (wired in E5).
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class LLMResponse:
    """Normalized LLM response.  Providers translate from native shape."""
    content: str
    model: str
    usage: dict = field(default_factory=dict)
    raw: Any = None  # provider-native response (escape hatch)


class BaseLLMProvider(ABC):
    """Concrete providers register themselves via the registry in __init__.py."""

    @classmethod
    @abstractmethod
    def matches(cls, model_name: str) -> bool:
        """Return True if this provider handles model_name (e.g. ``claude-*``)."""

    @abstractmethod
    async def call(
        self,
        *,
        messages: list[dict],
        model: str,
        **kwargs: Any,
    ) -> LLMResponse:
        """Execute the LLM call with OpenAI-style messages.  Implementations
        bridge to their native API and return a normalized ``LLMResponse``."""
