"""R-A12a — the supervisor's transient-failure retry must be DURABLE.

Today a retryable failure arms an in-process ``threading.Timer(wait_s, submit)``
(supervisor.py ~:1312). The activity is ``mark_failed`` on its DB row BEFORE the
timer arms, so if the daemon restarts during the 5–10 s back-off window the
scheduled resubmit is lost and the activity is silently never retried.

R-A12a replaces the Timer with a ``pending_wait`` record persisted on the run's
ExecutionSnapshot (``ExecutionSnapshot.pending_waits``). The record survives a
restart; a separate reconciler (a sibling task) fires the due waits and replays
``submit(activity_id, shadow_id, retry_count=attempt+1, …)``.

These tests drive ``Supervisor._handle_result`` directly (the same bare-supervisor
harness the v0.9.32 command-gate + G1 resume tests use) and assert:
  * a retryable failure ARMS a durable retry wait on the snapshot AND starts NO
    ``threading.Timer`` (and does NOT resubmit synchronously);
  * the record carries the replay kwargs the reconciler needs
    (activity_id, shadow_id, attempt, execution_id) + ``fire_at`` ≈ now+5*(attempt+1);
  * a failure at/over max attempts arms NO wait (falls through to dead-letter);
  * a failure whose result dict has NO execution_id (the worker-thread exception
    path) is still armed under a stable synthetic key (the durability guarantee
    must not evaporate exactly for the failures most likely to recur);
  * re-handling the same failed run is idempotent (dedupe by wait_id).
"""
from __future__ import annotations

import threading
import time

import pytest

from systemu.runtime.supervisor import Supervisor, MAX_RETRIES
from systemu.runtime.execution_snapshot import read_snapshot


def _bare_supervisor(tmp_path):
    """A Supervisor with __init__ bypassed, wired just enough to drive the
    retry-vs-dead-letter decision in ``_handle_result``. Snapshot I/O is
    redirected at ``tmp_path`` (the ``_snapshot_data_dir`` test seam) so the
    durable wait never touches the repo's ``./data`` dir."""
    sup = Supervisor.__new__(Supervisor)
    sup.vault = None
    sup._task_queue = None
    sup._dl_lock = threading.Lock()
    sup._dead_letters = []
    sup._publish = lambda *a, **k: None
    sup._aname = lambda aid: aid
    sup._analyze_failure = lambda *a, **k: None
    sup._snapshot_data_dir = tmp_path
    return sup


class _TimerSpy:
    """Records every ``threading.Timer`` constructed/started so a test can assert
    the durable retry path armed NO in-process timer."""

    instances: list = []

    def __init__(self, *a, **kw):
        self.args, self.kwargs = a, kw
        self.started = False
        _TimerSpy.instances.append(self)

    def start(self):
        self.started = True


@pytest.fixture(autouse=True)
def _reset_timer_spy():
    _TimerSpy.instances = []
    yield
    _TimerSpy.instances = []


def test_retry_arms_durable_wait_not_timer(monkeypatch, tmp_path):
    import systemu.runtime.supervisor as sup_mod
    monkeypatch.setattr(sup_mod.threading, "Timer", _TimerSpy)

    sup = _bare_supervisor(tmp_path)
    # Any synchronous resubmit would be a bug — the durable wait defers the
    # resubmit to the reconciler, which fires it only when fire_at is due.
    submit_calls: list = []
    monkeypatch.setattr(sup, "submit", lambda **kw: submit_calls.append(kw))

    payload = {"activity_id": "act_1", "shadow_id": "sh_1",
               "retry_count": 0, "origin": "chat", "priority": 5}
    result = {"status": "failure", "error": "boom", "execution_id": "exec_1"}

    before = time.time()
    sup._handle_result(payload, result)
    after = time.time()

    # NO in-process timer was even constructed — the retry is durable, not a
    # wall-clock Timer that a restart would drop.
    assert _TimerSpy.instances == []
    # NO synchronous resubmit — the reconciler fires the wait later.
    assert submit_calls == []

    # A durable retry wait was armed on the run's snapshot.
    snap = read_snapshot("exec_1", data_dir=tmp_path)
    assert snap is not None
    assert len(snap.pending_waits) == 1
    w = snap.pending_waits[0]
    assert w["wait_kind"] == "retry"
    assert w["activity_id"] == "act_1"
    assert w["shadow_id"] == "sh_1"
    assert w["execution_id"] == "exec_1"
    assert w["attempt"] == 0
    assert w["max_attempts"] == MAX_RETRIES
    assert w["dispatched"] is False
    # fire_at ≈ now + 5*(attempt+1) = now + 5 s (wall clock stamped at the arm site).
    lo, hi = before + 5 * (0 + 1), after + 5 * (0 + 1)
    assert lo - 0.5 <= w["fire_at"] <= hi + 0.5


