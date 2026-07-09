"""R-A12a Task 4 — the ``external_wait_reconciler`` daemon tick.

The reconciler is the durable-timer FIRE path: it scans persisted
``ExecutionSnapshot.pending_waits`` (the snapshot files under
``data/audit/exec_*/resume_snapshot.json``), and for every wait that is *due*,
*undispatched*, on a run that is **parked (not live)** and **not cancelled**, it
stamps the wait ``dispatched`` (persisted FIRST, so a crash can never double-submit)
and re-submits the retry via ``supervisor.submit(resume_from_execution_id=..., ...)``.

Concurrency contract (CONC-MAP / DEC-10): the reconciler is the **4th**
``write_snapshot`` caller. It respects the per-execution_id parked-run invariant —
it NEVER writes a snapshot whose run the supervisor reports as live, because the
process-local ``_lock`` gives no cross-process protection. These tests pin that
invariant plus the stamp-before-submit at-most-once semantics.

Style mirrors ``tests/test_harness_grant_reconciler.py`` (real on-disk
ExecutionSnapshot at a tmp data_dir, a fake Supervisor that records calls).
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from systemu.runtime.execution_snapshot import (
    ExecutionSnapshot,
    read_snapshot,
    write_snapshot,
)
from systemu.runtime.pending_waits import make_retry_wait


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures / fakes
# ─────────────────────────────────────────────────────────────────────────────

class _FakeSupervisor:
    """Records ``submit(**kw)`` calls; models the in-process running set so the
    reconciler's parked-run (liveness) check can be exercised.

    ``_running`` / ``_running_lock`` mirror the real ``Supervisor`` fields the
    reconciler consults (supervisor.py:254). ``mark_live(activity_id)`` populates
    a running slot exactly as the dispatcher does (payload carries activity_id).
    """

    def __init__(self):
        self.calls: list[dict] = []
        self._running: dict[str, dict] = {}
        self._running_lock = threading.Lock()
        self._pending_activity_ids: set[str] = set()
        self._pending_lock = threading.Lock()
        # optional hook: a callable invoked at submit time (for ordering tests)
        self._on_submit = None

    def mark_live(self, activity_id: str) -> None:
        self._running[f"{activity_id}_sub"] = {"payload": {"activity_id": activity_id}}

    def submit(self, activity_id, shadow_id, **kw):
        rec = {"activity_id": activity_id, "shadow_id": shadow_id, **kw}
        if self._on_submit is not None:
            self._on_submit(rec)
        self.calls.append(rec)
        return f"sub_{len(self.calls)}"


class _FakeVault:
    """Minimal vault exposing ``get_activity`` for the CANCELLED check."""

    def __init__(self, statuses: dict | None = None):
        self._statuses = statuses or {}

    def get_activity(self, activity_id):
        from systemu.core.models import ActivityStatus
        st = self._statuses.get(activity_id, ActivityStatus.ASSIGNED)
        return SimpleNamespace(id=activity_id, status=st)


def _seed(data_dir, *, execution_id, activity_id="a1", shadow_id="s1", waits=None):
    """Write an ExecutionSnapshot carrying ``waits`` at ``data_dir``."""
    write_snapshot(
        ExecutionSnapshot(
            execution_id=execution_id,
            shadow_id=shadow_id,
            scroll_id="scr1",
            activity_id=activity_id,
            pending_waits=list(waits or []),
        ),
        data_dir=data_dir,
    )


def _due_wait(now, *, execution_id="e1", activity_id="a1", shadow_id="s1",
              attempt=1, max_attempts=5, age=10.0):
    """A wait that is DUE (fire_at just past ``now``), recent (not stale),
    not exhausted."""
    return make_retry_wait(
        execution_id=execution_id, activity_id=activity_id, shadow_id=shadow_id,
        root_execution_id=execution_id, delay_s=0.0, attempt=attempt,
        max_attempts=max_attempts, now=now - age,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_due_undispatched_wait_on_parked_run_dispatches_and_stamps(tmp_path):
    from systemu.scheduler.jobs import external_wait_reconciler
    data_dir = tmp_path / "data"
    now = 1_000_000.0
    w = _due_wait(now, execution_id="e1", activity_id="a1", shadow_id="s1", attempt=0)
    _seed(data_dir, execution_id="e1", activity_id="a1", shadow_id="s1", waits=[w])

    sup = _FakeSupervisor()
    vault = _FakeVault()
    count = external_wait_reconciler(vault=vault, supervisor=sup, data_dir=data_dir, now=now)

    assert count == 1
    assert len(sup.calls) == 1
    call = sup.calls[0]
    assert call["activity_id"] == "a1"
    assert call["shadow_id"] == "s1"
    assert call["resume_from_execution_id"] == "e1"
    # The resubmit ADVANCES the attempt: a wait carrying the failed run's attempt=0
    # is replayed at retry_count=1 (attempt+1), matching the old threading.Timer's
    # retry_count+1 so the retry chain terminates at MAX_RETRIES rather than looping.
    assert call["retry_count"] == 1
    # persisted: the wait is now stamped dispatched
    snap = read_snapshot("e1", data_dir=data_dir)
    assert snap.pending_waits[0]["dispatched"] is True


def test_not_yet_due_skipped(tmp_path):
    from systemu.scheduler.jobs import external_wait_reconciler
    data_dir = tmp_path / "data"
    now = 1_000_000.0
    # fire_at = now + 100 (not due)
    w = make_retry_wait(execution_id="e1", activity_id="a1", shadow_id="s1",
                        root_execution_id="e1", delay_s=100.0, attempt=1,
                        max_attempts=5, now=now)
    _seed(data_dir, execution_id="e1", waits=[w])

    sup = _FakeSupervisor()
    count = external_wait_reconciler(vault=_FakeVault(), supervisor=sup,
                                     data_dir=data_dir, now=now)

    assert count == 0
    assert sup.calls == []
    snap = read_snapshot("e1", data_dir=data_dir)
    assert snap.pending_waits[0]["dispatched"] is False


def test_already_dispatched_skipped(tmp_path):
    from systemu.scheduler.jobs import external_wait_reconciler
    data_dir = tmp_path / "data"
    now = 1_000_000.0
    w = _due_wait(now)
    w["dispatched"] = True   # already fired on a prior tick / before a restart
    _seed(data_dir, execution_id="e1", waits=[w])

    sup = _FakeSupervisor()
    count = external_wait_reconciler(vault=_FakeVault(), supervisor=sup,
                                     data_dir=data_dir, now=now)

    assert count == 0
    assert sup.calls == []


def test_live_run_wait_not_touched(tmp_path):
    """The per-execution_id parked invariant: a run the supervisor reports RUNNING
    is skipped entirely — its snapshot is NEVER written by the reconciler."""
    from systemu.scheduler.jobs import external_wait_reconciler
    data_dir = tmp_path / "data"
    now = 1_000_000.0
    w = _due_wait(now, activity_id="a1")
    _seed(data_dir, execution_id="e1", activity_id="a1", waits=[w])

    sup = _FakeSupervisor()
    sup.mark_live("a1")   # the shadow loop is actively executing this run

    count = external_wait_reconciler(vault=_FakeVault(), supervisor=sup,
                                     data_dir=data_dir, now=now)

    assert count == 0
    assert sup.calls == []
    # snapshot NOT written → the due wait is still undispatched (untouched)
    snap = read_snapshot("e1", data_dir=data_dir)
    assert snap.pending_waits[0]["dispatched"] is False


def test_cancelled_run_wait_is_noop(tmp_path):
    """IMPL-11: a wait on a CANCELLED run does not resubmit; it is expired
    (stamped dispatched) so it can never fire."""
    from systemu.core.models import ActivityStatus
    from systemu.scheduler.jobs import external_wait_reconciler
    data_dir = tmp_path / "data"
    now = 1_000_000.0
    w = _due_wait(now, activity_id="a1")
    _seed(data_dir, execution_id="e1", activity_id="a1", waits=[w])

    sup = _FakeSupervisor()
    vault = _FakeVault({"a1": ActivityStatus.CANCELLED})

    count = external_wait_reconciler(vault=vault, supervisor=sup,
                                     data_dir=data_dir, now=now)

    assert count == 0
    assert sup.calls == []
    snap = read_snapshot("e1", data_dir=data_dir)
    assert snap.pending_waits[0]["dispatched"] is True   # expired, not resubmitted


def test_exhausted_or_stale_wait_dropped(tmp_path):
    """attempt>=max_attempts OR created_at older than the staleness bound → dropped
    (stamped dispatched) with NO resubmit and no infinite loop."""
    from systemu.scheduler.jobs import external_wait_reconciler, EXTERNAL_WAIT_STALE_SECONDS
    data_dir = tmp_path / "data"
    now = 1_000_000.0

    # exhausted: attempt == max_attempts (due)
    exhausted = _due_wait(now, execution_id="e1", activity_id="a1",
                          attempt=5, max_attempts=5)
    _seed(data_dir, execution_id="e1", activity_id="a1", waits=[exhausted])

    # stale: created_at far older than the bound (also due)
    stale = _due_wait(now, execution_id="e2", activity_id="a2",
                      attempt=1, max_attempts=5, age=EXTERNAL_WAIT_STALE_SECONDS + 3600.0)
    _seed(data_dir, execution_id="e2", activity_id="a2", waits=[stale])

    sup = _FakeSupervisor()
    count = external_wait_reconciler(vault=_FakeVault(), supervisor=sup,
                                     data_dir=data_dir, now=now)

    assert count == 0
    assert sup.calls == []
    assert read_snapshot("e1", data_dir=data_dir).pending_waits[0]["dispatched"] is True
    assert read_snapshot("e2", data_dir=data_dir).pending_waits[0]["dispatched"] is True


def test_reconciler_defensive_on_corrupt_snapshot(tmp_path):
    """A corrupt snapshot file is skipped; the tick still returns a count for the
    good runs and never raises."""
    from systemu.scheduler.jobs import external_wait_reconciler
    data_dir = tmp_path / "data"
    now = 1_000_000.0

    # a good run with a due wait
    w = _due_wait(now, execution_id="egood", activity_id="a1")
    _seed(data_dir, execution_id="egood", activity_id="a1", waits=[w])

    # a corrupt snapshot file alongside it
    bad_dir = data_dir / "audit" / "exec_ebad"
    bad_dir.mkdir(parents=True, exist_ok=True)
    (bad_dir / "resume_snapshot.json").write_text("{ this is not json", encoding="utf-8")

    sup = _FakeSupervisor()
    count = external_wait_reconciler(vault=_FakeVault(), supervisor=sup,
                                     data_dir=data_dir, now=now)

    assert count == 1               # the good run dispatched
    assert len(sup.calls) == 1
    assert sup.calls[0]["resume_from_execution_id"] == "egood"


def test_stamp_before_submit_idempotency(tmp_path):
    """``dispatched`` is persisted to disk BEFORE ``supervisor.submit`` runs, so a
    crash after the stamp can never double-submit across ticks/restarts."""
    from systemu.scheduler.jobs import external_wait_reconciler
    data_dir = tmp_path / "data"
    now = 1_000_000.0
    w = _due_wait(now, execution_id="e1", activity_id="a1", shadow_id="s1")
    wait_id = w["wait_id"]
    _seed(data_dir, execution_id="e1", activity_id="a1", shadow_id="s1", waits=[w])

    seen = {}

    def _at_submit(rec):
        # read the ON-DISK snapshot at the moment submit is invoked
        snap = read_snapshot(rec["resume_from_execution_id"], data_dir=data_dir)
        match = next(x for x in snap.pending_waits if x["wait_id"] == wait_id)
        seen["dispatched_on_disk"] = match["dispatched"]

    sup = _FakeSupervisor()
    sup._on_submit = _at_submit
    external_wait_reconciler(vault=_FakeVault(), supervisor=sup,
                             data_dir=data_dir, now=now)

    # the stamp was already durable before submit fired
    assert seen["dispatched_on_disk"] is True

    # a second tick (simulating a restart right after the stamp) does NOT re-submit
    sup2 = _FakeSupervisor()
    count2 = external_wait_reconciler(vault=_FakeVault(), supervisor=sup2,
                                      data_dir=data_dir, now=now)
    assert count2 == 0
    assert sup2.calls == []


def test_reconciler_advances_attempt_so_retries_terminate(tmp_path):
    """TERMINATION INVARIANT (the coverage that would have caught the infinite-loop
    bug): every reconciler-fired retry resubmits at ``attempt + 1`` — strictly
    greater than the wait's attempt — so the retry chain advances toward MAX_RETRIES
    and cannot loop forever at the same attempt. Combined with the supervisor's
    arm-condition (``retry_count < MAX_RETRIES``, pinned by
    test_ra12a_supervisor_durable_retry::test_retry_at_max_attempts_does_not_arm),
    this bounds the chain: fail@0 -> submit rc=1 -> fail@1 -> submit rc=2 -> fail@2
    -> NOT re-armed -> dead-letter."""
    from systemu.scheduler.jobs import external_wait_reconciler
    now = 2_000_000.0
    for attempt in (0, 1):
        data_dir = tmp_path / f"data{attempt}"
        w = _due_wait(now, execution_id=f"e{attempt}", activity_id=f"a{attempt}",
                      shadow_id=f"s{attempt}", attempt=attempt)
        _seed(data_dir, execution_id=f"e{attempt}", activity_id=f"a{attempt}",
              shadow_id=f"s{attempt}", waits=[w])
        sup = _FakeSupervisor()
        external_wait_reconciler(vault=_FakeVault(), supervisor=sup,
                                 data_dir=data_dir, now=now)
        assert len(sup.calls) == 1
        rc = sup.calls[0]["retry_count"]
        assert rc == attempt + 1
        assert rc > attempt        # strictly advances — never re-runs the same attempt
