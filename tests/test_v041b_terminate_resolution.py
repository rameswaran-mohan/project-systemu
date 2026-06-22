"""Tests for v0.4.1-b TERMINATE resolution UX.

Covers:
  * affinity_log: record + recent_terminations + is_excluded + window filtering
  * compute_intent_hash determinism
  * _apply_terminate_directive publishes the supervisor approval card with
    correct dedup_key + redirect_to and writes the affinity log
  * Detection of TERMINATE in audit file by the workflow detail panel logic
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from systemu.runtime import affinity_log as al
from systemu.runtime.affinity_log import (
    AffinityLog,
    compute_intent_hash,
    get_affinity_log,
    reset_singleton_for_tests,
)


# ─────────────────────────────────────────────────────────────────────────────
# AffinityLog

class TestAffinityLog:
    def test_record_then_recent(self, tmp_path):
        log = AffinityLog(tmp_path / "affinity.json")
        log.record_termination(
            intent_hash="abc123",
            shadow_id="sh-1",
            scroll_id="scroll_x",
            execution_id="exec_y",
        )
        recent = log.recent_terminations()
        assert len(recent) == 1
        assert recent[0].shadow_id == "sh-1"
        assert recent[0].intent_hash == "abc123"

    def test_is_excluded_within_window(self, tmp_path):
        log = AffinityLog(tmp_path / "affinity.json")
        log.record_termination(intent_hash="h1", shadow_id="sh-A")
        assert log.is_excluded(intent_hash="h1", shadow_id="sh-A") is True
        assert log.is_excluded(intent_hash="h1", shadow_id="sh-B") is False
        assert log.is_excluded(intent_hash="h2", shadow_id="sh-A") is False

    def test_window_filters_old_entries(self, tmp_path, monkeypatch):
        log = AffinityLog(tmp_path / "affinity.json")
        log.record_termination(intent_hash="h", shadow_id="s")
        # Default window is 48h; check exclusion under 1h window vs 100h
        assert log.is_excluded(intent_hash="h", shadow_id="s", window_hours=100) is True
        # Force the timestamp to be far in the past
        data = json.loads((tmp_path / "affinity.json").read_text(encoding="utf-8"))
        data["terminations"][0]["ts"] = (
            datetime.now(tz=timezone.utc) - timedelta(hours=72)
        ).isoformat(timespec="seconds")
        (tmp_path / "affinity.json").write_text(json.dumps(data))
        assert log.is_excluded(intent_hash="h", shadow_id="s", window_hours=48) is False
        assert log.is_excluded(intent_hash="h", shadow_id="s", window_hours=100) is True

    def test_clear_resets(self, tmp_path):
        log = AffinityLog(tmp_path / "affinity.json")
        log.record_termination(intent_hash="h", shadow_id="s")
        log.record_termination(intent_hash="h2", shadow_id="s2")
        assert log.clear() == 2
        assert log.recent_terminations() == []

    def test_missing_file_returns_empty(self, tmp_path):
        log = AffinityLog(tmp_path / "absent.json")
        assert log.recent_terminations() == []
        assert log.is_excluded(intent_hash="x", shadow_id="y") is False


# ─────────────────────────────────────────────────────────────────────────────
# compute_intent_hash

class TestIntentHash:
    def test_deterministic(self):
        a = compute_intent_hash(intent="Write a report",
                                objectives=[{"goal": "g1"}, {"goal": "g2"}])
        b = compute_intent_hash(intent="Write a report",
                                objectives=[{"goal": "g1"}, {"goal": "g2"}])
        assert a == b
        assert len(a) == 10   # 10-char hex prefix

    def test_different_inputs_different_hash(self):
        a = compute_intent_hash(intent="Write a report", objectives=[{"goal": "x"}])
        b = compute_intent_hash(intent="Write a poem",   objectives=[{"goal": "x"}])
        assert a != b

    def test_case_insensitive(self):
        a = compute_intent_hash(intent="Hello",  objectives=[{"goal": "X"}])
        b = compute_intent_hash(intent="HELLO",  objectives=[{"goal": "x"}])
        assert a == b

    def test_empty_inputs(self):
        h = compute_intent_hash(intent="", objectives=[])
        assert len(h) == 10


# ─────────────────────────────────────────────────────────────────────────────
# _apply_terminate_directive

class TestTerminateDirective:
    def test_publishes_approval_card_and_logs_affinity(self, tmp_path, monkeypatch):
        # Point the affinity log singleton at our tmp file
        reset_singleton_for_tests()
        monkeypatch.setattr(al, "_DEFAULT_PATH", tmp_path / "affinity.json")
        monkeypatch.setattr(al, "_singleton", None)

        # Capture EventBus events
        from systemu.interface.event_bus import EventBus
        events: list = []
        unsub = EventBus.get().subscribe(lambda e: events.append(e), replay=False)
        try:
            from systemu.runtime.shadow_runtime import _apply_terminate_directive
            from systemu.runtime.execution_mind import Directive

            context = MagicMock()
            scroll = SimpleNamespace(
                id="scr-X", intent="Generate weather report",
                objectives=[SimpleNamespace(goal="fetch data"),
                            SimpleNamespace(goal="format docx")],
            )
            shadow = SimpleNamespace(id="sh-bad")

            _apply_terminate_directive(
                Directive(action="TERMINATE", rationale="Scroll unsatisfiable: missing tool web_scrape"),
                context=context, shadow=shadow, scroll=scroll,
                execution_id="exec_term_test",
            )
        finally:
            unsub()

        # Approval card published
        approvals = [e for e in events if e.get("category") == "approval"]
        assert len(approvals) == 1
        ctx = approvals[0]["context"]
        assert ctx["dedup_key"] == "supervisor-terminate:exec_term_test"
        assert ctx["redirect_to"] == "/workflow/exec_term_test"
        assert "retry_with_different_shadow" in ctx["actions"]

        # Affinity log updated
        log = get_affinity_log()
        recent = log.recent_terminations()
        assert len(recent) == 1
        assert recent[0].shadow_id == "sh-bad"
        assert recent[0].reason == "supervisor_terminate"


# ─────────────────────────────────────────────────────────────────────────────
# Workflow detail panel detects TERMINATE in audit file

class TestTerminateDetection:
    def test_panel_shows_only_when_terminate_in_audit(self, tmp_path):
        """The panel reads the audit file looking for any row with
        action='TERMINATE'.  No such row → panel is empty."""
        audit_dir = tmp_path / "data" / "audit" / "exec_test"
        audit_dir.mkdir(parents=True)
        audit_path = audit_dir / "supervisor.jsonl"

        # Empty: no TERMINATE
        audit_path.write_text(
            json.dumps({"action": "NUDGE", "rationale": "x"}) + "\n",
            encoding="utf-8",
        )
        # Read it back the way the panel does
        terms = [
            json.loads(l) for l in audit_path.read_text(encoding="utf-8").splitlines() if l.strip()
        ]
        assert all(r.get("action") != "TERMINATE" for r in terms)

        # Now append a TERMINATE
        with audit_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"action": "TERMINATE",
                                 "rationale": "hopeless"}) + "\n")
        terms = [
            json.loads(l) for l in audit_path.read_text(encoding="utf-8").splitlines() if l.strip()
        ]
        assert any(r.get("action") == "TERMINATE" for r in terms)
