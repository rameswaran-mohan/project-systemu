"""R-A12a / IMPL-11 — cancelling a run must expire its durable pending_waits.

When an operator CANCELS a run, its durable ``ExecutionSnapshot.pending_waits``
(the retry timers armed by the supervisor's transient-failure path) must never
resubmit the cancelled run. There are TWO layers that guarantee this:

  1. **Proactive expiry (this task).** The supervisor's CANCELLED finalizer
     (``_handle_result`` → ``status == "cancelled"``) reads the run's snapshot by
     ``execution_id`` (carried on the cancelled result dict), ``expire_all`` its
     ``pending_waits`` (stamps every wait ``dispatched``) and re-persists it — so
     the timers are cleared PROMPTLY at cancel time, not left dangling until a
     later reconciler tick skips / staleness-drops them.

  2. **Reconciler belt-and-braces (already committed, Task 4).** Even if a wait is
     somehow still undispatched, ``external_wait_reconciler`` re-checks the run's
     CANCELLED status every tick (``_run_is_cancelled``) and expires the wait with
     NO ``supervisor.submit``.

These tests pin both layers and their end-to-end composition:
  * cancelling a run expires its pending_waits on disk (proactive);
  * the reconciler never resubmits a CANCELLED run's wait (belt-and-braces);
  * a late wait armed right as/after cancel is a no-op — no resubmit, the run
    stays CANCELLED.
"""
from __future__ import annotations

import threading

import pytest

from systemu.core.models import ActivityStatus
from systemu.runtime.supervisor import Supervisor
from systemu.runtime.execution_snapshot import (
    ExecutionSnapshot,
    read_snapshot,
    write_snapshot,
)
from systemu.runtime.pending_waits import make_retry_wait
from systemu.scheduler.jobs import external_wait_reconciler


# ─────────────────────────────────────────────────────────────────────────────
# Harnesses
# ─────────────────────────────────────────────────────────────────────────────

def _bare_supervisor(data_dir):
    """A Supervisor with __init__ bypassed, wired just enough to drive the
    CANCELLED branch of ``_handle_result``. Snapshot I/O is redirected at
    ``data_dir`` (the ``_snapshot_data_dir`` seam Task 3 added) so the expiry
    never touches the repo's ``./data`` dir. Mirrors the durable-retry test's
    bare harness."""
    sup = Supervisor.__new__(Supervisor)
    sup.vault = None
    sup._task_queue = None
    sup._dl_lock = threading.Lock()
    sup._dead_letters = []
    sup._publish = lambda *a, **k: None
    sup._aname = lambda aid: aid
    sup._analyze_failure = lambda *a, **k: None
    sup._snapshot_data_dir = data_dir
    return sup


class _FakeSupervisor:
    """Records ``submit(**kw)`` calls; models an EMPTY running set so the
    reconciler treats every run as parked (not live). Mirrors the reconciler
    test's fake."""

    def __init__(self):
        self.calls: list = []
        self._running: dict = {}
        self._running_lock = threading.Lock()
        self._pending_activity_ids: set = set()
        self._pending_lock = threading.Lock()

    def submit(self, activity_id, shadow_id, **kw):
        self.calls.append({"activity_id": activity_id, "shadow_id": shadow_id, **kw})
        return f"sub_{len(self.calls)}"


class _FakeVault:
    """Minimal vault exposing ``get_activity`` for the reconciler's CANCELLED
    check. Every id defaults to CANCELLED here (these tests only ever ask about
    the cancelled run)."""

    def __init__(self, status=ActivityStatus.CANCELLED):
        self._status = status

    def get_activity(self, activity_id):
        from types import SimpleNamespace
        return SimpleNamespace(id=activity_id, status=self._status)


def _undispatched_due_wait(*, execution_id, activity_id, shadow_id, attempt=1, now=1_000_000.0):
    """A durable retry wait that is DUE (fire_at in the past) and undispatched."""
    return make_retry_wait(
        execution_id=execution_id, activity_id=activity_id, shadow_id=shadow_id,
        root_execution_id=execution_id, delay_s=0.0, attempt=attempt,
        max_attempts=5, now=now - 10.0,
    )


