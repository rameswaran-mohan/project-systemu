"""Tests for v0.4.4-a — tool-level success-rate tracking.

Covers:
  * record() updates the right counters
  * Dependency-blocked failures don't penalise success_rate
  * Timeouts incremented separately + counted as failures
  * Neutral default (success_rate=0.5) for cold-start tools
  * list_all sorted by lowest success_rate then highest call volume,
    cold-start tools last
  * low_success_tools threshold + min_calls filtering
  * Persistence round-trip
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from systemu.runtime.tool_metrics import (
    ToolMetricEntry, ToolMetrics,
    get_tool_metrics, reset_singleton_for_tests,
)


@pytest.fixture(autouse=True)
def _reset():
    reset_singleton_for_tests()
    yield
    reset_singleton_for_tests()


# ─────────────────────────────────────────────────────────────────────────────

class TestRecord:
    def test_success_only(self, tmp_path):
        store = ToolMetrics(tmp_path / "tm.json")
        store.record(tool_id="tool_x", success=True)
        store.record(tool_id="tool_x", success=True)
        store.record(tool_id="tool_x", success=True)
        e = store.get("tool_x")
        assert e.calls == 3
        assert e.successes == 3
        assert e.failures == 0
        assert e.success_rate == 1.0

    def test_mixed_success_failure(self, tmp_path):
        store = ToolMetrics(tmp_path / "tm.json")
        store.record(tool_id="tool_x", success=True)
        store.record(tool_id="tool_x", success=False, error_type="param_error")
        e = store.get("tool_x")
        assert e.calls == 2
        assert e.successes == 1
        assert e.failures == 1
        assert e.success_rate == 0.5

    def test_dep_block_separated(self, tmp_path):
        store = ToolMetrics(tmp_path / "tm.json")
        store.record(tool_id="tool_x", success=True)
        store.record(tool_id="tool_x", success=True)
        # 2 dep-blocked failures should NOT drop the success rate
        store.record(tool_id="tool_x", success=False,
                     error_type="missing_dependency")
        store.record(tool_id="tool_x", success=False,
                     error_type="dependency_install_pending_approval")
        e = store.get("tool_x")
        assert e.calls == 4
        assert e.successes == 2
        assert e.failures == 0
        assert e.dependency_blocked == 2
        # Attributable calls = 4 - 2 = 2; success_rate = 2/2 = 1.0
        assert e.attributable_calls == 2
        assert e.success_rate == 1.0

    def test_timeout_counted_separately(self, tmp_path):
        store = ToolMetrics(tmp_path / "tm.json")
        store.record(tool_id="tool_x", success=False,
                     error_type="timeout", timed_out=True)
        e = store.get("tool_x")
        assert e.failures == 1
        assert e.timeouts == 1

    def test_empty_tool_id_skipped(self, tmp_path):
        store = ToolMetrics(tmp_path / "tm.json")
        store.record(tool_id="", success=True)
        assert store.list_all() == []


# ─────────────────────────────────────────────────────────────────────────────

class TestNeutralDefault:
    def test_unknown_returns_neutral(self, tmp_path):
        store = ToolMetrics(tmp_path / "tm.json")
        e = store.get("never_seen")
        assert e.calls == 0
        assert e.success_rate == 0.5
        assert e.has_history is False


# ─────────────────────────────────────────────────────────────────────────────

class TestListAll:
    def test_sorted_by_lowest_success_rate(self, tmp_path):
        store = ToolMetrics(tmp_path / "tm.json")
        # tool_a: 5 attributable, success_rate 0.4
        for _ in range(2):
            store.record(tool_id="tool_a", success=True)
        for _ in range(3):
            store.record(tool_id="tool_a", success=False,
                         error_type="param_error")
        # tool_b: 5 attributable, success_rate 0.8
        for _ in range(4):
            store.record(tool_id="tool_b", success=True)
        store.record(tool_id="tool_b", success=False,
                     error_type="param_error")
        # tool_c: 0 calls — cold-start
        # tool_d: only dep-block failures — 0 attributable
        store.record(tool_id="tool_d", success=False,
                     error_type="missing_dependency")

        rows = store.list_all()
        ids = [r["tool_id"] for r in rows]
        # tool_a (worst rate) first, tool_b second, tool_d last (cold)
        assert ids[:2] == ["tool_a", "tool_b"]
        assert ids[-1] == "tool_d"


class TestLowSuccessTools:
    def test_below_threshold_flagged(self, tmp_path):
        store = ToolMetrics(tmp_path / "tm.json")
        for _ in range(3):
            store.record(tool_id="bad", success=False, error_type="param_error")
        for _ in range(2):
            store.record(tool_id="bad", success=True)
        # 2/5 = 0.4 success
        flagged = store.low_success_tools(threshold=0.5, min_calls=5)
        assert any(r["tool_id"] == "bad" for r in flagged)

    def test_under_min_calls_not_flagged(self, tmp_path):
        store = ToolMetrics(tmp_path / "tm.json")
        store.record(tool_id="rare", success=False, error_type="param_error")
        # 1 call, well below min_calls=5
        flagged = store.low_success_tools(threshold=0.5, min_calls=5)
        assert flagged == []

    def test_above_threshold_not_flagged(self, tmp_path):
        store = ToolMetrics(tmp_path / "tm.json")
        for _ in range(5):
            store.record(tool_id="good", success=True)
        store.record(tool_id="good", success=False, error_type="param_error")
        # 5/6 ≈ 0.83
        flagged = store.low_success_tools(threshold=0.5, min_calls=3)
        assert flagged == []


class TestPersistence:
    def test_round_trip(self, tmp_path):
        path = tmp_path / "tm.json"
        a = ToolMetrics(path)
        a.record(tool_id="tool_x", success=True)
        a.record(tool_id="tool_x", success=False, error_type="param_error")
        b = ToolMetrics(path)
        e = b.get("tool_x")
        assert e.calls == 2
        assert e.successes == 1
        assert e.failures == 1


class TestClear:
    def test_wipes(self, tmp_path):
        store = ToolMetrics(tmp_path / "tm.json")
        store.record(tool_id="a", success=True)
        store.record(tool_id="b", success=True)
        assert store.clear() == 2
        assert store.list_all() == []
