"""Tests for v0.4.4-b — operator dashboard data access.

The UI itself (NiceGUI dialogs/tables) is hard to test in isolation,
so these tests validate the data-access *contracts* the UI relies on:

  * Army page can pull shadow metrics for a given shadow_id by reading
    shadow_metrics.json and filtering by row.shadow_id.
  * Workflow detail can pull affinity entries via
    AffinityLog.recent_terminations(shadow_id=...).
  * Tools page can pull ToolMetrics.get(tool_id) for any tool listed.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from systemu.runtime import affinity_log as al
from systemu.runtime import shadow_metrics as sm
from systemu.runtime import tool_metrics as tm


@pytest.fixture(autouse=True)
def _reset():
    al.reset_singleton_for_tests()
    sm.reset_singleton_for_tests()
    tm.reset_singleton_for_tests()
    yield
    al.reset_singleton_for_tests()
    sm.reset_singleton_for_tests()
    tm.reset_singleton_for_tests()


# ─────────────────────────────────────────────────────────────────────────────
# Army page contract: filter shadow_metrics rows by shadow_id

class TestArmyShadowMetricsContract:
    def test_filter_by_shadow_id(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sm, "_DEFAULT_PATH", tmp_path / "shadow_metrics.json")
        store = sm.get_shadow_metrics()
        store.record(shadow_id="sh-A", intent_hash="h1", status="success")
        store.record(shadow_id="sh-A", intent_hash="h2", status="failure")
        store.record(shadow_id="sh-B", intent_hash="h1", status="success")

        # Read the file the way Army page does
        raw = json.loads(store.path.read_text(encoding="utf-8"))
        sh_a_rows = [r for r in raw.get("rows", {}).values() if r.get("shadow_id") == "sh-A"]
        sh_b_rows = [r for r in raw.get("rows", {}).values() if r.get("shadow_id") == "sh-B"]
        assert len(sh_a_rows) == 2
        assert len(sh_b_rows) == 1


# ─────────────────────────────────────────────────────────────────────────────
# Workflow detail contract: affinity log filtered by shadow_id

class TestWorkflowDetailAffinityContract:
    def test_recent_terminations_for_shadow(self, tmp_path, monkeypatch):
        monkeypatch.setattr(al, "_DEFAULT_PATH", tmp_path / "affinity.json")
        log = al.get_affinity_log()
        log.record_termination(intent_hash="h1", shadow_id="sh-A")
        log.record_termination(intent_hash="h2", shadow_id="sh-A")
        log.record_termination(intent_hash="h1", shadow_id="sh-B")

        entries = log.recent_terminations(shadow_id="sh-A", window_hours=168)
        assert len(entries) == 2
        assert all(e.shadow_id == "sh-A" for e in entries)


# ─────────────────────────────────────────────────────────────────────────────
# Tools list contract: tool_metrics.get returns neutral default for unknown

class TestToolsListMetricsContract:
    def test_unknown_tool_returns_neutral(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tm, "_DEFAULT_PATH", tmp_path / "tool_metrics.json")
        entry = tm.get_tool_metrics().get("never_called_tool")
        # Tools list checks .has_history before rendering — that flag MUST be
        # False for unrecorded tools so the UI renders "—" instead of "50%".
        assert entry.has_history is False
        assert entry.attributable_calls == 0

    def test_recorded_tool_returns_real_rate(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tm, "_DEFAULT_PATH", tmp_path / "tool_metrics.json")
        store = tm.get_tool_metrics()
        for _ in range(4):
            store.record(tool_id="t", success=True)
        store.record(tool_id="t", success=False, error_type="param_error")
        entry = store.get("t")
        assert entry.has_history is True
        assert entry.attributable_calls == 5
        assert entry.success_rate == 0.8
