"""Per-shadow memory parser — used by the memory consolidator.

The retrieval logic (lexical scoring, top-K selection) has been removed.
Boot-time memory injection now reads GLOBAL_MEMORY in full and injects a
one-line SHADOW_MEMORY header; the shadow calls LOAD_RESOURCE on demand.

This module is kept for:
  - parse_memory_md : used by memory_consolidator and refinery to inspect sections
  - entry_confidence / entry_text : metadata helpers for the consolidator
"""

from __future__ import annotations

import logging
import re
from typing import Dict, List

logger = logging.getLogger(__name__)

# Section headings as written by consolidate_memory.md
_SECTION_HEADERS = [
    "Self-Assessment",
    "Heuristics",
    "Failure Patterns",
    "Tool Quirks",
    "Domain Glossary",
]


def parse_memory_md(md_text: str) -> Dict[str, List[str]]:
    """Split SHADOW_MEMORY.md into a {section_name: [bullet, ...]} dict.

    Bullets are returned as their raw text (with leading metadata like
    `[conf:5, last:..., evidence: ...]` preserved). Entries that look like
    placeholder text (italic, starts with underscore) are filtered out.
    """
    sections: Dict[str, List[str]] = {h: [] for h in _SECTION_HEADERS}
    if not md_text:
        return sections

    current: str = ""
    buffer: List[str] = []

    def _flush() -> None:
        if not current:
            return
        for line in buffer:
            line = line.strip()
            if not line or line.startswith("_") or line.startswith("<"):
                continue
            if line.startswith("- "):
                sections[current].append(line[2:].strip())
            elif sections[current] and not line.startswith("#"):
                sections[current][-1] = sections[current][-1] + " " + line

    for raw in md_text.splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", raw)
        if m:
            _flush()
            buffer = []
            heading = m.group(1).strip()
            current = heading if heading in _SECTION_HEADERS else ""
        else:
            buffer.append(raw)
    _flush()

    return sections


_META_RE = re.compile(r"^\[conf:(\d+)[^\]]*\]\s*", re.IGNORECASE)


def entry_confidence(bullet: str) -> int:
    """Pull the confidence integer out of a bullet's metadata prefix.

    Bullets without a `[conf:N, ...]` prefix default to 1 — treat as fresh.
    """
    m = _META_RE.match(bullet)
    return int(m.group(1)) if m else 1


def entry_text(bullet: str) -> str:
    """Strip the leading `[conf:..., last:..., evidence:...]` metadata."""
    return _META_RE.sub("", bullet).strip()
