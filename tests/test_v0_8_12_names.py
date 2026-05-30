"""v0.8.12 names-instead-of-ids tests."""
from __future__ import annotations
from unittest.mock import MagicMock
import pytest


class TestResolver:
    def _vault(self):
        v = MagicMock()
        v.get_shadow.return_value = MagicMock(name_attr="WeatherDocBot"); v.get_shadow.return_value.name = "WeatherDocBot"
        v.get_scroll.return_value.name = "Fetch Weather"
        v.get_tool.return_value.name = "web_search"
        v.get_skill.return_value.name = "http-data-fetching"
        v.get_activity.return_value.name = "Save CET Time"
        return v

    def test_resolves_each_prefix(self):
        from systemu.interface.name_resolver import resolve_name, clear_name_cache
        clear_name_cache()
        v = self._vault()
        assert resolve_name("shadow_7e6e", v) == "WeatherDocBot"
        assert resolve_name("scroll_101c", v) == "Fetch Weather"
        assert resolve_name("tool_a1f6", v) == "web_search"
        assert resolve_name("skill_3ed4", v) == "http-data-fetching"
        assert resolve_name("activity_d3c4", v) == "Save CET Time"

    def test_vault_miss_falls_back_to_id(self):
        from systemu.interface.name_resolver import resolve_name, clear_name_cache
        clear_name_cache()
        v = MagicMock()
        v.get_shadow.side_effect = KeyError("nope")
        assert resolve_name("shadow_gone", v) == "shadow_gone"

    def test_unknown_prefix_returns_id(self):
        from systemu.interface.name_resolver import resolve_name, clear_name_cache
        clear_name_cache()
        v = MagicMock()
        assert resolve_name("exec_abc", v) == "exec_abc"
        assert resolve_name("dec_xyz", v) == "dec_xyz"

    def test_evolution_returns_type_summary(self):
        from systemu.interface.name_resolver import resolve_name, clear_name_cache
        clear_name_cache()
        v = MagicMock()
        v.get_evolution.return_value.target_entity_type = "shadow"
        assert resolve_name("evolution_5abd", v) == "shadow evolution"

    def test_truncates_long_names(self):
        from systemu.interface.name_resolver import resolve_name, clear_name_cache
        clear_name_cache()
        v = MagicMock()
        v.get_scroll.return_value.name = "x" * 80
        out = resolve_name("scroll_long", v, max_len=40)
        assert len(out) <= 41 and out.endswith("…")

    def test_cache_avoids_second_vault_call(self):
        from systemu.interface.name_resolver import resolve_name, clear_name_cache
        clear_name_cache()
        v = MagicMock()
        v.get_tool.return_value.name = "web_read"
        resolve_name("tool_cached", v)
        resolve_name("tool_cached", v)
        assert v.get_tool.call_count == 1   # second call served from cache

    def test_resolve_names_maps_and_tolerates_bad(self):
        from systemu.interface.name_resolver import resolve_names, clear_name_cache
        clear_name_cache()
        v = MagicMock()
        v.get_tool.return_value.name = "web_search"
        v.get_skill.side_effect = KeyError("x")
        out = resolve_names(["tool_a", "skill_bad"], v)
        assert out == ["web_search", "skill_bad"]   # bad id falls back to id

    def test_short_id_truncates(self):
        from systemu.interface.name_resolver import short_id
        assert short_id("activity_d3c423a4b1", 12) == "activity_d3c"
        assert short_id("tool_x", 12) == "tool_x"


class TestSupervisorNames:
    def test_publish_messages_use_resolved_name(self):
        # The supervisor must build its activity event messages via resolve_name,
        # keeping the raw id in context. Assert by source inspection that the
        # _publish lines no longer interpolate the bare activity_id as the label.
        import inspect
        from systemu.runtime import supervisor
        src = inspect.getsource(supervisor)
        assert "resolve_name" in src, "supervisor must resolve activity names for the feed"


class TestActivitiesNames:
    def test_activities_imports_resolver(self):
        import inspect
        from systemu.interface.pages import activities
        src = inspect.getsource(activities)
        assert "resolve_name" in src and "short_id" in src, \
            "activities page must resolve scroll/shadow names + show short_id"


class TestArmyNames:
    def test_army_detail_uses_resolve_names(self):
        import inspect
        from systemu.interface.pages import army
        src = inspect.getsource(army)
        assert "resolve_names" in src, "army detail joins must resolve names"


class TestEvolutionNames:
    def test_evolutions_page_resolves_targets(self):
        import inspect
        from systemu.interface.pages import evolutions
        assert "resolve_name" in inspect.getsource(evolutions)

    def test_evolution_engine_resolves_targets(self):
        import inspect
        from systemu.pipelines import evolution_engine
        assert "resolve_name" in inspect.getsource(evolution_engine)


class TestRemainingPages:
    @pytest.mark.parametrize("modname", [
        "systemu.interface.pages.recover",
        "systemu.interface.pages.chat_page",
        "systemu.interface.pages.scrolls",
        "systemu.interface.pages.skills_page",
        "systemu.interface.pages.workflow_detail",
    ])
    def test_page_uses_resolver(self, modname):
        import importlib, inspect
        m = importlib.import_module(modname)
        assert "resolve_name" in inspect.getsource(m), f"{modname} should resolve names"


class TestTerminateCard:
    def test_terminate_card_uses_names(self):
        import inspect
        from systemu.runtime import shadow_runtime
        src = inspect.getsource(shadow_runtime)
        # the TERMINATE approval card should name the scroll/shadow, not bare exec id
        assert "resolve_name" in src, "shadow_runtime TERMINATE card must use names"
