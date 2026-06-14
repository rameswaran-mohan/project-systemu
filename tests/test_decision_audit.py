"""Tests for systemu.runtime.decision_audit (Plan 0 Build 1 Task 1.3).

One row per loop iteration recorded to
``{vault_root}/executions/{execution_id}/decision_audit.jsonl``.

Validates:
  * append two rows then read → ordered, fields preserved
  * a REQUEST_HARNESS row carries harness_kind / confidence / attempts
  * missing file → []
  * a write to a bad path does not raise (best-effort/never-raise)
"""
from __future__ import annotations

from pathlib import Path

from systemu.runtime import decision_audit as da
from systemu.runtime.decision_audit import IterationDecision


def _mk(execution_id: str, iteration: int, **over) -> IterationDecision:
    base = dict(
        execution_id=execution_id,
        iteration=iteration,
        action="THINK",
        reasoning="reasoning text",
        consecutive_thinks=0,
        loop_guard_active=False,
        loop_guard_message=None,
        stuck_round_count=0,
        consec_research_reads=0,
        consec_tool_failures=0,
        is_request_harness=False,
        harness_request_id=None,
        harness_kind=None,
        harness_confidence=None,
        harness_attempts_before=None,
    )
    base.update(over)
    return IterationDecision(**base)


def test_append_two_rows_then_read_ordered_fields_preserved(tmp_path):
    exec_id = "exec_abc"
    row0 = _mk(exec_id, 0, action="THINK", reasoning="first", consecutive_thinks=1)
    row1 = _mk(
        exec_id, 1,
        action="TOOL_CALL", reasoning="second",
        loop_guard_active=True, loop_guard_message="guard tripped",
        stuck_round_count=2, consec_research_reads=3, consec_tool_failures=1,
    )

    da.append_iteration_decision(tmp_path, exec_id, row0)
    da.append_iteration_decision(tmp_path, exec_id, row1)

    rows = da.read_iteration_decisions(tmp_path, exec_id)
    assert len(rows) == 2
    # ordered
    assert rows[0]["iteration"] == 0
    assert rows[1]["iteration"] == 1
    # fields preserved
    assert rows[0]["action"] == "THINK"
    assert rows[0]["reasoning"] == "first"
    assert rows[0]["consecutive_thinks"] == 1
    assert rows[1]["action"] == "TOOL_CALL"
    assert rows[1]["loop_guard_active"] is True
    assert rows[1]["loop_guard_message"] == "guard tripped"
    assert rows[1]["stuck_round_count"] == 2
    assert rows[1]["consec_research_reads"] == 3
    assert rows[1]["consec_tool_failures"] == 1
    # ts present + iso-ish
    assert rows[0]["ts"]
    assert "T" in rows[0]["ts"]

    # file lives at the documented path
    p = Path(tmp_path) / "executions" / exec_id / "decision_audit.jsonl"
    assert p.exists()


def test_request_harness_row_carries_kind_confidence_attempts(tmp_path):
    exec_id = "exec_harness"
    row = _mk(
        exec_id, 4,
        action="REQUEST_HARNESS",
        reasoning="need a tool",
        is_request_harness=True,
        harness_request_id="hreq_1234",
        harness_kind="TOOL",
        harness_confidence=0.82,
        harness_attempts_before=3,
    )
    da.append_iteration_decision(tmp_path, exec_id, row)

    rows = da.read_iteration_decisions(tmp_path, exec_id)
    assert len(rows) == 1
    r = rows[0]
    assert r["is_request_harness"] is True
    assert r["harness_request_id"] == "hreq_1234"
    assert r["harness_kind"] == "TOOL"
    assert r["harness_confidence"] == 0.82
    assert r["harness_attempts_before"] == 3


def test_missing_file_returns_empty(tmp_path):
    assert da.read_iteration_decisions(tmp_path, "nonexistent_exec") == []


def test_write_to_bad_path_does_not_raise(tmp_path):
    # Point vault_root at a path whose parent is a *file*, so mkdir/open fail.
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a dir", encoding="utf-8")
    bad_vault = blocker / "vault"  # parent is a regular file → cannot mkdir under it

    row = _mk("exec_bad", 0)
    # must not raise
    da.append_iteration_decision(bad_vault, "exec_bad", row)
    # and reading a path that can't exist returns []
    assert da.read_iteration_decisions(bad_vault, "exec_bad") == []
