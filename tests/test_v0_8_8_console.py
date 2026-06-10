"""v0.8.8 console revamp tests."""
from __future__ import annotations
import pytest


class TestNavHelpers:
    def test_tile_nav_target_known_labels(self):
        from systemu.interface.nav_helpers import tile_nav_target
        assert tile_nav_target("Scrolls") == "/scrolls"
        assert tile_nav_target("Shadows") == "/shadows"
        assert tile_nav_target("Tools") == "/tools"
        assert tile_nav_target("Skills") == "/skills"
        assert tile_nav_target("Activities") == "/activities"
        assert tile_nav_target("Evolutions") == "/evolutions"

    def test_tile_nav_target_unknown_returns_none(self):
        from systemu.interface.nav_helpers import tile_nav_target
        assert tile_nav_target("Bogus") is None

    # Phase 6 Slice 6f: workshop_deeplink / resolve_deeplink_tab were deleted
    # with the /workshop route — the Scrolls rebuild is now an in-place dialog
    # (scroll_rebuild.open_scroll_rebuild_dialog), so there's no deeplink to
    # build or tab to resolve.  Their tests are retired here.


class TestNavGroups:
    """v0.8.8 asserted the NAV_TOP + 3-group sidebar; Phase 5 (Slice 1)
    replaced it with the flat 6-spine nav (canary:
    tests/test_phase5_nav_spines.py). The Console page still serves "/" —
    it is now the Home spine's surface — so these assert the spine-era
    equivalents of the v0.8.8 invariants (console at the root, no Overview
    entry in the nav)."""

    def test_root_spine_serves_console_surface(self):
        from systemu.interface.dashboard import NAV_SPINES
        assert NAV_SPINES[0][0] == "/"      # Home spine leads the nav

    def test_overview_not_a_nav_label(self):
        from systemu.interface.dashboard import NAV_SPINES
        labels = [label for _p, _i, label in NAV_SPINES]
        assert "Overview" not in labels

    def test_nav_items_alias_starts_at_root(self):
        from systemu.interface.dashboard import NAV_ITEMS
        assert NAV_ITEMS[0][0] == "/"


class TestSharedHelpers:
    def test_render_decision_card_importable(self):
        # Extracted shared helper must be importable + callable
        from systemu.interface.pages.insights import render_decision_card
        assert callable(render_decision_card)

    def test_build_events_log_pane_importable(self):
        from systemu.interface.pages.notifications_page import build_events_log_pane
        assert callable(build_events_log_pane)

    def test_load_events_importable(self):
        # The file-tail loader used by both notifications page + console pane
        from systemu.interface.pages.notifications_page import _load_events
        assert callable(_load_events)


class TestLiveEventsPane:
    def test_ring_buffer_caps_at_max(self):
        from systemu.interface.components.live_events_pane import _append_capped
        buf = []
        for i in range(80):
            _append_capped(buf, {"message": f"e{i}"}, max_len=50)
        assert len(buf) == 50
        # Oldest dropped, newest kept
        assert buf[0]["message"] == "e30"
        assert buf[-1]["message"] == "e79"

    def test_level_color_mapping(self):
        from systemu.interface.components.live_events_pane import _level_color
        from systemu.interface.dashboard_state import THEME
        assert _level_color("ERROR") == THEME["danger"]
        assert _level_color("WARNING") == THEME["warning"]
        assert _level_color("SUCCESS") == THEME["success"]
        assert _level_color("INFO") == THEME["text_muted"]
        assert _level_color("anything-else") == THEME["text_muted"]


class TestConsolePage:
    def test_build_console_page_importable(self):
        from systemu.interface.pages.console import build_console_page
        assert callable(build_console_page)

    def test_stat_card_accepts_nav_target(self):
        # _stat_card must accept the new nav_target kwarg without error at
        # import/signature level
        import inspect
        from systemu.interface.pages.console import _stat_card
        sig = inspect.signature(_stat_card)
        assert "nav_target" in sig.parameters


# Phase 6 Slice 6f: TestWorkshopDeeplinkHandler is retired with the /workshop
# route + page.  build_workshop_page no longer exists; the Scrolls rebuild
# (Workshop's last surface) is covered by tests/test_scroll_rebuild.py.
