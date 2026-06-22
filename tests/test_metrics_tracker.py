"""Tests for the data flywheel metrics tracker."""
import json
import pytest
from pathlib import Path

from systemu.runtime.metrics_tracker import (
    record_execution, load_metrics, load_all_metrics,
    METRICS_FILENAME,
)


@pytest.fixture
def shadow_dir(tmp_path):
    d = tmp_path / "shadow_shadow_test"
    d.mkdir()
    return d


class TestRecordExecution:
    def test_creates_metrics_file(self, shadow_dir):
        record_execution("shadow_test", "TestShadow", shadow_dir, "exec_001", "success", 8, 4, 2, 2, 30.0)
        assert (shadow_dir / METRICS_FILENAME).exists()

    def test_basic_fields(self, shadow_dir):
        record_execution("shadow_test", "TestShadow", shadow_dir, "exec_001", "success", 8, 4, 2, 2, 30.0)
        m = load_metrics(shadow_dir)
        assert m["shadow_id"] == "shadow_test"
        assert m["shadow_name"] == "TestShadow"
        assert m["total_executions"] == 1
        assert m["success_count"] == 1
        assert m["failure_count"] == 0
        assert m["success_rate"] == 100.0

    def test_three_runs_aggregate(self, shadow_dir):
        record_execution("s", "Shadow", shadow_dir, "e1", "success", 10, 5, 2, 2, 30.0)
        record_execution("s", "Shadow", shadow_dir, "e2", "failure", 15, 6, 1, 2, 20.0)
        record_execution("s", "Shadow", shadow_dir, "e3", "success", 8,  4, 2, 2, 25.0)
        m = load_metrics(shadow_dir)
        assert m["total_executions"] == 3
        assert m["success_count"] == 2
        assert m["failure_count"] == 1
        assert round(m["success_rate"], 1) == 66.7
        assert round(m["avg_iterations"], 1) == 11.0  # (10+15+8)/3

    def test_execution_history_recorded(self, shadow_dir):
        record_execution("s", "S", shadow_dir, "exec_abc", "success", 7, 3, 1, 1, 15.0)
        m = load_metrics(shadow_dir)
        execs = m["executions"]
        assert len(execs) == 1
        assert execs[0]["execution_id"] == "exec_abc"
        assert execs[0]["status"] == "success"
        assert execs[0]["iterations"] == 7
        assert execs[0]["tool_calls"] == 3

    def test_max_history_capped(self, shadow_dir):
        for i in range(110):
            record_execution("s", "S", shadow_dir, f"exec_{i:03d}", "success", 5, 2, 1, 1, 10.0)
        m = load_metrics(shadow_dir)
        assert len(m["executions"]) == 100  # MAX_EXEC_HISTORY

    def test_objective_completion_rate(self, shadow_dir):
        record_execution("s", "S", shadow_dir, "e1", "success", 8, 4, 2, 2, 20.0)
        record_execution("s", "S", shadow_dir, "e2", "partial", 12, 5, 1, 2, 18.0)
        m = load_metrics(shadow_dir)
        # (2/2 + 1/2) / 2 = 0.75 = 75%
        assert m["objectives_completed_rate"] == 75.0

    def test_no_objectives_rate_zero(self, shadow_dir):
        record_execution("s", "S", shadow_dir, "e1", "failure", 5, 2, 0, 0, 10.0)
        m = load_metrics(shadow_dir)
        assert m["objectives_completed_rate"] == 0.0

    def test_atomic_write_no_corruption(self, shadow_dir):
        for i in range(20):
            record_execution("s", "S", shadow_dir, f"e{i}", "success", 5, 2, 1, 1, 10.0)
        # File should be valid JSON
        raw = (shadow_dir / METRICS_FILENAME).read_text()
        data = json.loads(raw)
        assert data["total_executions"] == 20


class TestLoadMetrics:
    def test_missing_dir_returns_empty(self, tmp_path):
        m = load_metrics(tmp_path / "nonexistent")
        assert m == {}

    def test_missing_metrics_file_returns_empty(self, tmp_path):
        d = tmp_path / "shadow_dir"
        d.mkdir()
        m = load_metrics(d)
        assert m == {}

    def test_corrupted_file_returns_empty(self, shadow_dir):
        (shadow_dir / METRICS_FILENAME).write_text("not json {{{")
        m = load_metrics(shadow_dir)
        assert m == {}


class TestLoadAllMetrics:
    def test_empty_vault(self, tmp_path):
        (tmp_path / "shadow_army").mkdir()
        result = load_all_metrics(str(tmp_path))
        assert result == []

    def test_multiple_shadows(self, tmp_path):
        army = tmp_path / "shadow_army"
        army.mkdir()
        for i in range(3):
            d = army / f"shadow_shadow_{i:03d}"
            d.mkdir()
            record_execution(f"shadow_{i}", f"Shadow{i}", d, "e1", "success", 5, 2, 1, 1, 10.0)
        results = load_all_metrics(str(tmp_path))
        assert len(results) == 3
        names = {r["shadow_name"] for r in results}
        assert "Shadow0" in names
        assert "Shadow2" in names

    def test_shadows_without_metrics_excluded(self, tmp_path):
        army = tmp_path / "shadow_army"
        army.mkdir()
        # Shadow with metrics
        d1 = army / "shadow_shadow_with"
        d1.mkdir()
        record_execution("s1", "WithMetrics", d1, "e1", "success", 5, 2, 1, 1, 10.0)
        # Shadow without metrics (empty dir)
        (army / "shadow_shadow_without").mkdir()
        results = load_all_metrics(str(tmp_path))
        assert len(results) == 1
        assert results[0]["shadow_name"] == "WithMetrics"
