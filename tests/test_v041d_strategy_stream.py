"""Tests for v0.4.1-d strategy-stream UI.

Covers:
  * EventBus.publish_supervisor_action emits an event with the expected shape
  * Each ExecutionMind.evaluate() call publishes one strategy-stream event
  * Supervisor filter in chat config includes the new category
  * Event payload carries execution_id, action, tier_used, pattern_signature
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from systemu.interface.event_bus import EventBus
from systemu.runtime.execution_mind import ExecutionMind


@pytest.fixture(autouse=True)
def _reset_state():
    EventBus.get().reset_dep_publish_state_for_tests()
    yield


# ─────────────────────────────────────────────────────────────────────────────

class TestPublishSupervisorAction:
    def test_publishes_with_expected_shape(self):
        events: list = []
        unsub = EventBus.get().subscribe(lambda e: events.append(e), replay=False)
        try:
            EventBus.get().publish_supervisor_action(
                execution_id="exec_x",
                action="NUDGE",
                rationale="schema requires snake_case",
                classifier="param_error",
                consec_failures=1,
                iteration=3,
                tier_used="tier_3",
                shadow_id="sh-1",
                pattern_signature="param_error|unknown|snake_case",
            )
        finally:
            unsub()
        cat_events = [e for e in events if e.get("category") == "supervisor_action"]
        assert len(cat_events) == 1
        ctx = cat_events[0]["context"]
        assert ctx["execution_id"]      == "exec_x"
        assert ctx["supervisor_action"] == "NUDGE"
        assert ctx["classifier"]        == "param_error"
        assert ctx["consec_failures"]   == 1
        assert ctx["tier_used"]         == "tier_3"
        assert ctx["pattern_signature"] == "param_error|unknown|snake_case"

    def test_message_contains_action_glyph(self):
        events: list = []
        unsub = EventBus.get().subscribe(lambda e: events.append(e), replay=False)
        try:
            EventBus.get().publish_supervisor_action(
                execution_id="exec_x", action="ROLLBACK",
            )
        finally:
            unsub()
        msg = events[-1]["message"]
        assert "ROLLBACK" in msg
        assert "Supervisor" in msg


# ─────────────────────────────────────────────────────────────────────────────

class TestExecutionMindPublishesStream:
    def test_each_evaluate_emits_one_stream_event(self, tmp_path, monkeypatch):
        events: list = []
        unsub = EventBus.get().subscribe(lambda e: events.append(e), replay=False)
        try:
            config = SimpleNamespace(
                intelligent_supervisor_enabled=True,
                supervisor_llm_budget_per_run=10,
                supervisor_tier_routine="tier_3",
                supervisor_tier_intervention="tier_1",
                supervisor_directive_timeout_s=1.0,
            )
            monkeypatch.setattr(
                "systemu.core.llm_router.llm_call_json",
                lambda **kw: {
                    "action": "NUDGE",
                    "rationale": "ok",
                    "hint": "use kebab-case",
                },
            )
            mind = ExecutionMind(
                execution_id="exec_test",
                shadow_id="sh-1",
                config=config,
                directive_sink=lambda d: None,
                data_dir=tmp_path,
            )
            mind.evaluate(
                trigger="tool_failure",
                recent_events=[],
                classifier="param_error",
                consec_failures=1,
                iteration=4,
            )
        finally:
            unsub()
        stream_events = [e for e in events if e.get("category") == "supervisor_action"]
        assert len(stream_events) == 1
        ctx = stream_events[0]["context"]
        assert ctx["execution_id"] == "exec_test"
        assert ctx["supervisor_action"] in ("NUDGE", "DO_NOTHING")  # NUDGE unless rejection-store blocks


# ─────────────────────────────────────────────────────────────────────────────

class TestSupervisorFilterIncludesStream:
    def test_filter_categories_include_supervisor_action(self):
        from systemu.interface.pages.systemu_chat import FILTER_CATEGORIES
        assert "supervisor_action" in FILTER_CATEGORIES["Supervisor"]

    def test_action_glyphs_cover_all_vocabulary(self):
        from systemu.interface.pages.systemu_chat import SUPERVISOR_ACTION_GLYPHS
        from systemu.runtime.execution_mind import ACTION_VOCABULARY
        for action in ACTION_VOCABULARY:
            assert action in SUPERVISOR_ACTION_GLYPHS, f"missing glyph for {action}"
