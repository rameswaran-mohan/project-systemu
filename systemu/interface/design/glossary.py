"""W10.4 — ONE plain-language glossary for the lore (charter v2 req 3).

The lore (Scrolls, Shadows, Forge…) is part of the product's character, but
a professional from any background must understand the screen at a glance.
Pages render these sublabels beside their lore titles; the mapping lives in
exactly one place so the translation never drifts per page.
"""
from __future__ import annotations

_GLOSSARY = {
    "scrolls":    "Workflows — captured task definitions you can run again",
    "activities": "Tasks — the executable steps extracted from a workflow",
    "shadows":    "Agents — the personas that run your workflows",
    "forge":      "Build a tool — create a new capability",
    "evolutions": "Improvements — proposed upgrades the system has learned",
}


def lore_sublabel(term: str) -> str:
    """Plain-language sublabel for a lore term; "" for unknown terms."""
    return _GLOSSARY.get((term or "").strip().lower(), "")
