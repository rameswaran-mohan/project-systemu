"""Tests for v0.4.0-d Intelligent Supervisor (ExecutionMind).

Covers:
  * ExecutionMind is no-op when disabled (the v0.4.0 default)
  * Budget cap: directives after the budget return DO_NOTHING
  * Audit file written with hypothesis + decision
  * Timeout path returns DO_NOTHING and doesn't crash
  * Action vocabulary enforcement: unknown actions fall back to DO_NOTHING
  * DirectiveInbox FIFO + bounded
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from systemu.runtime.execution_mind import (
    ACTION_VOCABULARY, HIGH_IMPACT_ACTIONS,
    Directive, DirectiveInbox, ExecutionMind, Hypothesis,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers

def _config(*, enabled=True, budget=10, tier_routine="tier_3", tier_intervention="tier_1"):
    c = SimpleNamespace()
    c.intelligent_supervisor_enabled = enabled
    c.supervisor_llm_budget_per_run = budget
    c.supervisor_tier_routine = tier_routine
    c.supervisor_tier_intervention = tier_intervention
    c.supervisor_directive_timeout_s = 5.0
    c.openrouter_api_key = "test"
    c.tier1_model = "t1"
    c.tier2_model = "t2"
    c.tier3_model = "t3"
    return c


def _mind(tmp_path, *, enabled=True, budget=10, sink=None):
    sink = sink or []
    return ExecutionMind(
        execution_id="exec_test",
        shadow_id="sh-test",
        config=_config(enabled=enabled, budget=budget),
        directive_sink=sink.append if isinstance(sink, list) else sink,
        data_dir=tmp_path,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1) Killswitch

class TestKillswitch:
    def test_disabled_returns_do_nothing(self, tmp_path):
        sink = []
        mind = _mind(tmp_path, enabled=False, sink=sink)
        d = mind.evaluate(
            trigger="tool_failure",
            recent_events=[],
            classifier=None,
            consec_failures=1,
            iteration=1,
        )
        assert d.action == "DO_NOTHING"
        assert "disabled" in d.rationale

    def test_disabled_does_not_call_sink(self, tmp_path):
        sink = []
        mind = _mind(tmp_path, enabled=False, sink=sink)
        mind.evaluate(
            trigger="tool_failure", recent_events=[], classifier=None,
            consec_failures=1, iteration=1,
        )
        # Disabled returns immediately without going through the sink
        # (the sink is only fed when the LLM path runs).
        assert sink == []


# ─────────────────────────────────────────────────────────────────────────────
# 2) Budget cap

class TestBudgetCap:
    def test_budget_exhaustion_returns_do_nothing(self, tmp_path, monkeypatch):
        sink = []
        mind = _mind(tmp_path, budget=2, sink=sink)

        def fake_llm(**kw):
            return {"action": "NUDGE", "rationale": "go", "hint": "do x"}
        monkeypatch.setattr(
            "systemu.core.llm_router.llm_call_json", fake_llm,
        )

        # First 2 calls use up the budget...
        d1 = mind.evaluate(
            trigger="tool_failure", recent_events=[], classifier="param_error",
            consec_failures=1, iteration=1,
        )
        d2 = mind.evaluate(
            trigger="tool_failure", recent_events=[], classifier="param_error",
            consec_failures=2, iteration=2,
        )
        # ...third returns DO_NOTHING with budget rationale
        d3 = mind.evaluate(
            trigger="tool_failure", recent_events=[], classifier="param_error",
            consec_failures=3, iteration=3,
        )
        assert d1.action == "NUDGE"
        assert d2.action == "NUDGE"
        assert d3.action == "DO_NOTHING"
        assert "budget" in d3.rationale


# ─────────────────────────────────────────────────────────────────────────────
# 3) Audit file

class TestAuditFile:
    def test_writes_audit_row(self, tmp_path, monkeypatch):
        mind = _mind(tmp_path)
        monkeypatch.setattr(
            "systemu.core.llm_router.llm_call_json",
            lambda **kw: {
                "action": "NUDGE",
                "rationale": "schema requires snake_case",
                "hint": "use snake_case",
                "hypothesis_update": {
                    "trying": "calling tool X",
                    "struggling_on": "param naming",
                    "confidence": 0.7,
                },
            },
        )
        mind.evaluate(
            trigger="tool_failure", recent_events=[{"role": "tool_result", "result": {"err": "x"}}],
            classifier="param_error", consec_failures=1, iteration=4,
        )

        audit = tmp_path / "audit" / "exec_exec_test" / "supervisor.jsonl"
        assert audit.exists()
        rows = [json.loads(l) for l in audit.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(rows) == 1
        assert rows[0]["action"] == "NUDGE"
        assert rows[0]["classifier"] == "param_error"
        assert rows[0]["hypothesis"]["confidence"] == 0.7


# ─────────────────────────────────────────────────────────────────────────────
# 4) Timeout

class TestTimeout:
    def test_llm_timeout_returns_do_nothing(self, tmp_path, monkeypatch):
        def slow_llm(**kw):
            time.sleep(2.0)
            return {"action": "NUDGE", "rationale": "should never see"}
        monkeypatch.setattr(
            "systemu.core.llm_router.llm_call_json", slow_llm,
        )
        mind = _mind(tmp_path)
        d = mind.evaluate(
            trigger="tool_failure", recent_events=[], classifier="param_error",
            consec_failures=1, iteration=1, timeout_s=0.3,
        )
        assert d.action == "DO_NOTHING"
        assert "timed out" in d.rationale


# ─────────────────────────────────────────────────────────────────────────────
# 5) Vocabulary enforcement

class TestVocabulary:
    def test_unknown_action_falls_back(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "systemu.core.llm_router.llm_call_json",
            lambda **kw: {"action": "EAT_LUNCH", "rationale": "off-script"},
        )
        mind = _mind(tmp_path)
        d = mind.evaluate(
            trigger="tool_failure", recent_events=[], classifier=None,
            consec_failures=0, iteration=1,
        )
        assert d.action == "DO_NOTHING"

    def test_all_high_impact_in_vocabulary(self):
        for a in HIGH_IMPACT_ACTIONS:
            assert a in ACTION_VOCABULARY


# ─────────────────────────────────────────────────────────────────────────────
# 6) DirectiveInbox

class TestDirectiveInbox:
    def test_append_and_drain(self):
        inbox = DirectiveInbox()
        inbox.append(Directive(action="NUDGE", hint="x"))
        inbox.append(Directive(action="ROLLBACK"))
        assert len(inbox) == 2
        drained = inbox.drain()
        assert [d.action for d in drained] == ["NUDGE", "ROLLBACK"]
        assert len(inbox) == 0

    def test_bounded(self):
        inbox = DirectiveInbox(maxlen=3)
        for i in range(10):
            inbox.append(Directive(action="DO_NOTHING", rationale=str(i)))
        assert len(inbox) == 3
        rationales = [d.rationale for d in inbox.drain()]
        # Oldest dropped, newest retained
        assert rationales == ["7", "8", "9"]
