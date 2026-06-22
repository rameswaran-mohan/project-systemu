"""Tests for Plan 0 / Build 1 / Task 1.7 — paper-readiness pull metrics.

Covers the NON-BREAKING additions:

  * ShadowMetrics: per-entry harness_runs / harness_successes counters,
    recorded via record_harness_run(used_harness=...), plus a
    with-harness vs without-harness success-rate helper.
  * failure_classifier: three new pull-specific categories
    (premature_request, wasted_request, unused_grant) and a new
    classify_pull_failure() function — without disturbing the existing
    classify_tool_result() / CATEGORIES contract.
"""
from __future__ import annotations

import pytest

from systemu.runtime import failure_classifier as fc
from systemu.runtime.shadow_metrics import (
    MetricEntry, ShadowMetrics, reset_singleton_for_tests,
)


@pytest.fixture(autouse=True)
def _reset():
    reset_singleton_for_tests()
    yield
    reset_singleton_for_tests()


# ─────────────────────────────────────────────────────────────────────────────
# ShadowMetrics harness slice

class TestHarnessSlice:
    def test_used_harness_success_increments_both(self, tmp_path):
        store = ShadowMetrics(tmp_path / "m.json")
        store.record_harness_run(
            shadow_id="sh-A", intent_hash="h1",
            used_harness=True, success=True,
        )
        e = store.get(shadow_id="sh-A", intent_hash="h1")
        assert e.harness_runs == 1
        assert e.harness_successes == 1

    def test_used_harness_failure_increments_runs_only(self, tmp_path):
        store = ShadowMetrics(tmp_path / "m.json")
        store.record_harness_run(
            shadow_id="sh-A", intent_hash="h1",
            used_harness=True, success=False,
        )
        e = store.get(shadow_id="sh-A", intent_hash="h1")
        assert e.harness_runs == 1
        assert e.harness_successes == 0

    def test_without_harness_touches_neither_harness_counter(self, tmp_path):
        store = ShadowMetrics(tmp_path / "m.json")
        store.record_harness_run(
            shadow_id="sh-A", intent_hash="h1",
            used_harness=False, success=True,
        )
        e = store.get(shadow_id="sh-A", intent_hash="h1")
        assert e.harness_runs == 0
        assert e.harness_successes == 0

    def test_harness_counters_default_zero(self, tmp_path):
        store = ShadowMetrics(tmp_path / "m.json")
        store.record(shadow_id="sh-A", intent_hash="h1", status="success")
        e = store.get(shadow_id="sh-A", intent_hash="h1")
        assert e.harness_runs == 0
        assert e.harness_successes == 0

    def test_unknown_pair_has_zero_harness_counters(self, tmp_path):
        store = ShadowMetrics(tmp_path / "m.json")
        e = store.get(shadow_id="nope", intent_hash="nope")
        assert e.harness_runs == 0
        assert e.harness_successes == 0

    def test_record_harness_also_tracks_overall_outcome(self, tmp_path):
        """A harness run is still a real execution — it should feed the
        normal executions/successes counters too."""
        store = ShadowMetrics(tmp_path / "m.json")
        store.record_harness_run(
            shadow_id="sh-A", intent_hash="h1",
            used_harness=True, success=True,
        )
        e = store.get(shadow_id="sh-A", intent_hash="h1")
        assert e.executions == 1
        assert e.successes == 1

    def test_persistence_round_trip(self, tmp_path):
        path = tmp_path / "m.json"
        a = ShadowMetrics(path)
        a.record_harness_run(
            shadow_id="sh-A", intent_hash="h1",
            used_harness=True, success=True,
        )
        a.record_harness_run(
            shadow_id="sh-A", intent_hash="h1",
            used_harness=True, success=False,
        )
        b = ShadowMetrics(path)
        e = b.get(shadow_id="sh-A", intent_hash="h1")
        assert e.harness_runs == 2
        assert e.harness_successes == 1

    def test_empty_ids_skipped(self, tmp_path):
        store = ShadowMetrics(tmp_path / "m.json")
        store.record_harness_run(
            shadow_id="", intent_hash="h1", used_harness=True, success=True,
        )
        assert store.get(shadow_id="", intent_hash="h1").harness_runs == 0


