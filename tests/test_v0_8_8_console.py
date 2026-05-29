"""v0.8.8 console revamp tests."""
from __future__ import annotations
import pytest


class TestNavHelpers:
    def test_tile_nav_target_known_labels(self):
        from systemu.interface.nav_helpers import tile_nav_target
        assert tile_nav_target("Scrolls") == "/scrolls"
        assert tile_nav_target("Shadows") == "/army"
        assert tile_nav_target("Tools") == "/tools"
        assert tile_nav_target("Skills") == "/skills"
        assert tile_nav_target("Activities") == "/activities"
        assert tile_nav_target("Evolutions") == "/evolutions"

    def test_tile_nav_target_unknown_returns_none(self):
        from systemu.interface.nav_helpers import tile_nav_target
        assert tile_nav_target("Bogus") is None

    def test_workshop_deeplink_builds_query_url(self):
        from systemu.interface.nav_helpers import workshop_deeplink
        assert workshop_deeplink("scroll", "scroll_abc") == "/workshop?type=scroll&id=scroll_abc"
        assert workshop_deeplink("shadow", "shadow_x") == "/workshop?type=shadow&id=shadow_x"

    def test_resolve_deeplink_tab_known_types(self):
        from systemu.interface.nav_helpers import resolve_deeplink_tab
        assert resolve_deeplink_tab("scroll") == "Scrolls"
        assert resolve_deeplink_tab("shadow") == "Shadows"
        assert resolve_deeplink_tab("tool") == "Tools"
        assert resolve_deeplink_tab("skill") == "Skills"

    def test_resolve_deeplink_tab_default_scrolls(self):
        from systemu.interface.nav_helpers import resolve_deeplink_tab
        assert resolve_deeplink_tab(None) == "Scrolls"
        assert resolve_deeplink_tab("") == "Scrolls"
        assert resolve_deeplink_tab("unknown") == "Scrolls"


class TestNavGroups:
    def test_nav_top_is_console(self):
        from systemu.interface.dashboard import NAV_TOP
        assert NAV_TOP == ("/", "🖥️", "Console")

    def test_overview_removed_from_groups(self):
        from systemu.interface.dashboard import NAV_GROUPS
        all_labels = [label for _, _, items in NAV_GROUPS for _, _, label in items]
        assert "Overview" not in all_labels
        # Console is NOT inside a group (it's NAV_TOP)
        assert "Console" not in all_labels

    def test_run_group_first_item_is_chat(self):
        from systemu.interface.dashboard import NAV_GROUPS
        run_group = next(items for label, _, items in NAV_GROUPS if label == "Run")
        assert run_group[0][2] == "Chat"   # Overview no longer leads Run

    def test_nav_items_includes_console_first(self):
        from systemu.interface.dashboard import NAV_ITEMS
        assert NAV_ITEMS[0] == ("/", "🖥️", "Console")


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

    def test_overview_reexports_console(self):
        # Back-compat: build_overview_page must still be importable + identical
        from systemu.interface.pages.overview import build_overview_page
        from systemu.interface.pages.console import build_console_page
        assert build_overview_page is build_console_page

    def test_stat_card_accepts_nav_target(self):
        # _stat_card must accept the new nav_target kwarg without error at
        # import/signature level
        import inspect
        from systemu.interface.pages.console import _stat_card
        sig = inspect.signature(_stat_card)
        assert "nav_target" in sig.parameters


class TestWorkshopDeeplinkHandler:
    def test_build_workshop_page_accepts_deeplink_kwargs(self):
        import inspect
        from systemu.interface.pages.workshop import build_workshop_page
        sig = inspect.signature(build_workshop_page)
        assert "deeplink_type" in sig.parameters
        assert "deeplink_id" in sig.parameters

    def test_resolve_deeplink_tab_used_by_workshop(self):
        # Workshop must resolve the initial tab via the shared helper
        from systemu.interface.nav_helpers import resolve_deeplink_tab
        assert resolve_deeplink_tab("tool") == "Tools"
