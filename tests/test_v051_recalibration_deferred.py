"""Tests for v0.5.1 — deferred recalibration items.

  v0.5.1-a Override actions  — exercised at the recalibrator helper level
  v0.5.1-b Spec diff          — compute_spec_diff
  v0.5.1-c Auto-approve risk  — is_low_risk_recalibration
  v0.5.1-d Cross-shadow tracker — InadequacyTracker
  v0.5.1-e Snapshot resume    — write/read/apply
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from systemu.core.models import Tool, ToolStatus, ToolType
from systemu.pipelines import tool_recalibrator as tr
from systemu.pipelines.tool_inadequacy_diagnosis import InadequacyDiagnosis


# ─────────────────────────────────────────────────────────────────────────────
# v0.5.1-b — compute_spec_diff

class TestSpecDiff:
    def test_no_changes_returns_empty(self):
        spec_a = {"description": "x", "parameters_schema": {"a": 1}}
        diff = tr.compute_spec_diff(spec_a, spec_a)
        assert diff == []

    def test_field_change_appears(self):
        old = {"description": "old"}
        new = {"description": "new"}
        diff = tr.compute_spec_diff(old, new)
        assert len(diff) == 1
        assert diff[0]["field"] == "description"
        assert diff[0]["old"] == "old"
        assert diff[0]["new"] == "new"

    def test_dict_field_renders_as_json(self):
        old = {"parameters_schema": {"x": {"type": "string"}}}
        new = {"parameters_schema": {"x": {"type": "string"},
                                      "y": {"type": "integer"}}}
        diff = tr.compute_spec_diff(old, new)
        assert len(diff) == 1
        assert "y" in diff[0]["new"]

    def test_truncation_at_200(self):
        old = {"description": "x" * 500}
        new = {"description": "y" * 500}
        diff = tr.compute_spec_diff(old, new)
        assert len(diff[0]["old"]) <= 201   # 200 + ellipsis
        assert len(diff[0]["new"]) <= 201

    def test_only_supplied_fields_compared(self):
        old = {"description": "a", "irrelevant_extra": "p"}
        new = {"description": "a", "irrelevant_extra": "q"}
        diff = tr.compute_spec_diff(old, new)
        assert diff == []   # irrelevant_extra not in default fields tuple


# ─────────────────────────────────────────────────────────────────────────────
# v0.5.1-c — is_low_risk_recalibration

class TestRiskClassifier:
    def _result(self, **kw):
        defaults = dict(
            success=True, mode="fork_new_tool",
            original_tool_id="tool_x", new_tool_id="tool_y",
            new_tool_name="tool_y",
            dry_run_status="passed", forced_fallback=False,
        )
        defaults.update(kw)
        return tr.RecalibrationResult(**defaults)

    def _diagnosis(self, confidence="high"):
        return InadequacyDiagnosis(
            is_inadequate=True, recalibration_mode="fork_new_tool",
            rationale="x", confidence=confidence,
        )

    def _tool(self, name="t"):
        return Tool(
            id="tool_x", name=name, description="t",
            tool_type=ToolType.PYTHON_FUNCTION,
            status=ToolStatus.DEPLOYED, enabled=True,
        )

    def test_happy_path_eligible(self):
        ok, reason = tr.is_low_risk_recalibration(
            result=self._result(), tool=self._tool(), diagnosis=self._diagnosis(),
        )
        assert ok is True
        assert "fork-mode" in reason

    def test_failed_recalibration_blocked(self):
        ok, reason = tr.is_low_risk_recalibration(
            result=self._result(success=False),
            tool=self._tool(), diagnosis=self._diagnosis(),
        )
        assert ok is False
        assert "did not succeed" in reason

    def test_bump_mode_blocked(self):
        ok, reason = tr.is_low_risk_recalibration(
            result=self._result(mode="bump_version"),
            tool=self._tool(), diagnosis=self._diagnosis(),
        )
        assert ok is False
        assert "bump_version" in reason

    def test_dry_run_skipped_blocked(self):
        ok, reason = tr.is_low_risk_recalibration(
            result=self._result(dry_run_status="skipped"),
            tool=self._tool(), diagnosis=self._diagnosis(),
        )
        assert ok is False
        assert "dry-run status" in reason

    def test_fallback_blocked(self):
        ok, reason = tr.is_low_risk_recalibration(
            result=self._result(forced_fallback=True),
            tool=self._tool(), diagnosis=self._diagnosis(),
        )
        assert ok is False
        assert "fallback" in reason

    def test_destructive_tool_blocked(self):
        ok, reason = tr.is_low_risk_recalibration(
            result=self._result(),
            tool=self._tool(name="delete_user_account"),
            diagnosis=self._diagnosis(),
        )
        assert ok is False
        assert "destructive" in reason

    def test_low_confidence_blocked(self):
        ok, reason = tr.is_low_risk_recalibration(
            result=self._result(),
            tool=self._tool(),
            diagnosis=self._diagnosis(confidence="medium"),
        )
        assert ok is False
        assert "confidence" in reason


# ─────────────────────────────────────────────────────────────────────────────
# v0.5.1-d — InadequacyTracker

class TestInadequacyTracker:
    def test_flag_then_query(self, tmp_path):
        from systemu.runtime.inadequacy_tracker import InadequacyTracker
        t = InadequacyTracker(tmp_path / "tracker.json")
        t.flag(tool_id="tool_x", shadow_id="sh-A", execution_id="exec-1")
        sig = t.cluster_signal_for("tool_x")
        assert sig.distinct_shadows == 1
        assert sig.is_cluster is False

    def test_cluster_signal_when_three_shadows(self, tmp_path):
        from systemu.runtime.inadequacy_tracker import InadequacyTracker
        t = InadequacyTracker(tmp_path / "tracker.json")
        for s in ("sh-A", "sh-B", "sh-C"):
            t.flag(tool_id="tool_x", shadow_id=s, execution_id=f"exec-{s}")
        sig = t.cluster_signal_for("tool_x")
        assert sig.distinct_shadows == 3
        assert sig.is_cluster is True

    def test_dedup_per_execution(self, tmp_path):
        from systemu.runtime.inadequacy_tracker import InadequacyTracker
        t = InadequacyTracker(tmp_path / "tracker.json")
        for _ in range(5):
            t.flag(tool_id="tool_x", shadow_id="sh-A", execution_id="exec-1")
        sig = t.cluster_signal_for("tool_x")
        assert sig.total_flags == 1   # 4 dupes dropped

    def test_window_filter(self, tmp_path):
        from systemu.runtime.inadequacy_tracker import InadequacyTracker
        t = InadequacyTracker(tmp_path / "tracker.json")
        t.flag(tool_id="tool_x", shadow_id="sh-A", execution_id="exec-1")
        # Force ts far in past
        data = json.loads((tmp_path / "tracker.json").read_text())
        from datetime import datetime, timedelta, timezone
        old = (datetime.now(tz=timezone.utc) - timedelta(hours=48)).isoformat(timespec="seconds")
        data["rows"]["tool_x"]["flags"][0]["ts"] = old
        (tmp_path / "tracker.json").write_text(json.dumps(data))
        # 24h window: should drop the entry
        sig = t.cluster_signal_for("tool_x", window_hours=24)
        assert sig.total_flags == 0
        # 72h window: still present
        sig = t.cluster_signal_for("tool_x", window_hours=72)
        assert sig.total_flags == 1

    def test_clear(self, tmp_path):
        from systemu.runtime.inadequacy_tracker import InadequacyTracker
        t = InadequacyTracker(tmp_path / "tracker.json")
        t.flag(tool_id="a", shadow_id="s", execution_id="e")
        t.flag(tool_id="b", shadow_id="s", execution_id="e")
        assert t.clear() == 2


# ─────────────────────────────────────────────────────────────────────────────
# v0.5.1-e — execution snapshot persistence

@pytest.fixture
def context_for_snapshot(tmp_path):
    from systemu.runtime.context_builder import ExecutionContext
    return ExecutionContext(
        execution_id="exec_snap",
        system_prompt="t",
        scroll_json=[],
        tool_index=[],
        skill_index=[],
        snapshot_dir=tmp_path / "snap",
        recalled_memory="",
        use_objectives=True,
        scroll_intent="t",
    )


class TestSnapshotPersistence:
    def test_round_trip(self, tmp_path, context_for_snapshot):
        from systemu.runtime.execution_snapshot import (
            ExecutionSnapshot, capture_from_context, read_snapshot, write_snapshot,
        )
        ctx = context_for_snapshot
        ctx.add_sticky_note("note one")
        ctx.add_sticky_note("note two")
        snap = capture_from_context(
            execution_id="exec_x", shadow_id="sh-1", scroll_id="scr-1",
            iteration=7, current_action_block=3,
            completed_objectives={1, 2}, context=ctx,
            original_tool_id="tool_y",
        )
        path = write_snapshot(snap, data_dir=tmp_path)
        assert path is not None
        assert path.exists()

        loaded = read_snapshot("exec_x", data_dir=tmp_path)
        assert loaded is not None
        assert loaded.iteration == 7
        assert loaded.completed_objective_ids == [1, 2]
        assert "note one" in loaded.sticky_notes

    def test_apply_to_context_restores_sticky_notes(self, tmp_path, context_for_snapshot):
        from systemu.runtime.execution_snapshot import ExecutionSnapshot, apply_to_context
        snap = ExecutionSnapshot(
            execution_id="exec_x", shadow_id="sh", scroll_id="scr",
            iteration=5, sticky_notes=["resumed:keep this"],
            completed_objective_ids=[1, 2, 3],
        )
        apply_to_context(snap, context=context_for_snapshot)
        assert "resumed:keep this" in context_for_snapshot.get_sticky_notes()

    def test_apply_queues_resume_reflection_block(self, tmp_path, context_for_snapshot):
        from systemu.runtime.execution_snapshot import ExecutionSnapshot, apply_to_context
        snap = ExecutionSnapshot(
            execution_id="exec_x", shadow_id="sh", scroll_id="scr",
            iteration=5,
            completed_objective_ids=[1, 2],
            recent_history_slice=[
                {"role": "tool_call", "tool": "create_word_doc",
                 "params": {"filename": "x.docx"}},
                {"role": "tool_result", "result": {"success": False, "error": "boom"}},
            ],
        )
        apply_to_context(snap, context=context_for_snapshot)
        # build_messages consumes the pending reflection block
        msgs = context_for_snapshot.build_messages(current_action_block=1)
        sys_msg = msgs[0]["content"]
        assert "Resumed from recalibration snapshot" in sys_msg
        assert "[1, 2]" in sys_msg
        assert "create_word_doc" in sys_msg

    def test_missing_snapshot_returns_none(self, tmp_path):
        from systemu.runtime.execution_snapshot import read_snapshot
        assert read_snapshot("never-existed", data_dir=tmp_path) is None

    def test_delete_snapshot(self, tmp_path):
        from systemu.runtime.execution_snapshot import (
            ExecutionSnapshot, delete_snapshot, write_snapshot,
        )
        snap = ExecutionSnapshot(
            execution_id="exec_x", shadow_id="s", scroll_id="sc", iteration=0,
        )
        write_snapshot(snap, data_dir=tmp_path)
        assert delete_snapshot("exec_x", data_dir=tmp_path) is True
        # Second delete is a no-op
        assert delete_snapshot("exec_x", data_dir=tmp_path) is False


# ─────────────────────────────────────────────────────────────────────────────
# v0.5.1-c (config knob round-trip)

class TestConfigKnob:
    def test_default_off(self, monkeypatch):
        monkeypatch.delenv("SYSTEMU_AUTO_APPROVE_LOW_RISK_RECAL", raising=False)
        from sharing_on.config import Config
        c = Config.from_env()
        assert c.auto_approve_low_risk_recalibrations is False

    def test_env_on(self, monkeypatch):
        monkeypatch.setenv("SYSTEMU_AUTO_APPROVE_LOW_RISK_RECAL", "true")
        from sharing_on.config import Config
        c = Config.from_env()
        assert c.auto_approve_low_risk_recalibrations is True
