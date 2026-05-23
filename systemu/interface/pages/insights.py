"""Insights — tabbed parent page hosting Memory, Flywheel, and Events panels.

v0.7.2 sidebar consolidation: three small read-only analytics surfaces that
used to live as separate top-level routes (/memory, /flywheel,
/notifications) are now tabs inside one /insights destination.  The
underlying page builders are unchanged — this module only composes them.

Direct-link to a specific tab via ``/insights?tab=memory|flywheel|events``.
The legacy URLs (/memory, /flywheel, /notifications) are preserved as
redirect handlers in ``dashboard.py`` so bookmarks + notification deep
links continue to work.
"""

from __future__ import annotations

from nicegui import ui

from systemu.interface.dashboard_state import THEME
from systemu.interface.pages.flywheel_page import build_flywheel_page
from systemu.interface.pages.memory_consolidation_page import (
    build_memory_consolidation_page,
)
from systemu.interface.pages.notifications_page import build_notifications_page

_VALID_TABS = ("memory", "flywheel", "events")


def build_insights_page(default_tab: str = "memory") -> None:
    """Render the Insights page with three tabs.

    Args:
        default_tab: Which tab is active on load.  Falls back to ``memory``
                     when the query string supplies anything unrecognised.
    """
    if default_tab not in _VALID_TABS:
        default_tab = "memory"

    ui.label("Insights").style(
        f"font-size: 28px; font-weight: 800; color: {THEME['text']}; margin-bottom: 4px;"
    )
    ui.label(
        "Operational analytics — memory health, the data flywheel, and the live event log."
    ).style(
        f"color: {THEME['text_muted']}; font-size: 14px; margin-bottom: 20px;"
    )

    # ── Tabs header ─────────────────────────────────────────────────────────
    with ui.tabs().style(
        f"background: {THEME['surface']}; border-bottom: 1px solid {THEME['border']};"
    ) as tabs:
        ui.tab("memory", label="💡 Memory")
        ui.tab("flywheel", label="🔁 Flywheel")
        ui.tab("events", label="🔔 Events")

    # ── Tab panels ──────────────────────────────────────────────────────────
    # Each panel calls the existing page builder verbatim — no logic moves.
    with ui.tab_panels(tabs, value=default_tab).classes("w-full").style(
        "padding-top: 16px;"
    ):
        with ui.tab_panel("memory"):
            build_memory_consolidation_page()
        with ui.tab_panel("flywheel"):
            build_flywheel_page()
        with ui.tab_panel("events"):
            build_notifications_page()
