"""Pure navigation helpers for the v0.8.8 console revamp.

Kept dependency-free (no NiceGUI imports) so the console page, workshop page,
and the four list pages can all import them without circular-import risk, and
so they're trivially unit-testable.
"""
from __future__ import annotations

from typing import Optional

# Tile label → list-page route
_TILE_NAV = {
    "Scrolls":     "/scrolls",
    "Shadows":     "/army",
    "Tools":       "/tools",
    "Skills":      "/skills",
    "Activities":  "/activities",
    "Evolutions":  "/evolutions",
}

# Edit deeplink entity type → Workshop tab label
_DEEPLINK_TAB = {
    "scroll":  "Scrolls",
    "shadow":  "Shadows",
    "tool":    "Tools",
    "skill":   "Skills",
    "activity": "Activities",
}


def tile_nav_target(label: str) -> Optional[str]:
    """Return the list-page route for a Console stat-tile label, or None."""
    return _TILE_NAV.get(label)


def workshop_deeplink(entity_type: str, entity_id: str) -> str:
    """Build a Workshop deep-link URL for an Edit button."""
    return f"/workshop?type={entity_type}&id={entity_id}"


def resolve_deeplink_tab(deeplink_type: Optional[str]) -> str:
    """Map a deeplink entity type to a Workshop tab label.

    Unknown / None / empty → "Scrolls" (the Workshop default tab).
    """
    if not deeplink_type:
        return "Scrolls"
    return _DEEPLINK_TAB.get(deeplink_type, "Scrolls")
