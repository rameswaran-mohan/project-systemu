"""Mem0 backend wraps the mem0ai Python client.

Mem0 is a third-party long-term memory layer used widely by agent
frameworks.  Adding it as a swap-in lets operators who already invested
in Mem0 reuse it as Systemu's per-shadow memory store.
"""
from __future__ import annotations
from .base import BaseMemoryBackend

# Lazy/optional import: pull Memory into the module namespace so tests can
# monkeypatch ``systemu.runtime.memory_backends.mem0.Memory``.  When the
# extras aren't installed we expose ``Memory = None`` so attribute access
# works and the real error surfaces only when someone tries to instantiate.
try:
    from mem0 import Memory  # type: ignore
except ImportError:
    Memory = None  # type: ignore


class Mem0MemoryBackend(BaseMemoryBackend):
    """Each Systemu shadow maps to a Mem0 user (``systemu_<shadow_id>``)."""

    def __init__(self):
        if Memory is None:
            raise ImportError(
                "Mem0 backend requested but 'mem0ai' is not installed. "
                "Install with: pip install 'systemu[mem0]'"
            )
        self._mem0 = Memory()

    def _user_id(self, shadow_id: str) -> str:
        return f"systemu_{shadow_id}"

    def load_buffer(self, shadow_id: str) -> list:
        entries = self._mem0.get_all(user_id=self._user_id(shadow_id))
        out = []
        for e in (entries or []):
            metadata = e.get("metadata") or {}
            entry = {"lesson": e.get("memory", ""), **metadata}
            # Ensure 'category' is present even when metadata didn't carry one
            entry.setdefault("category", metadata.get("category", ""))
            out.append(entry)
        return out

    def append_buffer(self, shadow_id: str, entry: dict) -> None:
        lesson_text = entry.get("lesson", "")
        metadata = {k: v for k, v in entry.items() if k != "lesson"}
        self._mem0.add(
            lesson_text,
            user_id=self._user_id(shadow_id),
            metadata=metadata,
        )

    def load_consolidated(self, shadow_id: str) -> str:
        """Mem0 doesn't have a "consolidated MD" concept — return a
        bullet list of lessons.  Sufficient for the LLM-view path; the
        save_consolidated() no-op below means Mem0 keeps managing its
        own consolidation internally."""
        entries = self.load_buffer(shadow_id)
        if not entries:
            return ""
        return "\n".join(f"- {e['lesson']}" for e in entries)

    def save_consolidated(self, shadow_id: str, md: str) -> None:
        # No-op for Mem0 — it manages its own consolidation.
        pass