def _seed_snapshot(data_dir, *, execution_id, activity_id, shadow_id, waits):
    write_snapshot(
        ExecutionSnapshot(
            execution_id=execution_id,
            shadow_id=shadow_id,
            scroll_id="scr1",
            activity_id=activity_id,
            pending_waits=list(waits),
        ),
        data_dir=data_dir,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_cancel_expires_pending_waits(tmp_path):
    """Driving the CANCELLED finalizer expires (stamps dispatched) every durable
    pending_wait on the run's snapshot, so no retry timer survives the cancel."""
    data_dir = tmp_path / "data"
    eid = "exec_c1"
    w1 = _undispatched_due_wait(execution_id=eid, activity_id="act_c1", shadow_id="sh_c1", attempt=0)
    w2 = _undispatched_due_wait(execution_id=eid, activity_id="act_c1", shadow_id="sh_c1", attempt=1)
    _seed_snapshot(data_dir, execution_id=eid, activity_id="act_c1", shadow_id="sh_c1", waits=[w1, w2])

    sup = _bare_supervisor(data_dir)
    payload = {"activity_id": "act_c1", "shadow_id": "sh_c1", "submission_id": "sub_c1"}
    # The cancelled result carries execution_id (build_result stamps it).
    result = {"status": "cancelled", "execution_id": eid, "summary": "Cancelled by operator"}

    sup._handle_result(payload, result)

    snap = read_snapshot(eid, data_dir=data_dir)
    assert snap is not None
    assert len(snap.pending_waits) == 2
    assert all(w["dispatched"] is True for w in snap.pending_waits)


def test_cancel_without_execution_id_does_not_crash(tmp_path):
    """A cancelled result dict with NO execution_id (defensive) must not break the
    cancel finalizer — the expiry is a best-effort no-op."""
    data_dir = tmp_path / "data"
    sup = _bare_supervisor(data_dir)
    payload = {"activity_id": "act_c2", "shadow_id": "sh_c2"}
    result = {"status": "cancelled"}   # NO execution_id

    # Must not raise; the existing cancel behavior still completes.
    sup._handle_result(payload, result)


def test_reconciler_never_resubmits_a_cancelled_run(tmp_path):
    """IMPL-11 belt-and-braces: even if a wait is somehow still undispatched, the
    reconciler seeing the run CANCELLED expires it WITHOUT calling submit."""
    data_dir = tmp_path / "data"
    now = 1_000_000.0
    eid = "exec_c3"
    w = _undispatched_due_wait(execution_id=eid, activity_id="act_c3", shadow_id="sh_c3", now=now)
    _seed_snapshot(data_dir, execution_id=eid, activity_id="act_c3", shadow_id="sh_c3", waits=[w])

    sup = _FakeSupervisor()
    vault = _FakeVault(ActivityStatus.CANCELLED)

    count = external_wait_reconciler(vault=vault, supervisor=sup, data_dir=data_dir, now=now)

    assert count == 0
    assert sup.calls == []
    snap = read_snapshot(eid, data_dir=data_dir)
    assert snap.pending_waits[0]["dispatched"] is True   # expired, not resubmitted


def test_late_wait_after_cancel_is_noop(tmp_path):
    """End-to-end: a run is cancelled (finalizer expires its wait); then a late
    wait armed right as/after cancel lands on the snapshot. The reconciler, seeing
    the run CANCELLED, expires the late wait too — no resubmit, run stays
    CANCELLED."""
    data_dir = tmp_path / "data"
    now = 1_000_000.0
    eid = "exec_c4"

    # 1) A durable wait armed before the cancel.
    w_early = _undispatched_due_wait(execution_id=eid, activity_id="act_c4", shadow_id="sh_c4", attempt=0, now=now)
    _seed_snapshot(data_dir, execution_id=eid, activity_id="act_c4", shadow_id="sh_c4", waits=[w_early])

    # 2) Operator cancel → the finalizer expires the early wait.
    sup = _bare_supervisor(data_dir)
    payload = {"activity_id": "act_c4", "shadow_id": "sh_c4"}
    sup._handle_result(payload, {"status": "cancelled", "execution_id": eid})

    snap = read_snapshot(eid, data_dir=data_dir)
    assert snap.pending_waits[0]["dispatched"] is True   # early wait expired at cancel

    # 3) A late wait races in right as/after cancel (a retry-arm that lost the race
    #    to the cancel). It lands on disk UNDISPATCHED.
    w_late = _undispatched_due_wait(execution_id=eid, activity_id="act_c4", shadow_id="sh_c4", attempt=1, now=now)
    snap.pending_waits = list(snap.pending_waits) + [w_late]
    write_snapshot(snap, data_dir=data_dir)

    # 4) The reconciler runs with the activity CANCELLED → no resubmit, late wait expired.
    rec_sup = _FakeSupervisor()
    vault = _FakeVault(ActivityStatus.CANCELLED)
    count = external_wait_reconciler(vault=vault, supervisor=rec_sup, data_dir=data_dir, now=now)

    assert count == 0
    assert rec_sup.calls == []           # run was NOT resurrected
    snap2 = read_snapshot(eid, data_dir=data_dir)
    assert all(w["dispatched"] is True for w in snap2.pending_waits)   # both expired
