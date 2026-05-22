"""Tests for systemu.runtime.failure_telemetry.

v0.4.0-0 foundation. Validates:
  * record_* helpers append correctly-shaped JSONL rows
  * load_events skips malformed lines
  * compute_histogram groups + counts as expected
  * rotation triggers above MAX_BYTES
  * write failures never propagate to callers
  * success-status execution_terminal is filtered out
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from systemu.runtime import failure_telemetry as ft


# ─────────────────────────────────────────────────────────────────────────────
# Helpers

def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# Tool-failure recording

def test_record_tool_failure_writes_row(tmp_path, monkeypatch):
    target = tmp_path / "ft.jsonl"
    monkeypatch.setattr(ft, "_default_path", lambda: target)

    ft.record_tool_failure(
        shadow_id="sh-1",
        execution_id="exec-1",
        tool_name="create_word_doc",
        error_type="missing_dependency",
        error="docx module not installed",
        extra={"missing_packages": ["python-docx"]},
    )
    rows = _read(target)
    assert len(rows) == 1
    r = rows[0]
    assert r["event_type"] == "tool_failure"
    assert r["shadow_id"] == "sh-1"
    assert r["tool_name"] == "create_word_doc"
    assert r["error_type"] == "missing_dependency"
    assert r["context"]["missing_packages"] == ["python-docx"]


def test_record_tool_failure_caps_error_length(tmp_path, monkeypatch):
    target = tmp_path / "ft.jsonl"
    monkeypatch.setattr(ft, "_default_path", lambda: target)

    ft.record_tool_failure(
        shadow_id="sh-1",
        execution_id="exec-1",
        tool_name="t",
        error_type=None,
        error="A" * 5000,
    )
    rows = _read(target)
    assert len(rows[0]["error"]) <= 1000


# ─────────────────────────────────────────────────────────────────────────────
# Execution-terminal recording

def test_execution_terminal_writes_for_failure(tmp_path, monkeypatch):
    target = tmp_path / "ft.jsonl"
    monkeypatch.setattr(ft, "_default_path", lambda: target)

    ft.record_execution_terminal(
        shadow_id="sh-1",
        execution_id="exec-1",
        activity_id="act-1",
        scroll_id="scr-1",
        status="failure",
        iterations=27,
    )
    rows = _read(target)
    assert len(rows) == 1
    assert rows[0]["event_type"] == "execution_terminal"
    assert rows[0]["status"] == "failure"
    assert rows[0]["context"]["iterations"] == 27


def test_execution_terminal_skips_success(tmp_path, monkeypatch):
    """We only want failure-mode data; success doesn't belong in this file."""
    target = tmp_path / "ft.jsonl"
    monkeypatch.setattr(ft, "_default_path", lambda: target)

    ft.record_execution_terminal(
        shadow_id="sh-1",
        execution_id="exec-1",
        activity_id=None,
        scroll_id=None,
        status="success",
        iterations=5,
    )
    assert _read(target) == []


@pytest.mark.parametrize("status", ["failure", "partial", "cancelled", "stuck"])
def test_execution_terminal_records_all_failure_statuses(tmp_path, monkeypatch, status):
    target = tmp_path / "ft.jsonl"
    monkeypatch.setattr(ft, "_default_path", lambda: target)

    ft.record_execution_terminal(
        shadow_id="sh", execution_id="exec", activity_id=None, scroll_id=None,
        status=status, iterations=1,
    )
    rows = _read(target)
    assert len(rows) == 1
    assert rows[0]["status"] == status


# ─────────────────────────────────────────────────────────────────────────────
# Supervisor diagnosis

def test_supervisor_diagnosis_writes_row(tmp_path, monkeypatch):
    target = tmp_path / "ft.jsonl"
    monkeypatch.setattr(ft, "_default_path", lambda: target)

    ft.record_supervisor_diagnosis(
        shadow_id="sh-1",
        activity_id="act-1",
        diagnosis={
            "root_cause":        "Tool returned 404 because the URL was malformed.",
            "failure_category":  "tool_error",
            "immediate_fix":     "Validate URL before invoking the tool.",
            "retry_recommended": False,
            "prevention":        "Add URL-validation step to scroll.",
        },
    )
    rows = _read(target)
    assert len(rows) == 1
    r = rows[0]
    assert r["event_type"] == "supervisor_diagnosis"
    assert r["failure_category"] == "tool_error"
    assert r["context"]["retry_recommended"] is False


