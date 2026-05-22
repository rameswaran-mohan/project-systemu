"""Tests for v0.4.3-a — ShadowMetrics tracker + affinity-router consultation.

Covers:
  * record() updates the right (shadow_id, intent_hash) counters
  * Success/failure/partial/cancelled distinction
  * get() returns neutral default for unknown pairs (success_rate=0.5)
  * has_history flag
  * list_for_intent sorted by success_rate desc
  * clear() wipes
  * Supervisor._resolve_shadow_with_affinity prefers shadows with higher
    success_rate when skill_overlap ties
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from systemu.runtime import affinity_log as al
from systemu.runtime import shadow_metrics as sm
from systemu.runtime.shadow_metrics import (
    MetricEntry, ShadowMetrics, get_shadow_metrics, reset_singleton_for_tests,
)


@pytest.fixture(autouse=True)
def _reset():
    reset_singleton_for_tests()
    al.reset_singleton_for_tests()
    yield
    reset_singleton_for_tests()
    al.reset_singleton_for_tests()


# ─────────────────────────────────────────────────────────────────────────────
# ShadowMetrics core

class TestRecordAndGet:
    def test_first_success_records(self, tmp_path):
        store = ShadowMetrics(tmp_path / "metrics.json")
        store.record(shadow_id="sh-A", intent_hash="h1", status="success")
        entry = store.get(shadow_id="sh-A", intent_hash="h1")
        assert entry.executions == 1
        assert entry.successes == 1
        assert entry.success_rate == 1.0
        assert entry.has_history is True

    def test_failure_recorded_separately(self, tmp_path):
        store = ShadowMetrics(tmp_path / "metrics.json")
        store.record(shadow_id="sh-A", intent_hash="h1", status="failure")
        store.record(shadow_id="sh-A", intent_hash="h1", status="success")
        store.record(shadow_id="sh-A", intent_hash="h1", status="success")
        entry = store.get(shadow_id="sh-A", intent_hash="h1")
        assert entry.executions == 3
        assert entry.successes == 2
        assert entry.failures == 1
        assert abs(entry.success_rate - (2 / 3)) < 1e-9

    def test_partial_distinguished_from_failure(self, tmp_path):
        store = ShadowMetrics(tmp_path / "metrics.json")
        store.record(shadow_id="sh-A", intent_hash="h1", status="partial")
        entry = store.get(shadow_id="sh-A", intent_hash="h1")
        assert entry.partials == 1
        assert entry.failures == 0
        # Partials are not attributed as success either
        assert entry.successes == 0

    def test_cancelled_increments_executions_only(self, tmp_path):
        store = ShadowMetrics(tmp_path / "metrics.json")
        store.record(shadow_id="sh-A", intent_hash="h1", status="cancelled")
        entry = store.get(shadow_id="sh-A", intent_hash="h1")
        assert entry.executions == 1
        assert entry.successes == 0
        assert entry.failures == 0
        assert entry.partials == 0


class TestNeutralDefault:
    def test_unknown_pair_returns_neutral(self, tmp_path):
        store = ShadowMetrics(tmp_path / "metrics.json")
        entry = store.get(shadow_id="never-seen", intent_hash="nope")
        assert entry.executions == 0
        assert entry.success_rate == 0.5      # neutral, not 0
        assert entry.has_history is False


class TestListForIntent:
    def test_sorted_by_success_rate_desc(self, tmp_path):
        store = ShadowMetrics(tmp_path / "metrics.json")
        # sh-A: 3/3 success rate
        for _ in range(3):
            store.record(shadow_id="sh-A", intent_hash="h1", status="success")
        # sh-B: 1/3 success rate
        store.record(shadow_id="sh-B", intent_hash="h1", status="success")
        store.record(shadow_id="sh-B", intent_hash="h1", status="failure")
        store.record(shadow_id="sh-B", intent_hash="h1", status="failure")
        # sh-C: 2/2 success rate
        store.record(shadow_id="sh-C", intent_hash="h1", status="success")
        store.record(shadow_id="sh-C", intent_hash="h1", status="success")

        rows = store.list_for_intent("h1")
        assert [r["shadow_id"] for r in rows] == ["sh-A", "sh-C", "sh-B"]

    def test_other_intents_excluded(self, tmp_path):
        store = ShadowMetrics(tmp_path / "metrics.json")
        store.record(shadow_id="sh-A", intent_hash="h1", status="success")
        store.record(shadow_id="sh-B", intent_hash="h2", status="success")
        rows = store.list_for_intent("h1")
        assert [r["shadow_id"] for r in rows] == ["sh-A"]


class TestPersistence:
    def test_round_trips_across_instances(self, tmp_path):
        path = tmp_path / "metrics.json"
        a = ShadowMetrics(path)
        a.record(shadow_id="sh-1", intent_hash="h", status="success")
        a.record(shadow_id="sh-1", intent_hash="h", status="failure")
        b = ShadowMetrics(path)
        e = b.get(shadow_id="sh-1", intent_hash="h")
        assert e.executions == 2
        assert e.successes == 1
        assert e.failures == 1

    def test_clear_resets(self, tmp_path):
        store = ShadowMetrics(tmp_path / "metrics.json")
        store.record(shadow_id="sh-A", intent_hash="h1", status="success")
        store.record(shadow_id="sh-B", intent_hash="h1", status="success")
        assert store.clear() == 2
        assert store.get(shadow_id="sh-A", intent_hash="h1").executions == 0


class TestEmptyInput:
    def test_empty_shadow_id_skipped(self, tmp_path):
        store = ShadowMetrics(tmp_path / "metrics.json")
        store.record(shadow_id="", intent_hash="h", status="success")
        assert store.list_for_intent("h") == []

    def test_empty_intent_hash_skipped(self, tmp_path):
        store = ShadowMetrics(tmp_path / "metrics.json")
        store.record(shadow_id="sh-A", intent_hash="", status="success")
        assert store.get(shadow_id="sh-A", intent_hash="").executions == 0


# ─────────────────────────────────────────────────────────────────────────────
# Supervisor._resolve_shadow_with_affinity consults metrics

class TestSupervisorMetricsRanking:
    def test_higher_success_rate_wins_tie_on_skill_overlap(
        self, tmp_path, monkeypatch,
    ):
        """When two shadows both match required skills exactly, the one
        with the better track record on this intent_hash wins."""
        monkeypatch.setattr(al, "_DEFAULT_PATH", tmp_path / "affinity.json")
        monkeypatch.setattr(sm, "_DEFAULT_PATH", tmp_path / "metrics.json")
        # Seed metrics: sh-B is a proven winner on this intent_hash;
        # sh-C has never tried it.
        scroll_intent = "Generate weather report"
        from systemu.runtime.affinity_log import compute_intent_hash
        objectives = [SimpleNamespace(goal="fetch"), SimpleNamespace(goal="format")]
        ih = compute_intent_hash(intent=scroll_intent, objectives=objectives)

        ms = get_shadow_metrics()
        for _ in range(5):
            ms.record(shadow_id="sh-B", intent_hash=ih, status="success")
        # sh-C has no history → neutral 0.5

        vault = MagicMock()
        vault.get_activity.return_value = SimpleNamespace(
            id="act-1", scroll_id="scr-1",
            required_skill_ids=["skill_x"],
        )
        vault.get_scroll.return_value = SimpleNamespace(
            id="scr-1", intent=scroll_intent, objectives=objectives,
        )
        vault.list_shadows.return_value = [
            {"id": "sh-A", "skill_ids": ["skill_x"]},
            {"id": "sh-B", "skill_ids": ["skill_x"]},
            {"id": "sh-C", "skill_ids": ["skill_x"]},
        ]

        from systemu.runtime.supervisor import Supervisor
        sup = Supervisor.__new__(Supervisor)
        sup.vault = vault
        result = sup._resolve_shadow_with_affinity(
            activity_id="act-1", shadow_id="sh-A",
            exclude_shadow_id="sh-A", scroll_id="scr-1",
        )
        # sh-B preferred over sh-C because it has a higher success rate
        # on the same intent_hash (1.0 vs 0.5 neutral).
        assert result == "sh-B"

    def test_proven_failure_loses_to_neutral_cold_start(
        self, tmp_path, monkeypatch,
    ):
        """A shadow with a poor track record on this intent_hash should
        lose to an untried shadow (neutral default 0.5)."""
        monkeypatch.setattr(al, "_DEFAULT_PATH", tmp_path / "affinity.json")
        monkeypatch.setattr(sm, "_DEFAULT_PATH", tmp_path / "metrics.json")
        from systemu.runtime.affinity_log import compute_intent_hash
        objectives = [SimpleNamespace(goal="fetch")]
        ih = compute_intent_hash(intent="X", objectives=objectives)
        ms = get_shadow_metrics()
        for _ in range(5):
            ms.record(shadow_id="sh-B", intent_hash=ih, status="failure")

        vault = MagicMock()
        vault.get_activity.return_value = SimpleNamespace(
            id="act-1", scroll_id="scr-1",
            required_skill_ids=["skill_x"],
        )
        vault.get_scroll.return_value = SimpleNamespace(
            id="scr-1", intent="X", objectives=objectives,
        )
        vault.list_shadows.return_value = [
            {"id": "sh-A", "skill_ids": ["skill_x"]},
            {"id": "sh-B", "skill_ids": ["skill_x"]},  # proven failure
            {"id": "sh-C", "skill_ids": ["skill_x"]},  # untried, neutral
        ]

        from systemu.runtime.supervisor import Supervisor
        sup = Supervisor.__new__(Supervisor)
        sup.vault = vault
        result = sup._resolve_shadow_with_affinity(
            activity_id="act-1", shadow_id="sh-A",
            exclude_shadow_id="sh-A", scroll_id="scr-1",
        )
        # Untried sh-C (rate=0.5) beats proven-failure sh-B (rate=0.0).
        assert result == "sh-C"

    def test_skill_overlap_still_dominates_metrics(
        self, tmp_path, monkeypatch,
    ):
        """A 2-skill match with neutral history beats a 1-skill match
        with perfect history — capability comes first, history is a
        tiebreaker only at equal overlap."""
        monkeypatch.setattr(al, "_DEFAULT_PATH", tmp_path / "affinity.json")
        monkeypatch.setattr(sm, "_DEFAULT_PATH", tmp_path / "metrics.json")
        from systemu.runtime.affinity_log import compute_intent_hash
        objectives = [SimpleNamespace(goal="g")]
        ih = compute_intent_hash(intent="X", objectives=objectives)
        ms = get_shadow_metrics()
        # sh-B: perfect history but only 1 skill match
        for _ in range(10):
            ms.record(shadow_id="sh-B", intent_hash=ih, status="success")
        vault = MagicMock()
        vault.get_activity.return_value = SimpleNamespace(
            id="act-1", scroll_id="scr-1",
            required_skill_ids=["skill_x", "skill_y"],
        )
        vault.get_scroll.return_value = SimpleNamespace(
            id="scr-1", intent="X", objectives=objectives,
        )
        vault.list_shadows.return_value = [
            {"id": "sh-A", "skill_ids": ["skill_x", "skill_y"]},
            {"id": "sh-B", "skill_ids": ["skill_x"]},                    # perfect but partial overlap
            {"id": "sh-C", "skill_ids": ["skill_x", "skill_y", "z"]},    # full overlap, neutral
        ]
        from systemu.runtime.supervisor import Supervisor
        sup = Supervisor.__new__(Supervisor)
        sup.vault = vault
        result = sup._resolve_shadow_with_affinity(
            activity_id="act-1", shadow_id="sh-A",
            exclude_shadow_id="sh-A", scroll_id="scr-1",
        )
        # sh-C wins on overlap (2 vs 1), beating sh-B despite the latter's perfect history.
        assert result == "sh-C"
