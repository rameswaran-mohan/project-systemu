"""Output capping + binary detection for tool result hygiene."""
from __future__ import annotations


def cap_output(text: str, *, max_chars: int) -> str:
    """Truncate ``text`` to ``max_chars`` with a marker appended on truncation.

    ``max_chars <= 0`` disables capping (returns the text unchanged).
    """
    if not max_chars or max_chars <= 0:
        return text
    text = text or ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n[... truncated at {max_chars} chars]"


def is_likely_binary(data: bytes) -> bool:
    """Heuristic: NUL bytes in the first kilobyte → binary."""
    if not data:
        return False
    sample = data[:1024]
    return b"\x00" in sample
