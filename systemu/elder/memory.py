"""Elder global memory — parse, score, and render ELDER_MEMORY.md for prompt injection.

ELDER_MEMORY.md is injected into EVERY shadow's boot context, above the per-shadow
SHADOW_MEMORY block, so all shadows benefit from cross-task personalisation the Elder
has accumulated. This module is read-only at execution time — the Elder writes to it
after Wild Card reflection.

Sections:
  User Preferences   — date formats, naming conventions, output locations
  Workflow Patterns  — recurring multi-step sequences observed across tasks
  Tool Affinities    — preferred library/tool choices
  Recurring Variables — common paths, filenames, API endpoints
  Personalisation Notes — free-form learnings about the user's working style
"""

from __future__ import annotations

import re
from typing import Dict, List

from systemu.core.utils import utcnow

ELDER_MEMORY_SECTIONS = [
    "User Preferences",
    "Workflow Patterns",
    "Tool Affinities",
    "Recurring Variables",
    "Personalisation Notes",
]

_MAX_ENTRIES_PER_SECTION = 10


def parse_elder_memory(md_text: str) -> Dict[str, List[str]]:
    """Split ELDER_MEMORY.md into {section: [bullet, ...]} dict.

    Placeholder lines (starting with underscore/italic) are filtered out.
    """
    sections: Dict[str, List[str]] = {h: [] for h in ELDER_MEMORY_SECTIONS}
    if not md_text:
        return sections

    current = ""
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
            current = heading if heading in ELDER_MEMORY_SECTIONS else ""
        else:
            buffer.append(raw)
    _flush()
    return sections


def build_elder_memory_block(md_text: str) -> str:
    """Return a compact prompt block for injection into shadow boot context.

    All sections with content are included (unlike per-shadow memory which
    scores by relevance — Elder memory is small enough to inject in full).
    Returns empty string if ELDER_MEMORY.md has no real content yet.
    """
    if not md_text:
        return ""

    sections = parse_elder_memory(md_text)
    has_content = any(entries for entries in sections.values())
    if not has_content:
        return ""

    lines = ["## Elder Memory (global personalisation)\n"]
    for section, entries in sections.items():
        if entries:
            lines.append(f"**{section}**")
            for entry in entries[:_MAX_ENTRIES_PER_SECTION]:
                lines.append(f"- {entry}")
            lines.append("")

    return "\n".join(lines).strip()


def render_elder_memory_md(sections: Dict[str, List[str]], buffer_pending: int = 0) -> str:
    """Reconstruct a full ELDER_MEMORY.md file from parsed sections dict.

    Called by the consolidation step after merging buffer entries into the sections.
    """
    from datetime import datetime
    entry_count = sum(len(v) for v in sections.values())
    lines = [
        "---",
        f"last_consolidated: {utcnow().isoformat()}",
        f"entry_count: {entry_count}",
        f"buffer_pending: {buffer_pending}",
        "---",
        "",
        "# Elder Memory — Global Personalisation",
        "",
    ]
    for section in ELDER_MEMORY_SECTIONS:
        lines.append(f"## {section}")
        lines.append("")
        entries = sections.get(section, [])
        if entries:
            for entry in entries:
                lines.append(f"- {entry}")
        else:
            lines.append("_No entries yet._")
        lines.append("")

    return "\n".join(lines)