# ─────────────────────────────────────────────────────────────────────────────
# load_events resilience

def test_load_events_skips_malformed_lines(tmp_path, monkeypatch):
    target = tmp_path / "ft.jsonl"
    target.parent.mkdir(exist_ok=True)
    target.write_text(
        '{"ts":"x","event_type":"tool_failure"}\n'
        'this is not json\n'
        '\n'
        '{"ts":"y","event_type":"tool_failure","tool_name":"t"}\n',
        encoding="utf-8",
    )
    events = list(ft.load_events(target))
    assert len(events) == 2


def test_load_events_missing_file_returns_empty(tmp_path):
    assert list(ft.load_events(tmp_path / "absent.jsonl")) == []


# ─────────────────────────────────────────────────────────────────────────────
# compute_histogram

def test_compute_histogram_groups_and_sorts(tmp_path, monkeypatch):
    target = tmp_path / "ft.jsonl"
    monkeypatch.setattr(ft, "_default_path", lambda: target)

    # 3 missing_dependency, 1 param_error
    for _ in range(3):
        ft.record_tool_failure(
            shadow_id="sh", execution_id="e",
            tool_name="create_word_doc",
            error_type="missing_dependency",
            error="docx missing",
        )
    ft.record_tool_failure(
        shadow_id="sh", execution_id="e",
        tool_name="api_call",
        error_type="param_error",
        error="bad URL",
    )

    rows = ft.compute_histogram(
        group_by=("error_type", "tool_name"),
        path=target,
    )
    assert len(rows) == 2
    # Most frequent first
    assert rows[0]["error_type"] == "missing_dependency"
    assert rows[0]["count"] == 3
    assert rows[1]["count"] == 1


def test_compute_histogram_filters_event_types(tmp_path, monkeypatch):
    target = tmp_path / "ft.jsonl"
    monkeypatch.setattr(ft, "_default_path", lambda: target)

    ft.record_tool_failure(
        shadow_id="sh", execution_id="e",
        tool_name="t", error_type="x", error="e",
    )
    ft.record_execution_terminal(
        shadow_id="sh", execution_id="e", activity_id=None, scroll_id=None,
        status="failure", iterations=1,
    )

    rows = ft.compute_histogram(
        group_by=("event_type",),
        event_types=("tool_failure",),
        path=target,
    )
    assert len(rows) == 1
    assert rows[0]["event_type"] == "tool_failure"


# ─────────────────────────────────────────────────────────────────────────────
# Rotation + write resilience

def test_rotation_triggers_above_max_bytes(tmp_path, monkeypatch):
    target = tmp_path / "ft.jsonl"
    monkeypatch.setattr(ft, "_default_path", lambda: target)
    monkeypatch.setattr(ft, "_MAX_BYTES", 200)   # tiny ceiling for the test

    for _ in range(20):
        ft.record_tool_failure(
            shadow_id="shadow_" + "x" * 40,
            execution_id="exec",
            tool_name="t",
            error_type="missing_dependency",
            error="boom",
        )

    backup = target.with_suffix(target.suffix + ".1")
    assert backup.exists(), "rotation should have produced .1"
    # New file exists with at least one line written after rotation.
    assert target.exists()
    assert _read(target)


def test_record_swallows_io_errors(monkeypatch, tmp_path):
    """A telemetry write failure must NEVER propagate to the caller."""
    # Point telemetry at a path whose parent is a regular file (can't mkdir).
    blocker = tmp_path / "regular_file"
    blocker.write_text("blocked")
    bad_path = blocker / "ft.jsonl"
    monkeypatch.setattr(ft, "_default_path", lambda: bad_path)
    # Must NOT raise.
    ft.record_tool_failure(
        shadow_id=None, execution_id=None,
        tool_name="t", error_type="x", error="e",
    )