# ─────────────────────────────────────────────────────────────────────────────
# with-harness vs without-harness success-rate helper

class TestHarnessRates:
    def test_ratio_split(self, tmp_path):
        store = ShadowMetrics(tmp_path / "m.json")
        # 4 harness runs, 3 successes → with-harness rate = 0.75
        store.record_harness_run(shadow_id="s", intent_hash="h", used_harness=True, success=True)
        store.record_harness_run(shadow_id="s", intent_hash="h", used_harness=True, success=True)
        store.record_harness_run(shadow_id="s", intent_hash="h", used_harness=True, success=True)
        store.record_harness_run(shadow_id="s", intent_hash="h", used_harness=True, success=False)
        # 2 non-harness runs, 1 success → without-harness rate = 0.5
        store.record_harness_run(shadow_id="s", intent_hash="h", used_harness=False, success=True)
        store.record_harness_run(shadow_id="s", intent_hash="h", used_harness=False, success=False)

        rates = store.harness_success_rates(shadow_id="s", intent_hash="h")
        assert abs(rates["with_harness"] - 0.75) < 1e-9
        assert abs(rates["without_harness"] - 0.5) < 1e-9

    def test_neutral_when_no_data(self, tmp_path):
        store = ShadowMetrics(tmp_path / "m.json")
        rates = store.harness_success_rates(shadow_id="cold", intent_hash="h")
        assert rates["with_harness"] == 0.5
        assert rates["without_harness"] == 0.5


# ─────────────────────────────────────────────────────────────────────────────
# failure_classifier — new pull categories

class TestPullCategoriesPresent:
    def test_new_categories_in_tuple(self):
        for cat in ("premature_request", "wasted_request", "unused_grant"):
            assert cat in fc.CATEGORIES

    def test_existing_categories_preserved(self):
        for cat in (
            "missing_dependency", "param_error", "timeout", "http_error",
            "network_error", "permission_error", "file_not_found",
            "parse_error", "api_error", "tool_inadequate", "unknown",
        ):
            assert cat in fc.CATEGORIES


class TestClassifyPullFailure:
    def test_premature_request(self):
        out = fc.classify_pull_failure(
            attempts_before=0, decision="request",
            fallback_ok=None, used_after_grant=None,
        )
        assert out == "premature_request"

    def test_wasted_request(self):
        out = fc.classify_pull_failure(
            attempts_before=3, decision="deny",
            fallback_ok=True, used_after_grant=None,
        )
        assert out == "wasted_request"

    def test_unused_grant(self):
        out = fc.classify_pull_failure(
            attempts_before=3, decision="grant",
            fallback_ok=None, used_after_grant=False,
        )
        assert out == "unused_grant"

    def test_unknown_fallthrough(self):
        # Plenty of attempts, granted, and used → nothing wrong.
        out = fc.classify_pull_failure(
            attempts_before=3, decision="grant",
            fallback_ok=None, used_after_grant=True,
        )
        assert out == "unknown"

    def test_deny_without_fallback_is_not_wasted(self):
        out = fc.classify_pull_failure(
            attempts_before=3, decision="deny",
            fallback_ok=False, used_after_grant=None,
        )
        assert out == "unknown"

    def test_grant_used_is_not_unused(self):
        out = fc.classify_pull_failure(
            attempts_before=3, decision="grant",
            fallback_ok=None, used_after_grant=True,
        )
        assert out == "unknown"

    def test_every_result_is_a_valid_category(self):
        for out in (
            fc.classify_pull_failure(attempts_before=0, decision="request",
                                     fallback_ok=None, used_after_grant=None),
            fc.classify_pull_failure(attempts_before=3, decision="deny",
                                     fallback_ok=True, used_after_grant=None),
            fc.classify_pull_failure(attempts_before=3, decision="grant",
                                     fallback_ok=None, used_after_grant=False),
            fc.classify_pull_failure(attempts_before=3, decision="grant",
                                     fallback_ok=None, used_after_grant=True),
        ):
            assert out in fc.CATEGORIES
