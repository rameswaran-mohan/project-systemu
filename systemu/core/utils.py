"""Shared utilities used across all Systemu modules."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from pathlib import Path


def utcnow() -> datetime:
    """Drop-in replacement for the deprecated ``datetime.datetime.utcnow()``.

    Returns a **naive** UTC datetime — same shape as the legacy
    ``datetime.utcnow()`` so it slots into Pydantic models, ISO strings
    without ``+00:00`` suffixes, and JSON serialisers that strip tzinfo.

    Python 3.12 deprecated ``datetime.utcnow()`` because the naive return
    value looked like local time but actually held UTC — a footgun.  Our
    codebase has always treated these as UTC explicitly, so re-deriving via
    ``datetime.now(timezone.utc).replace(tzinfo=None)`` preserves the legacy
    semantics without the deprecation warning.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def generate_id(prefix: str) -> str:
    """Generate a unique entity ID: <prefix>_<8-char-hex>.

    Example:
        generate_id("scroll")  →  "scroll_a1b2c3d4"
    """
    return f"{prefix}_{secrets.token_hex(4)}"


def load_prompt(name: str) -> str:
    """Load a prompt template from systemu/prompts/<name>.

    Searches relative to the systemu package root so this works regardless
    of the current working directory.
    """
    prompts_dir = Path(__file__).resolve().parent.parent / "prompts"
    path = prompts_dir / name
    if not path.exists():
        raise FileNotFoundError(f"Prompt template not found: {path}")
    return path.read_text(encoding="utf-8")
