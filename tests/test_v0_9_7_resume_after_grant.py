"""v0.9.7 — Supervisor.resume_after_grant mechanical tests.

Verifies:
  1. resume_after_grant re-enqueues a snapshotted execution with the grant
     payload injected as a ``__HARNESS_GRANT__`` sticky note on the snapshot
     and calls submit() with resume_from_execution_id.
  2. Double-call is idempotent — the sticky note is already present, so no
     second submit() is issued and the sentinel sub_already_dispatched_* is
     returned.
  3. Missing snapshot returns sub_no_dispatch_* and does NOT call submit().
  4. Grant payload is carried faithfully in the sticky note (JSON-encoded).
  5. Submit is called with priority=1, consult_affinity_log=False, correct
     origin/chat_submission_id threading.

Pattern mirrors test_v050e_resume.py + test_v0_8_22_1_resumable_decisions.py.
"""
from __future__ import annotations

import json
import queue
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from systemu.runtime.supervisor import Supervisor
from systemu.runtime.execution_snapshot import (
    ExecutionSnapshot,
    write_snapshot,
    read_snapshot,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _supervisor_stub(vault=None) -> Supervisor:
    """Minimal Supervisor that bypasses thread setup."""
    s = Supervisor.__new__(Supervisor)
    s.vault = vault or SimpleNamespace()
    s._pending_lock = threading.Lock()
    s._pending_activity_ids = set()
    s._running_lock = threading.Lock()
    s._running = {}
    s._task_queue = None
    s._queue = queue.PriorityQueue()
    # Silence EventBus publish in unit tests
    s._publish = lambda *a, **kw: None
    return s


def _write_test_snapshot(data_dir: Path, execution_id: str, **overrides) -> ExecutionSnapshot:
    """Write a minimal snapshot for the given execution_id and return it."""
    snap = ExecutionSnapshot(
        execution_id=execution_id,
        shadow_id=overrides.get("shadow_id", "sh-1"),
        scroll_id=overrides.get("scroll_id", "sc-1"),
        activity_id=overrides.get("activity_id", "act-1"),
        iteration=overrides.get("iteration", 3),
        completed_objective_ids=overrides.get("completed_objective_ids", [0, 1]),
        sticky_notes=list(overrides.get("sticky_notes", [])),
    )
    write_snapshot(snap, data_dir=data_dir)
    return snap


# ─────────────────────────────────────────────────────────────────────────────
# Test 1: happy-path re-dispatch
# ─────────────────────────────────────────────────────────────────────────────

class TestResumeAfterGrantHappyPath:
    def test_requeues_with_grant_payload_in_snapshot(self, tmp_path, monkeypatch):
        """resume_after_grant writes the grant payload into the snapshot as a
        __HARNESS_GRANT__ sticky note and calls submit() with the right args."""
        data_dir = tmp_path / "data"
        (data_dir / "audit").mkdir(parents=True, exist_ok=True)

        _write_test_snapshot(data_dir, "exec-H1",
                             shadow_id="sh-1", activity_id="act-1")

        submit_calls: List[Dict[str, Any]] = []

        sup = _supervisor_stub()

        def _fake_submit(activity_id, shadow_id, **kw):
            submit_calls.append({
                "activity_id": activity_id,
                "shadow_id":   shadow_id,
                **kw,
            })
            return "sub_test_001"

        monkeypatch.setattr(sup, "submit", _fake_submit)

        # Patch read/write_snapshot to use our data_dir
        import systemu.runtime.execution_snapshot as _es
        monkeypatch.setattr(_es, "read_snapshot",
                            lambda eid, **kw: read_snapshot(eid, data_dir=data_dir))
        monkeypatch.setattr(_es, "write_snapshot",
                            lambda snap, **kw: write_snapshot(snap, data_dir=data_dir))

        grant_payload = {"granted_tool": "geocode_place", "tool_id": "tool_abc"}

        sub_id = sup.resume_after_grant(
            execution_id="exec-H1",
            activity_id="act-1",
            shadow_id="sh-1",
            grant_payload=grant_payload,
            origin="chat",
            chat_submission_id="ts-2026",
        )

        # submit() was called exactly once
        assert len(submit_calls) == 1
        call = submit_calls[0]
        assert call["activity_id"] == "act-1"
        assert call["shadow_id"] == "sh-1"
        assert call["resume_from_execution_id"] == "exec-H1"
        assert call["priority"] == 1
        assert call["consult_affinity_log"] is False
        assert call.get("origin") == "chat"
        assert call.get("chat_submission_id") == "ts-2026"

        # Sub_id propagated
        assert sub_id == "sub_test_001"

        # Grant payload was written into the snapshot
        snap = read_snapshot("exec-H1", data_dir=data_dir)
        assert snap is not None
        grant_notes = [n for n in snap.sticky_notes
                       if n.startswith(f"__HARNESS_GRANT__::exec-H1::")]
        assert len(grant_notes) == 1
        # JSON-decode and verify contents
        suffix = grant_notes[0].split("::", 2)[2]
        decoded = json.loads(suffix)
        assert decoded["granted_tool"] == "geocode_place"
        assert decoded["tool_id"] == "tool_abc"

    def test_operator_answer_payload(self, tmp_path, monkeypatch):
        """INPUT / ASK_OPERATOR grant carries operator_answer in the payload."""
        data_dir = tmp_path / "data"
        (data_dir / "audit").mkdir(parents=True, exist_ok=True)
        _write_test_snapshot(data_dir, "exec-H2",
                             shadow_id="sh-2", activity_id="act-2")

        submit_calls = []
        sup = _supervisor_stub()
        monkeypatch.setattr(sup, "submit",
                            lambda *a, **kw: submit_calls.append(kw) or "sub_x")

        import systemu.runtime.execution_snapshot as _es
        monkeypatch.setattr(_es, "read_snapshot",
                            lambda eid, **kw: read_snapshot(eid, data_dir=data_dir))
        monkeypatch.setattr(_es, "write_snapshot",
                            lambda snap, **kw: write_snapshot(snap, data_dir=data_dir))

        sup.resume_after_grant(
            execution_id="exec-H2",
            activity_id="act-2",
            shadow_id="sh-2",
            grant_payload={"operator_answer": "Use the Bangalore fallback"},
        )

        snap = read_snapshot("exec-H2", data_dir=data_dir)
        grant_notes = [n for n in snap.sticky_notes
                       if n.startswith("__HARNESS_GRANT__::exec-H2::")]
        assert len(grant_notes) == 1
        decoded = json.loads(grant_notes[0].split("::", 2)[2])
        assert "Bangalore" in decoded["operator_answer"]

    def test_deny_payload(self, tmp_path, monkeypatch):
        """DENY grant still re-submits so the shadow can continue with fallback."""
        data_dir = tmp_path / "data"
        (data_dir / "audit").mkdir(parents=True, exist_ok=True)
        _write_test_snapshot(data_dir, "exec-H3",
                             shadow_id="sh-3", activity_id="act-3")

        submit_calls = []
        sup = _supervisor_stub()
        monkeypatch.setattr(sup, "submit",
                            lambda *a, **kw: submit_calls.append(kw) or "sub_d")

        import systemu.runtime.execution_snapshot as _es
        monkeypatch.setattr(_es, "read_snapshot",
                            lambda eid, **kw: read_snapshot(eid, data_dir=data_dir))
        monkeypatch.setattr(_es, "write_snapshot",
                            lambda snap, **kw: write_snapshot(snap, data_dir=data_dir))

        sup.resume_after_grant(
            execution_id="exec-H3",
            activity_id="act-3",
            shadow_id="sh-3",
            grant_payload={"denied": True, "rationale": "policy forbids forging"},
        )

        # submit was called
        assert len(submit_calls) == 1
        # snapshot carries the deny note
        snap = read_snapshot("exec-H3", data_dir=data_dir)
        grant_notes = [n for n in snap.sticky_notes
                       if n.startswith("__HARNESS_GRANT__::exec-H3::")]
        decoded = json.loads(grant_notes[0].split("::", 2)[2])
        assert decoded["denied"] is True


# ─────────────────────────────────────────────────────────────────────────────
# Test 2: Idempotency — double-resume doesn't double-dispatch
# ─────────────────────────────────────────────────────────────────────────────

class TestResumeAfterGrantIdempotency:
    def test_double_call_does_not_double_dispatch(self, tmp_path, monkeypatch):
        """A second call with the same execution_id is a no-op: the grant note
        is already on the snapshot, so submit() is NOT called a second time."""
        data_dir = tmp_path / "data"
        (data_dir / "audit").mkdir(parents=True, exist_ok=True)
        _write_test_snapshot(data_dir, "exec-I1",
                             shadow_id="sh-i", activity_id="act-i")

        submit_calls = []
        sup = _supervisor_stub()
        monkeypatch.setattr(sup, "submit",
                            lambda *a, **kw: submit_calls.append(kw) or "sub_i")

        import systemu.runtime.execution_snapshot as _es
        monkeypatch.setattr(_es, "read_snapshot",
                            lambda eid, **kw: read_snapshot(eid, data_dir=data_dir))
        monkeypatch.setattr(_es, "write_snapshot",
                            lambda snap, **kw: write_snapshot(snap, data_dir=data_dir))

        grant_payload = {"granted_tool": "bing_search"}

        # First call — should dispatch
        sub1 = sup.resume_after_grant(
            execution_id="exec-I1",
            activity_id="act-i",
            shadow_id="sh-i",
            grant_payload=grant_payload,
        )
        assert len(submit_calls) == 1

        # Second call — same execution_id — must NOT dispatch again
        sub2 = sup.resume_after_grant(
            execution_id="exec-I1",
            activity_id="act-i",
            shadow_id="sh-i",
            grant_payload=grant_payload,
        )
        assert len(submit_calls) == 1          # still 1 — no second dispatch
        assert sub2.startswith("sub_already_dispatched_")

    def test_pre_stamped_snapshot_is_idempotent(self, tmp_path, monkeypatch):
        """If the snapshot was already stamped (e.g. daemon restarted after the
        first dispatch), a fresh call should still be a no-op."""
        data_dir = tmp_path / "data"
        (data_dir / "audit").mkdir(parents=True, exist_ok=True)

        # Pre-stamp the grant note before calling resume_after_grant
        snap = _write_test_snapshot(data_dir, "exec-I2",
                                    shadow_id="sh-i2", activity_id="act-i2")
        snap.sticky_notes.append(
            '__HARNESS_GRANT__::exec-I2::{"granted_tool":"already_done"}'
        )
        write_snapshot(snap, data_dir=data_dir)

        submit_calls = []
        sup = _supervisor_stub()
        monkeypatch.setattr(sup, "submit",
                            lambda *a, **kw: submit_calls.append(kw) or "sub_x")

        import systemu.runtime.execution_snapshot as _es
        monkeypatch.setattr(_es, "read_snapshot",
                            lambda eid, **kw: read_snapshot(eid, data_dir=data_dir))
        monkeypatch.setattr(_es, "write_snapshot",
                            lambda snap, **kw: write_snapshot(snap, data_dir=data_dir))

        sub = sup.resume_after_grant(
            execution_id="exec-I2",
            activity_id="act-i2",
            shadow_id="sh-i2",
            grant_payload={"granted_tool": "new_tool"},
        )
        assert len(submit_calls) == 0
        assert sub.startswith("sub_already_dispatched_")


# ─────────────────────────────────────────────────────────────────────────────
# Test 3: Missing snapshot
# ─────────────────────────────────────────────────────────────────────────────

class TestResumeAfterGrantMissingSnapshot:
    def test_missing_snapshot_returns_no_dispatch_sentinel(self, tmp_path, monkeypatch):
        """When there is no snapshot on disk (shadow_runtime failed to write it),
        resume_after_grant returns a sentinel and does NOT call submit()."""
        data_dir = tmp_path / "data"
        (data_dir / "audit").mkdir(parents=True, exist_ok=True)
        # No snapshot written

        submit_calls = []
        sup = _supervisor_stub()
        monkeypatch.setattr(sup, "submit",
                            lambda *a, **kw: submit_calls.append(kw) or "sub_x")

        import systemu.runtime.execution_snapshot as _es
        monkeypatch.setattr(_es, "read_snapshot",
                            lambda eid, **kw: read_snapshot(eid, data_dir=data_dir))

        sub = sup.resume_after_grant(
            execution_id="exec-MISSING",
            activity_id="act-missing",
            shadow_id="sh-missing",
            grant_payload={"granted_tool": "geocode"},
        )
        assert sub.startswith("sub_no_dispatch_")
        assert len(submit_calls) == 0


# ─────────────────────────────────────────────────────────────────────────────
# Test 4: Submit shape — mirrors TestSubmissionShape from test_v050e_resume.py
# ─────────────────────────────────────────────────────────────────────────────

class TestResumeAfterGrantSubmitShape:
    def test_priority_and_affinity_flags(self, tmp_path, monkeypatch):
        """resume_after_grant must use priority=1, retry_count=0,
        consult_affinity_log=False. (Fix #7: a successful grant-resume is
        forward progress, not a failure-retry — it must NOT pre-consume a
        MAX_RETRIES slot. The double-dispatch guard is the __HARNESS_GRANT__
        snapshot stamp, not retry_count.)"""
        data_dir = tmp_path / "data"
        (data_dir / "audit").mkdir(parents=True, exist_ok=True)
        _write_test_snapshot(data_dir, "exec-S1",
                             shadow_id="sh-s1", activity_id="act-s1")

        captured: Dict[str, Any] = {}

        def _fake_submit(activity_id, shadow_id, **kw):
            captured["activity_id"] = activity_id
            captured["shadow_id"] = shadow_id
            captured.update(kw)
            return "sub_shape"

        sup = _supervisor_stub()
        monkeypatch.setattr(sup, "submit", _fake_submit)

        import systemu.runtime.execution_snapshot as _es
        monkeypatch.setattr(_es, "read_snapshot",
                            lambda eid, **kw: read_snapshot(eid, data_dir=data_dir))
        monkeypatch.setattr(_es, "write_snapshot",
                            lambda snap, **kw: write_snapshot(snap, data_dir=data_dir))

        sup.resume_after_grant(
            execution_id="exec-S1",
            activity_id="act-s1",
            shadow_id="sh-s1",
            grant_payload={"granted_tool": "geocode"},
            origin="manual",
            chat_submission_id="ts-xyz",
        )

        assert captured["priority"] == 1
        assert captured["retry_count"] == 0   # Fix #7: grant-resume is not a retry
        assert captured["consult_affinity_log"] is False
        assert captured["resume_from_execution_id"] == "exec-S1"
        assert captured["origin"] == "manual"
        assert captured["chat_submission_id"] == "ts-xyz"
        assert captured["activity_id"] == "act-s1"
        assert captured["shadow_id"] == "sh-s1"

    def test_reason_contains_execution_id_prefix(self, tmp_path, monkeypatch):
        """The submit reason must contain 'harness_grant' so logs are searchable."""
        data_dir = tmp_path / "data"
        (data_dir / "audit").mkdir(parents=True, exist_ok=True)
        _write_test_snapshot(data_dir, "exec-S2",
                             shadow_id="sh-s2", activity_id="act-s2")

        captured: Dict[str, Any] = {}
        sup = _supervisor_stub()
        monkeypatch.setattr(sup, "submit",
                            lambda *a, **kw: captured.update(kw) or "sub_r")

        import systemu.runtime.execution_snapshot as _es
        monkeypatch.setattr(_es, "read_snapshot",
                            lambda eid, **kw: read_snapshot(eid, data_dir=data_dir))
        monkeypatch.setattr(_es, "write_snapshot",
                            lambda snap, **kw: write_snapshot(snap, data_dir=data_dir))

        sup.resume_after_grant(
            execution_id="exec-S2",
            activity_id="act-s2",
            shadow_id="sh-s2",
            grant_payload={"operator_answer": "yes"},
        )

        reason = captured.get("reason", "")
        assert "harness_grant" in reason
        assert len(reason) <= 120   # matches the [:120] cap in the implementation

    def test_default_origin_is_chat(self, tmp_path, monkeypatch):
        """When origin is omitted, it defaults to 'chat'."""
        data_dir = tmp_path / "data"
        (data_dir / "audit").mkdir(parents=True, exist_ok=True)
        _write_test_snapshot(data_dir, "exec-S3",
                             shadow_id="sh-s3", activity_id="act-s3")

        captured: Dict[str, Any] = {}
        sup = _supervisor_stub()
        monkeypatch.setattr(sup, "submit",
                            lambda *a, **kw: captured.update(kw) or "sub_o")

        import systemu.runtime.execution_snapshot as _es
        monkeypatch.setattr(_es, "read_snapshot",
                            lambda eid, **kw: read_snapshot(eid, data_dir=data_dir))
        monkeypatch.setattr(_es, "write_snapshot",
                            lambda snap, **kw: write_snapshot(snap, data_dir=data_dir))

        sup.resume_after_grant(
            execution_id="exec-S3",
            activity_id="act-s3",
            shadow_id="sh-s3",
            grant_payload={"operator_answer": "go ahead"},
            # origin NOT passed
        )

        assert captured.get("origin") == "chat"


# ─────────────────────────────────────────────────────────────────────────────
# Test 5: Snapshot sticky notes preserved
# ─────────────────────────────────────────────────────────────────────────────

class TestResumeAfterGrantSnapshotIntegrity:
    def test_existing_sticky_notes_are_preserved(self, tmp_path, monkeypatch):
        """resume_after_grant must NOT discard existing sticky notes — it appends
        the grant note, leaving all previous notes intact."""
        data_dir = tmp_path / "data"
        (data_dir / "audit").mkdir(parents=True, exist_ok=True)
        _write_test_snapshot(
            data_dir, "exec-N1",
            shadow_id="sh-n1", activity_id="act-n1",
            sticky_notes=["existing note 1", "another note"],
        )

        sup = _supervisor_stub()
        monkeypatch.setattr(sup, "submit", lambda *a, **kw: "sub_n")

        import systemu.runtime.execution_snapshot as _es
        monkeypatch.setattr(_es, "read_snapshot",
                            lambda eid, **kw: read_snapshot(eid, data_dir=data_dir))
        monkeypatch.setattr(_es, "write_snapshot",
                            lambda snap, **kw: write_snapshot(snap, data_dir=data_dir))

        sup.resume_after_grant(
            execution_id="exec-N1",
            activity_id="act-n1",
            shadow_id="sh-n1",
            grant_payload={"granted_tool": "geocode"},
        )

        snap = read_snapshot("exec-N1", data_dir=data_dir)
        assert "existing note 1" in snap.sticky_notes
        assert "another note" in snap.sticky_notes
        grant_notes = [n for n in snap.sticky_notes
                       if n.startswith("__HARNESS_GRANT__::exec-N1::")]
        assert len(grant_notes) == 1

    def test_completed_objectives_preserved(self, tmp_path, monkeypatch):
        """Completed objective ids in the snapshot must survive the grant write."""
        data_dir = tmp_path / "data"
        (data_dir / "audit").mkdir(parents=True, exist_ok=True)
        _write_test_snapshot(
            data_dir, "exec-N2",
            shadow_id="sh-n2", activity_id="act-n2",
            completed_objective_ids=[0, 1, 2],
        )

        sup = _supervisor_stub()
        monkeypatch.setattr(sup, "submit", lambda *a, **kw: "sub_c")

        import systemu.runtime.execution_snapshot as _es
        monkeypatch.setattr(_es, "read_snapshot",
                            lambda eid, **kw: read_snapshot(eid, data_dir=data_dir))
        monkeypatch.setattr(_es, "write_snapshot",
                            lambda snap, **kw: write_snapshot(snap, data_dir=data_dir))

        sup.resume_after_grant(
            execution_id="exec-N2",
            activity_id="act-n2",
            shadow_id="sh-n2",
            grant_payload={"operator_answer": "proceed"},
        )

        snap = read_snapshot("exec-N2", data_dir=data_dir)
        assert snap.completed_objective_ids == [0, 1, 2]
