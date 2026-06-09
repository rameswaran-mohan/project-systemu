"""Concept -> Material Symbol (bundled with NiceGUI/Quasar). One icon per concept;
no shared/emoji glyphs. Pages call ``icon("approve")`` and pass it to ui.icon /
button(icon=...) — never an emoji literal."""
from __future__ import annotations

ICONS = {
    "approve": "check_circle",
    "reject": "cancel",
    "inspect": "visibility",
    "home": "home",
    "work": "list_alt",
    "shadow": "smart_toy",
    "build": "construction",
    "insights": "monitoring",
    "settings": "settings",
    "inbox": "inbox",
    "record": "fiber_manual_record",
    "task": "bolt",
    "memory": "database",
    "tool": "build",
    "skill": "auto_awesome",
    "evolution": "trending_up",
    "recover": "healing",
    "danger": "warning",
    "success": "check",
    "running": "play_circle",
}


def icon(concept: str) -> str:
    """Return the Material Symbol name for a concept, or a safe 'help' default."""
    return ICONS.get(concept, "help")
