"""Task 1 (harness grant-resume) — `_handle_result` park-safety.

When a shadow run parks itself on a blocking harness ESCALATE it returns the
new ``suspended_harness_escalation`` status. The Supervisor's ``_handle_result``
must PARK the activity (leave it ASSIGNED, the snapshot is on disk for the
harness-grant reconciler) — it must NOT schedule a retry timer, NOT dead-letter,
and NOT mutate the activity to a terminal state. This mirrors the ``cancelled``
branch (publish + early return; the running-set / semaphore release is already
done by the caller's finally block before ``_handle_result`` runs).

Pattern mirrors tests/test_v0_9_7_resume_after_grant.py:40-52.
"""
from __future__ import annotations

import queue
import threading
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from systemu.runtime.supervisor import Supervisor


def _supervisor_stub() -> Supervisor:
    """Minimal Supervisor that bypasses thread setup (mirrors resume_after_grant test)."""
    s = Supervisor.__new__(Supervisor)
    s.vault = SimpleNamespace()
    s._pending_lock = threading.Lock()
    s._pending_activity_ids = set()
    s._running_lock = threading.Lock()
    s._running = {}
    s._task_queue = None
    s._queue = queue.PriorityQueue()
    s._dl_lock = threading.Lock()
    s._dead_letters = []
    # Silence EventBus publish in unit tests.
    s._publish = lambda *a, **kw: None
    return s


def test_suspended_harness_status_parks_not_retries(monkeypatch):
    sup = _supervisor_stub()

    # Capture any retry submit() and any background diagnosis thread.
    submit_calls: List[Dict[str, Any]] = []
    sup.submit = lambda *a, **kw: submit_calls.append(kw)  # type: ignore[assignment]

    timers_started: List[Any] = []
    real_timer = threading.Timer

    def _record_timer(*a, **kw):
        t = real_timer(*a, **kw)
        timers_started.append(t)
        return t

    monkeypatch.setattr("systemu.runtime.supervisor.threading.Timer", _record_timer)

    threads_started: List[Any] = []
    real_thread = threading.Thread

    def _record_thread(*a, **kw):
        t = real_thread(*a, **kw)
        threads_started.append(t)
        return t

    monkeypatch.setattr("systemu.runtime.supervisor.threading.Thread", _record_thread)

    # save_activity must NOT be called for the park branch (no terminal mutation).
    save_calls: List[Any] = []
    sup.vault.save_activity = lambda act: save_calls.append(act)  # type: ignore[attr-defined]
    sup.vault.get_activity = lambda aid: (_ for _ in ()).throw(  # type: ignore[attr-defined]
        AssertionError("get_activity should not be called in the park branch")
    )

    payload = {
        "activity_id": "act_x",
        "shadow_id": "sh_x",
        "retry_count": 0,
        "submission_id": "sub_x",
    }
    result = {
        "status": "suspended_harness_escalation",
        "execution_id": "exec_x",
        "activity_id": "act_x",
        "shadow_id": "sh_x",
    }

    sup._handle_result(payload, result)

    # No retry timer scheduled.
    assert timers_started == [], "park must NOT schedule a retry timer"
    # No retry submit().
    assert submit_calls == [], "park must NOT re-submit the activity"
    # No dead-letter recorded.
    assert sup._dead_letters == [], "park must NOT dead-letter"
    # No background failure-diagnosis thread.
    assert threads_started == [], "park must NOT launch failure diagnosis"
    # No terminal activity mutation.
    assert save_calls == [], "park must NOT mark the activity terminal"