def test_retry_at_max_attempts_does_not_arm(monkeypatch, tmp_path):
    import systemu.runtime.supervisor as sup_mod
    monkeypatch.setattr(sup_mod.threading, "Timer", _TimerSpy)

    sup = _bare_supervisor(tmp_path)
    submit_calls: list = []
    monkeypatch.setattr(sup, "submit", lambda **kw: submit_calls.append(kw))

    payload = {"activity_id": "act_2", "shadow_id": "sh_2",
               "retry_count": MAX_RETRIES, "origin": "chat"}
    result = {"status": "failure", "error": "boom", "execution_id": "exec_2"}

    sup._handle_result(payload, result)

    # At/over max attempts _should_retry is False → the existing dead-letter path
    # runs; NO durable wait is armed.
    assert read_snapshot("exec_2", data_dir=tmp_path) is None
    assert len(sup._dead_letters) == 1
    assert sup._dead_letters[0]["activity_id"] == "act_2"
    assert _TimerSpy.instances == []
    assert submit_calls == []


def test_retry_without_execution_id_uses_stable_fallback_key(monkeypatch, tmp_path):
    """The worker-thread exception path builds a result dict with NO execution_id.
    The durable wait must still be armed — under a stable synthetic key derived from
    activity + shadow_id + attempt (the shadow_id makes the key RUN-UNIQUE so two
    distinct runs of the same activity+attempt don't collide; see
    test_ra12a_synthetic_key_unique) — so a restart-recurring failure is not silently
    dropped exactly when durability matters most."""
    import systemu.runtime.supervisor as sup_mod
    monkeypatch.setattr(sup_mod.threading, "Timer", _TimerSpy)

    sup = _bare_supervisor(tmp_path)
    monkeypatch.setattr(sup, "submit", lambda **kw: None)

    payload = {"activity_id": "act_3", "shadow_id": "sh_3", "retry_count": 1}
    result = {"status": "failure", "error": "boom"}   # NO execution_id

    sup._handle_result(payload, result)

    # Run-unique synthetic key: retryarm-<activity_id>-<shadow_id>-<attempt>.
    snap = read_snapshot("retryarm-act_3-sh_3-1", data_dir=tmp_path)
    assert snap is not None
    assert len(snap.pending_waits) == 1
    w = snap.pending_waits[0]
    assert w["activity_id"] == "act_3"
    assert w["shadow_id"] == "sh_3"
    assert w["attempt"] == 1
    assert _TimerSpy.instances == []


def test_arming_is_idempotent_by_wait_id(monkeypatch, tmp_path):
    """Re-handling the same failed run (same execution_id + attempt) must not
    accumulate twin waits — arm_wait dedupes by wait_id."""
    import systemu.runtime.supervisor as sup_mod
    monkeypatch.setattr(sup_mod.threading, "Timer", _TimerSpy)

    sup = _bare_supervisor(tmp_path)
    monkeypatch.setattr(sup, "submit", lambda **kw: None)

    payload = {"activity_id": "act_4", "shadow_id": "sh_4", "retry_count": 0}
    result = {"status": "failure", "error": "boom", "execution_id": "exec_4"}

    sup._handle_result(payload, result)
    sup._handle_result(payload, result)

    snap = read_snapshot("exec_4", data_dir=tmp_path)
    assert snap is not None
    assert len(snap.pending_waits) == 1
