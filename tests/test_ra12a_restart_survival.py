"""R-A12a headline acceptance test — a durable retry wait SURVIVES a daemon restart.

This is the END-TO-END AC for R-A12a and the whole point of the feature. The old
retry path armed an in-process ``threading.Timer(wait_s, submit)``: the scheduled
resubmit lived only in the daemon process's memory, so a restart during the 5–10 s
back-off window silently dropped it and the transiently-failed activity was never
retried. R-A12a replaces the Timer with a ``pending_wait`` record persisted on the
run's ExecutionSnapshot; a separate reconciler fires it after the restart.

Here we compose the two REAL units across a *simulated* daemon restart:

  1. ARM (pre-restart)   — the REAL supervisor retry path
     (``Supervisor._handle_result`` → ``_arm_durable_retry``, driven with the same
     bare-supervisor harness + ``_snapshot_data_dir`` seam as
     ``tests/test_ra12a_supervisor_durable_retry.py``) persists an undispatched
     ``wait_kind=="retry"`` record to a snapshot on disk.
  2. RESTART             — ALL in-process state is discarded (the whole supervisor
     object is dropped) and a FRESH supervisor stub + fresh vault handle are built
     pointing at the SAME data_dir. There is NO ``threading.Timer`` in the "new
     process"; the only durable trace of the pending retry is the on-disk snapshot.
  3. FIRE (post-restart) — the REAL reconciler
     (``external_wait_reconciler``, whose fakes mirror
     ``tests/test_ra12a_external_wait_reconciler.py``) reads the snapshot, fires the
     due wait through the fresh supervisor's ``submit``, stamps it ``dispatched``,
     and a second tick does NOT re-submit (idempotent across restart + repeated ticks).

Integration alignment (the critical check): the ARM site persists via
``write_snapshot`` → ``<data_dir>/audit/exec_<eid>/resume_snapshot.json`` and the
reconciler scans ``<data_dir>/audit/exec_*`` — the SAME tree. Both default to
``./data`` in production; here both are pinned to ``tmp_path``. The reconciler
replays with ``retry_count == wait["attempt"] + 1`` (it ADVANCES the attempt,
matching the old ``threading.Timer``'s ``retry_count+1`` so the retry chain
terminates at ``MAX_RETRIES`` — see ``test_ra12a_external_wait_reconciler.py``).
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

from systemu.runtime.supervisor import Supervisor, MAX_RETRIES
from systemu.runtime.execution_snapshot import read_snapshot
from systemu.scheduler.jobs import external_wait_reconciler


# ─────────────────────────────────────────────────────────────────────────────
# ARM side — the REAL supervisor retry path with snapshot I/O redirected at a tmp
# data_dir via the ``_snapshot_data_dir`` seam. Mirrors ``_bare_supervisor`` in
# tests/test_ra12a_supervisor_durable_retry.py (only enough of __init__ to reach
# the retry-vs-dead-letter decision in ``_handle_result``).
# ─────────────────────────────────────────────────────────────────────────────

def _bare_supervisor(data_dir):
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


# ─────────────────────────────────────────────────────────────────────────────
# FIRE side — handles built AFTER the "restart". No in-process state from the ARM
# survives. Mirrors ``_FakeSupervisor`` / ``_FakeVault`` in
# tests/test_ra12a_external_wait_reconciler.py.
# ─────────────────────────────────────────────────────────────────────────────

class _FreshSupervisor:
    """A brand-new supervisor stub, as if minted by a freshly-restarted daemon —
    it records ``submit`` calls and models the (empty) in-process running set the
    reconciler's parked-run check consults."""

    def __init__(self):
        self.calls: list[dict] = []
        self._running: dict[str, dict] = {}
        self._running_lock = threading.Lock()
        self._pending_activity_ids: set[str] = set()
        self._pending_lock = threading.Lock()

    def submit(self, activity_id, shadow_id, **kw):
        self.calls.append({"activity_id": activity_id, "shadow_id": shadow_id, **kw})
        return f"sub_{len(self.calls)}"


class _FreshVault:
    """A fresh vault handle post-restart; an activity defaults to ASSIGNED (i.e.
    NOT cancelled), so the reconciler treats the run as a parked retry."""

    def __init__(self, statuses: dict | None = None):
        self._statuses = statuses or {}

    def get_activity(self, activity_id):
        from systemu.core.models import ActivityStatus
        st = self._statuses.get(activity_id, ActivityStatus.ASSIGNED)
        return SimpleNamespace(id=activity_id, status=st)


# ─────────────────────────────────────────────────────────────────────────────
# The acceptance test.
# ─────────────────────────────────────────────────────────────────────────────

