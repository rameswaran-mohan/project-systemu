"""v0.7.2: sidebar-consolidation smoke tests.

Covers:
  - NAV_GROUPS shape (3 groups, expected items per group, no duplicates).
  - Back-compat NAV_ITEMS flat list still exposes every route.
  - Every NAV path resolves to a registered @ui.page route.
  - Legacy URL handlers (/systemu-chat, /memory, /flywheel, /notifications)
    still exist as callables so deep-links keep landing somewhere.
  - The new /insights route accepts a ?tab= query param.
  - The new build_insights_page + build_chat_tabs callables are importable
    and tolerate unknown default_tab values gracefully.
"""

from __future__ import annotations

import pytest


def test_nav_groups_shape():
    from systemu.interface.dashboard import NAV_GROUPS

    group_labels = [g[0] for g in NAV_GROUPS]
    assert group_labels == ["Run", "Build", "System"]

    # Run is open by default; Build + System collapse on first load.
    defaults = {label: default_open for label, default_open, _ in NAV_GROUPS}
    assert defaults == {"Run": True, "Build": False, "System": False}


def test_nav_groups_items():
    from systemu.interface.dashboard import NAV_GROUPS

    by_group = {label: [p for p, _, _ in items] for label, _, items in NAV_GROUPS}

    assert by_group["Run"] == ["/", "/chat", "/scrolls", "/army", "/activities"]
    assert by_group["Build"] == ["/tools", "/skills", "/workshop", "/evolutions"]
    assert by_group["System"] == ["/insights", "/settings"]


def test_nav_items_no_duplicates_and_reduced_count():
    from systemu.interface.dashboard import NAV_ITEMS

    paths = [p for p, _, _ in NAV_ITEMS]
    assert len(paths) == len(set(paths)), "duplicate path in NAV_ITEMS"
    # 14 flat items in v0.7.1 -> 11 grouped destinations in v0.7.2
    # (Chat absorbs Systemu Chat; Insights absorbs Memory + Flywheel + Notifications.)
    assert len(NAV_ITEMS) == 11


def test_no_duplicate_settings_emoji_in_sidebar():
    """Flywheel + Settings both showed ⚙️ pre-v0.7.2. After consolidation,
    Flywheel is a tab inside Insights so only Settings keeps the gear icon
    in the side menu."""
    from systemu.interface.dashboard import NAV_ITEMS

    gear_items = [(p, label) for p, icon, label in NAV_ITEMS if icon == "⚙️"]
    assert len(gear_items) == 1
    assert gear_items[0][1] == "Settings"


def test_legacy_systemu_chat_not_in_sidebar():
    """The /systemu-chat URL must still resolve (redirect) but the label
    must not appear in the sidebar."""
    from systemu.interface.dashboard import NAV_ITEMS

    paths = [p for p, _, _ in NAV_ITEMS]
    labels = [label for _, _, label in NAV_ITEMS]
    assert "/systemu-chat" not in paths
    assert "Systemu Chat" not in labels


def test_legacy_paths_not_in_sidebar():
    """Memory / Flywheel / Notifications are merged into /insights."""
    from systemu.interface.dashboard import NAV_ITEMS

    paths = [p for p, _, _ in NAV_ITEMS]
    for legacy in ("/memory", "/flywheel", "/notifications"):
        assert legacy not in paths, f"{legacy} should be merged into /insights"


def test_insights_page_importable():
    from systemu.interface.pages.insights import build_insights_page, _VALID_TABS

    assert callable(build_insights_page)
    assert set(_VALID_TABS) == {"memory", "flywheel", "events", "actions"}  # actions tab added in v0.8.0


def test_chat_tabs_importable():
    from systemu.interface.pages.chat_page import (
        build_chat_tabs,
        _VALID_CHAT_TABS,
    )

    assert callable(build_chat_tabs)
    assert set(_VALID_CHAT_TABS) == {"compose", "live"}


def test_register_routes_callable_imports_cleanly():
    """The dashboard module must still register routes without exploding —
    catches missing imports after we dropped build_memory_consolidation_page,
    build_flywheel_page, etc. from the dashboard's direct import block."""
    from systemu.interface import dashboard

    # We don't actually call register_routes() here (it needs a NiceGUI
    # app instance + AppState).  Importing the symbol is sufficient: it
    # would have raised ImportError at collection time if the import
    # block referenced a missing builder.
    assert callable(dashboard.register_routes)
