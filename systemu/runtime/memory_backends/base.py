"""v0.7-g: memory backend interface — abstracts the filesystem from the
consolidator path so Mem0 (or other backends) can plug in via env var."""
from __future__ import annotations
from abc import ABC, abstractmethod


class BaseMemoryBackend(ABC):
    """Single source of truth for memory I/O.

    Backends must be safe to instantiate at module import time (no
    network calls in __init__).  The four methods below are the entire
    contract for both the daily consolidator (pipelines/memory_consolidator)
    and the LLM-facing view filter (runtime/memory_consolidator).
    """

    @abstractmethod
    def load_buffer(self, shadow_id: str) -> list: ...

    @abstractmethod
    def append_buffer(self, shadow_id: str, entry: dict) -> None: ...

    @abstractmethod
    def load_consolidated(self, shadow_id: str) -> str: ...

    @abstractmethod
    def save_consolidated(self, shadow_id: str, md: str) -> None: ...
