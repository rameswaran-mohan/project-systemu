"""Pure navigation helpers for the v0.8.8 console revamp.

Kept dependency-free (no NiceGUI imports) so the console page and the list
pages can all import them without circular-import risk, and so they're
trivially unit-testable.

Phase 6 Slice 6f: the Workshop deep-link helpers (``workshop_deeplink`` /
``resolve_deeplink_tab`` / ``_DEEPLINK_TAB``) were removed with the /workshop
route — the Scrolls rebuild (Workshop's last surface) is now an in-place dialog
(``scroll_rebuild.open_scroll_rebuild_dialog``).
"""
from __future__ import annotations

from typing import Optional

# Tile label → list-page route
_TILE_NAV = {
    "Scrolls":     "/scrolls",
    "Shadows":     "/shadows",
    "Tools":       "/tools",
    "Skills":      "/skills",
    "Activities":  "/activities",
    "Evolutions":  "/evolutions",
}

def tile_nav_target(label: str) -> Optional[str]:
    """Return the list-page route for a Console stat-tile label, or None."""
    return _TILE_NAV.get(label)
