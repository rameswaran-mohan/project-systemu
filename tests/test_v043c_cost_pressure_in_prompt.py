"""Tests for v0.4.3-c — cost-pressure surfaced to the supervisor LLM.

Verifies that ExecutionMind's LLM payload now carries:
  * hour_spent_usd / hour_cap_usd / hour_utilisation
  * day_spent_usd / day_cap_usd / day_utilisation
  * near_cap flag (true when either utilisation ≥ 0.75)
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from systemu.runtime import supervisor_cost_ledger as scl
from systemu.runtime.execution_mind import ExecutionMind


@pytest.fixture(autouse=True)
def _reset_singleton():
    scl.reset_singleton_for_tests()
    yield
    scl.reset_singleton_for_tests()


def _config():
    return SimpleNamespace(
        intelligent_supervisor_enabled=True,
        supervisor_llm_budget_per_run=10,
        supervisor_tier_routine="tier_3",
        supervisor_tier_intervention="tier_1",
        supervisor_directive_timeout_s=1.0,
        supervisor_llm_budget_per_hour_usd=5.0,
        supervisor_llm_budget_per_day_usd=50.0,
    )


def _make_mind(tmp_path):
    return ExecutionMind(
        execution_id="exec_test",
        shadow_id="sh-1",
        config=_config(),
        directive_sink=lambda d: None,
        data_dir=tmp_path,
    )


class TestCostPressureInPayload:
    def test_fresh_ledger_yields_zero_utilisation(self, tmp_path, monkeypatch):
        monkeypatch.setattr(scl, "_DEFAULT_LEDGER_PATH", tmp_path / "cost.json")
        captured: dict = {}

        def fake_llm(*, tier, system, user, config, temperature, max_tokens):
            captured["user"] = user
            return {"action": "DO_NOTHING", "rationale": "ok"}

        monkeypatch.setattr(
            "systemu.core.llm_router.llm_call_json", fake_llm,
        )

        mind = _make_mind(tmp_path)
        mind.evaluate(
            trigger="tool_failure",
            recent_events=[],
            classifier="param_error",
            consec_failures=1,
            iteration=1,
        )
        payload = json.loads(captured["user"])
        cp = payload["cost_pressure"]
        assert cp["hour_spent_usd"] == 0.0
        assert cp["hour_cap_usd"] == 5.0
        assert cp["hour_utilisation"] == 0.0
        assert cp["near_cap"] is False

    def test_high_spend_sets_near_cap(self, tmp_path, monkeypatch):
        monkeypatch.setattr(scl, "_DEFAULT_LEDGER_PATH", tmp_path / "cost.json")
        # Manually pre-fill the ledger so the next call sees high pressure.
        ledger = scl.get_ledger(_config())
        ledger.record(4.0)   # 80% of $5.00 hour cap
        captured: dict = {}

        def fake_llm(*, tier, system, user, config, temperature, max_tokens):
            captured["user"] = user
            return {"action": "DO_NOTHING", "rationale": "ok"}

        monkeypatch.setattr(
            "systemu.core.llm_router.llm_call_json", fake_llm,
        )

        mind = _make_mind(tmp_path)
        mind.evaluate(
            trigger="tool_failure",
            recent_events=[],
            classifier="param_error",
            consec_failures=1,
            iteration=2,
        )
        payload = json.loads(captured["user"])
        cp = payload["cost_pressure"]
        assert cp["hour_utilisation"] >= 0.75
        assert cp["near_cap"] is True

    def test_payload_includes_both_buckets(self, tmp_path, monkeypatch):
        """Pin: hour AND day fields both present so the prompt can reason
        about both axes independently."""
        monkeypatch.setattr(scl, "_DEFAULT_LEDGER_PATH", tmp_path / "cost.json")
        captured: dict = {}

        def fake_llm(*, tier, system, user, config, temperature, max_tokens):
            captured["user"] = user
            return {"action": "DO_NOTHING", "rationale": "ok"}

        monkeypatch.setattr(
            "systemu.core.llm_router.llm_call_json", fake_llm,
        )

        mind = _make_mind(tmp_path)
        mind.evaluate(
            trigger="tool_failure",
            recent_events=[],
            classifier="param_error",
            consec_failures=1,
            iteration=1,
        )
        payload = json.loads(captured["user"])
        cp = payload["cost_pressure"]
        assert "hour_spent_usd" in cp
        assert "hour_cap_usd" in cp
        assert "day_spent_usd" in cp
        assert "day_cap_usd" in cp

    def test_zero_cap_yields_zero_utilisation_no_crash(self, tmp_path, monkeypatch):
        """When the operator disables a cap (= 0), utilisation must compute
        as 0.0 not crash with DivisionByZero."""
        monkeypatch.setattr(scl, "_DEFAULT_LEDGER_PATH", tmp_path / "cost.json")
        cfg = _config()
        cfg.supervisor_llm_budget_per_hour_usd = 0
        cfg.supervisor_llm_budget_per_day_usd = 0
        captured: dict = {}

        def fake_llm(*, tier, system, user, config, temperature, max_tokens):
            captured["user"] = user
            return {"action": "DO_NOTHING", "rationale": "ok"}

        monkeypatch.setattr(
            "systemu.core.llm_router.llm_call_json", fake_llm,
        )

        mind = ExecutionMind(
            execution_id="exec_test",
            shadow_id="sh-1",
            config=cfg,
            directive_sink=lambda d: None,
            data_dir=tmp_path,
        )
        mind.evaluate(
            trigger="tool_failure",
            recent_events=[],
            classifier=None,
            consec_failures=0,
            iteration=1,
        )
        payload = json.loads(captured["user"])
        cp = payload["cost_pressure"]
        assert cp["hour_utilisation"] == 0.0
        assert cp["day_utilisation"] == 0.0
        assert cp["near_cap"] is False
