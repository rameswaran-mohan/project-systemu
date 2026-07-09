"""R-A12a (concurrency fix 1) — the reconciler must DROP a due wait whose
activity already reached a terminal, non-cancelled state.

Scenario the review confirmed (HIGH, spurious duplicate execution): activity A
fails at attempt 0 and arms a durable retry wait W (fire_at = now + 5 s). Before W
fires, A is re-run by ANOTHER path (the hourly sweep / startup_recovery_sweep / an
operator manual re-run) and **SUCCEEDS** (``mark_activity_completed`` → COMPLETED)
— or is dead-lettered (``mark_activity_failed`` → FAILED). Neither branch expires
pending_waits (only CANCELLED does, via ``_expire_pending_waits_on_cancel``), so W
stays undispatched. The old reconciler only skipped LIVE or CANCELLED runs, so on
the next tick it stamped W ``dispatched`` and called ``supervisor.submit(...)``,
**re-executing an already-finished activity and replaying its effectful actions.**

STEP-0 status facts (verified against ``systemu/runtime/supervisor.py`` +
``activity_completion.py``): the retry path leaves the ACTIVITY non-terminal —
comment at supervisor.py ~:1455 "The activity is NOT dead-lettered / marked
terminally failed here — it stays non-terminal (ASSIGNED)". So the terminal
drop-set is exactly ``{COMPLETED, FAILED, CANCELLED}`` (all distinct from the
retry-pending ASSIGNED state); a legitimately-retry-pending (ASSIGNED) activity is
NOT dropped and its retry still fires.

Reuses the on-disk ExecutionSnapshot + fake Supervisor/Vault harness from
``tests/test_ra12a_external_wait_reconciler.py``.
"""
from __future__ import annotations

from systemu.core.models import ActivityStatus
from systemu.runtime.execution_snapshot import read_snapshot

# Reuse the exact harness the sibling reconciler test pins the invariant with.
from test_ra12a_external_wait_reconciler import (
    _FakeSupervisor,
    _FakeVault,
    _seed,
    _due_wait,
)


def test_completed_activity_wait_dropped_no_submit(tmp_path):
    """A due, undispatched wait whose activity is COMPLETED (a concurrent re-run
    already succeeded) is DROPPED — stamped dispatched, NO supervisor.submit — so
    the finished activity is not re-executed and its effects are not replayed."""
    from systemu.scheduler.jobs import external_wait_reconciler
    data_dir = tmp_path / "data"
    now = 1_000_000.0
    w = _due_wait(now, execution_id="e1", activity_id="a1", shadow_id="s1", attempt=1)
    _seed(data_dir, execution_id="e1", activity_id="a1", shadow_id="s1", waits=[w])

    sup = _FakeSupervisor()
    vault = _FakeVault({"a1": ActivityStatus.COMPLETED})

    count = external_wait_reconciler(vault=vault, supervisor=sup,
                                     data_dir=data_dir, now=now)

    assert count == 0
    assert sup.calls == []
    snap = read_snapshot("e1", data_dir=data_dir)
    assert snap.pending_waits[0]["dispatched"] is True   # dropped, not resubmitted


def test_dead_lettered_failed_activity_wait_dropped_no_submit(tmp_path):
    """A due, undispatched wait whose activity is terminal-FAILED (dead-lettered by
    another path) is DROPPED — no resubmit — the retry is moot, the activity already
    reached a terminal outcome."""
    from systemu.scheduler.jobs import external_wait_reconciler
    data_dir = tmp_path / "data"
    now = 1_000_000.0
    w = _due_wait(now, execution_id="e2", activity_id="a2", shadow_id="s2", attempt=1)
    _seed(data_dir, execution_id="e2", activity_id="a2", shadow_id="s2", waits=[w])

    sup = _FakeSupervisor()
    vault = _FakeVault({"a2": ActivityStatus.FAILED})

    count = external_wait_reconciler(vault=vault, supervisor=sup,
                                     data_dir=data_dir, now=now)

    assert count == 0
    assert sup.calls == []
    snap = read_snapshot("e2", data_dir=data_dir)
    assert snap.pending_waits[0]["dispatched"] is True


def test_retry_pending_assigned_activity_still_submits(tmp_path):
    """OVER-DROP GUARD: a due wait whose activity is legitimately retry-pending —
    ASSIGNED (the non-terminal state the supervisor's retry path leaves it in) and
    NOT live — MUST still fire. The terminal-drop must exclude ASSIGNED or it would
    silently kill every legitimate durable retry."""
    from systemu.scheduler.jobs import external_wait_reconciler
    data_dir = tmp_path / "data"
    now = 1_000_000.0
    w = _due_wait(now, execution_id="e3", activity_id="a3", shadow_id="s3", attempt=1)
    _seed(data_dir, execution_id="e3", activity_id="a3", shadow_id="s3", waits=[w])

    sup = _FakeSupervisor()
    # ASSIGNED is the _FakeVault default; make it explicit for intent.
    vault = _FakeVault({"a3": ActivityStatus.ASSIGNED})

    count = external_wait_reconciler(vault=vault, supervisor=sup,
                                     data_dir=data_dir, now=now)

    assert count == 1
    assert len(sup.calls) == 1
    call = sup.calls[0]
    assert call["activity_id"] == "a3"
    assert call["shadow_id"] == "s3"
    assert call["resume_from_execution_id"] == "e3"
    assert call["retry_count"] == 2   # attempt(1) + 1
    snap = read_snapshot("e3", data_dir=data_dir)
    assert snap.pending_waits[0]["dispatched"] is True
