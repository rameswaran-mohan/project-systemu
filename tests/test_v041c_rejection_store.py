"""Tests for v0.4.1-c RejectionStore + operator-feedback learning.

Covers:
  * record_rejection (idempotent count + first/last timestamps)
  * is_recently_rejected with window
  * list_rejections sorted by count
  * revoke + clear
  * ExecutionMind consults store and downgrades to DO_NOTHING
  * Audit row written on every record
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from systemu.runtime.rejection_store import RejectionStore, reset_singleton_for_tests


@pytest.fixture(autouse=True)
def _reset():
    reset_singleton_for_tests()
    yield
    reset_singleton_for_tests()


# ─────────────────────────────────────────────────────────────────────────────
# RejectionStore core

class TestRecordAndQuery:
    def test_first_record_creates_entry(self, tmp_path):
        store = RejectionStore(
            path=tmp_path / "rej.json",
            audit_path=tmp_path / "audit.jsonl",
        )
        r = store.record_rejection("sig-a", action="NUDGE", dedup_key="k1")
        assert r.reject_count == 1
        assert r.first_rejected_at != ""
        assert store.is_recently_rejected("sig-a") is True

    def test_repeated_rejection_increments_count(self, tmp_path):
        store = RejectionStore(path=tmp_path / "rej.json",
                               audit_path=tmp_path / "audit.jsonl")
        store.record_rejection("sig-a")
        store.record_rejection("sig-a")
        r = store.record_rejection("sig-a")
        assert r.reject_count == 3

    def test_first_timestamp_preserved(self, tmp_path):
        store = RejectionStore(path=tmp_path / "rej.json",
                               audit_path=tmp_path / "audit.jsonl")
        first = store.record_rejection("sig-a").first_rejected_at
        time.sleep(0.01)
        second = store.record_rejection("sig-a").first_rejected_at
        assert first == second   # first timestamp must not move

    def test_unknown_signature_not_rejected(self, tmp_path):
        store = RejectionStore(path=tmp_path / "rej.json",
                               audit_path=tmp_path / "audit.jsonl")
        assert store.is_recently_rejected("never-seen") is False

    def test_empty_signature_treated_as_no(self, tmp_path):
        store = RejectionStore(path=tmp_path / "rej.json",
                               audit_path=tmp_path / "audit.jsonl")
        assert store.is_recently_rejected("") is False


class TestWindow:
    def test_outside_window_returns_false(self, tmp_path):
        store = RejectionStore(path=tmp_path / "rej.json",
                               audit_path=tmp_path / "audit.jsonl")
        store.record_rejection("sig-a")
        # Force the timestamp far into the past
        data = json.loads((tmp_path / "rej.json").read_text(encoding="utf-8"))
        old = (datetime.now(tz=timezone.utc) - timedelta(hours=200)).isoformat(timespec="seconds")
        data["rejections"]["sig-a"]["last_rejected_at"] = old
        data["rejections"]["sig-a"]["first_rejected_at"] = old
        (tmp_path / "rej.json").write_text(json.dumps(data))
        assert store.is_recently_rejected("sig-a", window_hours=48) is False
        assert store.is_recently_rejected("sig-a", window_hours=300) is True


class TestListAndRevoke:
    def test_list_sorted_by_count_desc(self, tmp_path):
        store = RejectionStore(path=tmp_path / "rej.json",
                               audit_path=tmp_path / "audit.jsonl")
        store.record_rejection("a")
        for _ in range(3):
            store.record_rejection("b")
        store.record_rejection("c")
        store.record_rejection("c")
        rejs = store.list_rejections()
        assert [r.pattern_signature for r in rejs] == ["b", "c", "a"]

    def test_revoke_removes(self, tmp_path):
        store = RejectionStore(path=tmp_path / "rej.json",
                               audit_path=tmp_path / "audit.jsonl")
        store.record_rejection("sig-x")
        assert store.revoke("sig-x") is True
        assert store.revoke("sig-x") is False
        assert store.is_recently_rejected("sig-x") is False

    def test_clear_wipes(self, tmp_path):
        store = RejectionStore(path=tmp_path / "rej.json",
                               audit_path=tmp_path / "audit.jsonl")
        store.record_rejection("a")
        store.record_rejection("b")
        assert store.clear() == 2
        assert store.list_rejections() == []


class TestAuditTrail:
    def test_record_writes_audit_row(self, tmp_path):
        audit = tmp_path / "audit.jsonl"
        store = RejectionStore(path=tmp_path / "rej.json", audit_path=audit)
        store.record_rejection("sig", action="NUDGE", dedup_key="k1")
        rows = [json.loads(l) for l in audit.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(rows) == 1
        assert rows[0]["pattern_signature"] == "sig"
        assert rows[0]["action"] == "NUDGE"


# ─────────────────────────────────────────────────────────────────────────────
# ExecutionMind consultation

class TestExecutionMindConsultsRejection:
    def test_downgrades_to_do_nothing_when_signature_rejected(self, tmp_path, monkeypatch):
        # Point the rejection store singleton at our tmp file by patching
        # the get_rejection_store factory.
        from systemu.runtime import rejection_store as rs
        forced = RejectionStore(
            path=tmp_path / "rej.json", audit_path=tmp_path / "audit.jsonl",
        )
        monkeypatch.setattr(rs, "_singleton", forced)
        # Record a rejection for the signature ExecutionMind will compute
        # for our test case: pattern_signature(error_type="param_error",
        # tool_name=None, error_message="bad param 'filename'")
        from systemu.core.memory_types import pattern_signature
        sig = pattern_signature(
            error_type="param_error", tool_name=None,
            error_message="schema requires snake_case",
        )
        forced.record_rejection(sig, action="NUDGE")

        # ExecutionMind that would emit NUDGE for a param_error should now
        # be downgraded to DO_NOTHING by the rejection-store guard.
        from systemu.runtime.execution_mind import ExecutionMind

        config = SimpleNamespace(
            intelligent_supervisor_enabled=True,
            supervisor_llm_budget_per_run=10,
            supervisor_tier_routine="tier_3",
            supervisor_tier_intervention="tier_1",
            supervisor_directive_timeout_s=1.0,
        )
        sink: list = []
        mind = ExecutionMind(
            execution_id="exec_t",
            shadow_id="sh-1",
            config=config,
            directive_sink=sink.append,
            data_dir=tmp_path,
        )
        # Stub the LLM to return NUDGE with a rationale that hashes to the
        # rejected signature.
        monkeypatch.setattr(
            "systemu.core.llm_router.llm_call_json",
            lambda **kw: {
                "action": "NUDGE",
                "rationale": "schema requires snake_case",
                "hint": "use snake_case keys",
            },
        )
        d = mind.evaluate(
            trigger="tool_failure",
            recent_events=[],
            classifier="param_error",
            consec_failures=1,
            iteration=2,
        )
        assert d.action == "DO_NOTHING"
        assert "operator recently dismissed" in d.rationale