def test_durable_retry_survives_restart(tmp_path):
    # Both the arm site and the reconciler are pinned to this ONE tree, exactly as
    # production pins both to ``./data`` (arm: _snapshot_data_dir default None →
    # write_snapshot's "data"; reconciler: daemon passes Path("data")).
    data_dir = tmp_path

    # ── 1) ARM (pre-restart): drive the REAL supervisor retry path. ─────────────
    arm_sup = _bare_supervisor(data_dir)
    # Any synchronous resubmit would be a bug — the durable wait defers the resubmit
    # to the reconciler. Record so we can assert nothing fired synchronously.
    sync_submits: list = []
    arm_sup.submit = lambda **kw: sync_submits.append(kw)

    payload = {"activity_id": "act_e2e", "shadow_id": "sh_e2e",
               "retry_count": 0, "origin": "chat", "priority": 5}
    result = {"status": "failure", "error": "transient boom",
              "execution_id": "exec_e2e"}
    arm_sup._handle_result(payload, result)

    assert sync_submits == []   # nothing fired synchronously at arm time

    # The pending retry is now PERSISTED to disk — the ONLY durable trace of it.
    armed = read_snapshot("exec_e2e", data_dir=data_dir)
    assert armed is not None
    assert len(armed.pending_waits) == 1
    w = armed.pending_waits[0]
    assert w["wait_kind"] == "retry"
    assert w["activity_id"] == "act_e2e"
    assert w["shadow_id"] == "sh_e2e"
    assert w["execution_id"] == "exec_e2e"
    assert w["attempt"] == 0
    assert w["max_attempts"] == MAX_RETRIES
    assert w["dispatched"] is False
    fire_at = w["fire_at"]

    # ── 2) RESTART: discard ALL in-process state; fresh handles, SAME data_dir. ──
    # The entire supervisor object (and any in-memory retry state) is dropped, as
    # if the daemon process exited. Nothing but the on-disk snapshot carries over.
    del arm_sup
    fresh_sup = _FreshSupervisor()
    fresh_vault = _FreshVault()

    # ── 3) FIRE (post-restart): advance ``now`` past fire_at and run the REAL ────
    # reconciler using ONLY (data_dir + fresh handles). No timer/callback survived.
    count = external_wait_reconciler(
        vault=fresh_vault, supervisor=fresh_sup, data_dir=data_dir, now=fire_at + 1,
    )

    assert count == 1
    assert len(fresh_sup.calls) == 1
    call = fresh_sup.calls[0]
    assert call["activity_id"] == "act_e2e"
    assert call["shadow_id"] == "sh_e2e"
    assert call["resume_from_execution_id"] == "exec_e2e"
    # The reconciler ADVANCES the attempt: retry_count == the wait's attempt + 1
    # (matching the old threading.Timer's retry_count+1 so the chain terminates at
    # MAX_RETRIES — see test_ra12a_external_wait_reconciler.py's identical assertion).
    assert call["retry_count"] == w["attempt"] + 1

    # The dispatched stamp is DURABLE — persisted on disk, not just in memory.
    after = read_snapshot("exec_e2e", data_dir=data_dir)
    assert after.pending_waits[0]["dispatched"] is True

    # A SECOND tick (a further restart / repeated poll) does NOT re-submit —
    # idempotent across the "restart" plus repeated ticks.
    fresh_sup2 = _FreshSupervisor()
    count2 = external_wait_reconciler(
        vault=_FreshVault(), supervisor=fresh_sup2, data_dir=data_dir, now=fire_at + 1,
    )
    assert count2 == 0
    assert fresh_sup2.calls == []


def test_pre_restart_timer_would_have_been_lost(tmp_path, monkeypatch):
    """The contrast that makes the AC meaningful: the DURABLE on-disk record — not
    an in-process timer — is what carries the retry across a restart.

    The old path armed a ``threading.Timer`` whose scheduled resubmit a restart
    silently dropped. The new arm path constructs NO ``threading.Timer`` at all, so
    there is nothing in-memory for a restart to lose; the reconciler recovers the
    retry using ONLY (data_dir + fresh handles) — no timer object, no callback, no
    carried-over supervisor state.
    """
    import systemu.runtime.supervisor as sup_mod

    class _TimerSpy:
        """Records every ``threading.Timer`` the arm path would have constructed."""
        instances: list = []

        def __init__(self, *a, **kw):
            _TimerSpy.instances.append(self)

        def start(self):  # pragma: no cover - never reached (no timer is armed)
            pass

    _TimerSpy.instances = []
    monkeypatch.setattr(sup_mod.threading, "Timer", _TimerSpy)

    data_dir = tmp_path

    # ── ARM ─────────────────────────────────────────────────────────────────────
    arm_sup = _bare_supervisor(data_dir)
    monkeypatch.setattr(arm_sup, "submit", lambda **kw: None)

    payload = {"activity_id": "act_lost", "shadow_id": "sh_lost", "retry_count": 1}
    result = {"status": "failure", "error": "boom", "execution_id": "exec_lost"}
    arm_sup._handle_result(payload, result)

    # NOT a single in-process timer was constructed — a restart has nothing in
    # memory to drop; the retry lives entirely on disk.
    assert _TimerSpy.instances == []
    snap = read_snapshot("exec_lost", data_dir=data_dir)
    assert snap is not None
    assert len(snap.pending_waits) == 1
    fire_at = snap.pending_waits[0]["fire_at"]

    # ── RESTART + FIRE: the reconciler's ONLY inputs are data_dir + fresh handles. ─
    del arm_sup
    fresh_sup = _FreshSupervisor()
    count = external_wait_reconciler(
        vault=_FreshVault(), supervisor=fresh_sup, data_dir=data_dir, now=fire_at + 1,
    )

    assert count == 1
    assert len(fresh_sup.calls) == 1
    assert fresh_sup.calls[0]["activity_id"] == "act_lost"
    assert fresh_sup.calls[0]["resume_from_execution_id"] == "exec_lost"
