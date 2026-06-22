"""W5.3 — the right-rail Live pane: per-event headers + outcomes + decisions.

The Live pane rendered flat `[LEVEL] message` labels: pending-decision events
(no top-level message) drew literally "[INFO] " blank lines, task outcomes
were never published on the sync path, and nothing was expandable or
actionable. Pins the pure row model, the self-describing decision events, the
log_event details pass-through, and the outcome publish.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from systemu.interface.components.right_rail import (
    format_live_run_line, live_event_row)


class TestLiveEventRow:
    def test_plain_run_event(self):
        row = live_event_row({"ts": "2026-06-12T10:00:05", "level": "INFO",
                              "message": "Tool maps_search returned 3 results"})
        assert row["title"] == "Tool maps_search returned 3 results"
        assert row["time"] == "10:00:05"
        assert row["has_details"] is False
        assert row["decision_id"] is None

    def test_decision_event_uses_context_title_not_blank(self):
        """THE blank-line bug: decision events carry no message (pre-W5.3)."""
        row = live_event_row({
            "category": "operator_decision_posted",
            "context": {"decision_id": "dec_1",
                        "title": "Stuck on Objective 1: 'find the nearest salon'"},
        })
        assert "find the nearest salon" in row["title"]
        assert row["decision_id"] == "dec_1"
        assert row["has_details"] is True  # expandable → inline Answer

    def test_event_with_details_is_expandable(self):
        row = live_event_row({
            "ts": "2026-06-12T10:01:00", "level": "SUCCESS",
            "message": "Task success: find the nearest salon",
            "details": {"summary": "Found 3 salons.", "output_dir": "C:/out"},
        })
        assert row["has_details"] is True
        assert row["details"]["summary"] == "Found 3 salons."

    def test_resolved_decision_event_is_plain(self):
        # Only POSTED decisions offer Answer; resolved ones are plain history.
        row = live_event_row({
            "ts": "2026-06-12T10:02:00", "level": "INFO",
            "message": "Resolved: Stuck on Objective 1",
            "category": "operator_decision_resolved",
            "context": {"decision_id": "dec_1"},
        })
        assert row["decision_id"] is None
        assert row["has_details"] is False

    def test_format_live_run_line_backcompat(self):
        assert format_live_run_line(
            {"level": "INFO", "message": "x"}) == "[INFO] x"


class TestDecisionEventsSelfDescribing:
    @pytest.fixture
    def vault(self, tmp_path: Path):
        from systemu.vault.vault import Vault
        (tmp_path / "decisions").mkdir(parents=True, exist_ok=True)
        (tmp_path / "decisions" / "index.json").write_text("[]", encoding="utf-8")
        return Vault(str(tmp_path))

    def test_posted_and_resolved_events_carry_ts_level_message(self, vault):
        from systemu.approval.decision_queue import OperatorDecisionQueue
        from systemu.interface.event_bus import EventBus

        captured = []
        unsub = EventBus.get().subscribe(captured.append, replay=False)
        try:
            q = OperatorDecisionQueue(vault)
            did = q.post(title="Stuck on Objective 1", body="b",
                         options=["Provide hint", "Other"],
                         context={"kind": "structured_question"},
                         dedup_key="stuck:s:obj_1:r1")
            q.resolve(did, choice='{"action": "Provide hint"}')
        finally:
            unsub()

        cats = {e.get("category"): e for e in captured}
        posted = cats["operator_decision_posted"]
        assert posted["message"] == "Needs you: Stuck on Objective 1"
        assert posted["level"] == "WARNING"
        assert posted["ts"]
        resolved = cats["operator_decision_resolved"]
        assert resolved["message"] == "Resolved: Stuck on Objective 1"
        assert resolved["level"] == "INFO"
        assert resolved["ts"]


class TestOutcomePublishing:
    def test_log_event_passes_details_through(self, tmp_path, monkeypatch):
        from systemu.interface import notifications
        from systemu.interface.event_bus import EventBus

        captured = []
        unsub = EventBus.get().subscribe(captured.append, replay=False)
        try:
            notifications.log_event(
                "SUCCESS", "task_outcome", "Task success: x",
                {"origin": "chat"},
                details={"summary": "done", "output_dir": "C:/out"},
            )
        finally:
            unsub()
        ev = next(e for e in captured if e.get("category") == "task_outcome")
        assert ev["details"] == {"summary": "done", "output_dir": "C:/out"}

    def test_log_event_without_details_unchanged(self):
        from systemu.interface import notifications
        from systemu.interface.event_bus import EventBus
        captured = []
        unsub = EventBus.get().subscribe(captured.append, replay=False)
        try:
            notifications.log_event("INFO", "x", "no details here")
        finally:
            unsub()
        ev = next(e for e in captured if e.get("message") == "no details here")
        assert "details" not in ev

    def test_direct_task_sync_path_publishes_outcome(self):
        from systemu.pipelines import direct_task
        src = inspect.getsource(direct_task)
        assert '"task_outcome"' in src, \
            "the sync path must stream the task outcome to the live panes"
        assert '"output_dir"' in src and '"summary": _summary' in src


class TestDetailsBodyOutcomeSections:
    def test_outcome_and_artifacts_sections_rendered(self):
        from systemu.interface.components import live_events_pane
        src = inspect.getsource(live_events_pane.render_event_details_body)
        assert 'details.get("summary")' in src
        assert 'details.get("output_dir")' in src
        # Dead-button fix: Show-LLM only renders when an llm_ref exists.
        assert "if not llm_ref:" in src
