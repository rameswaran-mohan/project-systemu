"""D-3 (e2e 0.10.31): the health banner walked EVERY process on the machine
during page render.

THE WITNESS
-----------
0.10.31 removed the provider socket probe from the render path (D4) and the
renderer kept stalling: six 30 s ``Page.captureScreenshot`` timeouts on
/welcome and /chat, DOM reads fine throughout, a click by ref dropped while the
same click by coordinate worked seconds later, and a control page on another
origin that never stalled. So the browser was fine and the SERVER's event loop
was not.

Profiled, ``_build_layout("Home", "/")`` with the scan's 20 s TTL expired::

    2/1  0.001  1.982  interface/dashboard.py:327(_build_layout)
    2/1  0.000  1.626  components/health_banner.py:814(render_health_banner)
      1  0.000  1.622  components/health_banner.py:487(_count_systemu_daemons)
    358  0.004  1.613  psutil/__init__.py:1460(process_iter)
    158  1.480  1.480  {built-in method psutil_windows.proc_info}

Timed on its own the scan is 695-1192 ms cold. The TTL in front of it is the
D4 defect verbatim, one layer down: it bounds how OFTEN the machine-wide walk
is paid, not WHERE, so every 20 s one page paint froze the loop for over a
second.

THE PROPERTY
------------
    NO PROCESS SCAN RUNS ON THE RENDER PATH.

    ``_scan_daemon_processes`` stays the one scan and is unchanged in what it
    establishes. It now runs on the shared refresher thread and PUBLISHES a
    ``DaemonScanSnapshot``; the render reads that snapshot. With no snapshot
    the render reports NO daemon records, which can only withhold the
    multi-daemon warning -- never invent a port race.

    Measured: the scan takes 1251 ms on a worker thread while a 10 ms asyncio
    heartbeat on the main loop drifts by a median of 22 ms and a worst case of
    36 ms (idle baseline 6 / 8 ms). psutil releases the GIL for the Win32 calls
    that dominate, so the freeze becomes a hiccup.

THE PIN
-------
``psutil.process_iter`` is replaced with a fake that COUNTS and RAISES. The
scan swallows every exception, so counting the ATTEMPT is the only assertion
the defect cannot hide from -- the same shape as D4's socket tripwire.
"""
from __future__ import annotations

import sys
import time
from types import ModuleType, SimpleNamespace

import pytest

from systemu.interface.components import health_banner as hb
from systemu.runtime import render_snapshots as rs


# ── the process-scan tripwire ───────────────────────────────────────────────

class _ScanTouched(RuntimeError):
    """Raised by the machine-wide walk the render must never reach."""


@pytest.fixture()
def no_process_scan(monkeypatch):
    """``psutil.process_iter`` raises AND is counted. Returns the counter."""
    import psutil

    touched = []

    def _trip(*a, **k):
        touched.append(a)
        raise _ScanTouched("the render path walked every process")

    monkeypatch.setattr(psutil, "process_iter", _trip)
    return touched


@pytest.fixture()
def quiet_banner(monkeypatch):
    """Everything the banner touches EXCEPT the daemon scan, held still."""
    from systemu.runtime import provider_snapshot as snap
    from systemu.runtime import provider_status as ps

    snap.stop_refresher()
    snap.clear()
    rs.stop_refresher()
    rs.clear()
    monkeypatch.setattr(hb, "_provider_view", lambda *a, **k: ({}, True, None))
    monkeypatch.setattr(hb, "_lockout_degraded", lambda: ())
    monkeypatch.setattr(hb, "_storage_degraded", lambda: None)
    monkeypatch.setattr(ps, "any_satisfied", lambda *a, **k: True)
    # Neither the TTL wrapper in front of the count nor the scan's own cache
    # may answer from a value an earlier test left behind -- otherwise a test
    # that should have reached the scan silently does not.
    hb._daemon_probe_cache["ts"] = -1e9
    hb._daemon_probe_cache["count"] = 0
    hb._daemon_procs_cache["ts"] = -1e9
    yield
    rs.stop_refresher()
    rs.clear()
    snap.stop_refresher()
    snap.clear()


def _daemon_issues(state):
    return [i for i in state.issues if "systemu daemon processes" in i.message]


def _proc(pid, port, **kw):
    return hb.DaemonProcess(pid=pid, port=port, vault=kw.get("vault", "V"),
                            cwd=kw.get("cwd", "C"), exe=kw.get("exe", "py"),
                            is_self=kw.get("is_self", False))


def _publish(procs, *, age_s=0.0, interval_s=30.0):
    rs.publish(rs.DAEMON_SCAN, rs.DaemonScanSnapshot(
        taken_at=time.monotonic() - age_s, interval_s=interval_s,
        procs=tuple(procs)))


# ── a recording NiceGUI, so the REAL renderer can be executed ───────────────

class _Node:
    def __init__(self):
        self.value = ""

    def __getattr__(self, name):
        def _m(*a, **k):
            return self
        return _m

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Refreshable:
    def __init__(self, fn):
        self._fn = fn

    def __call__(self, *a, **k):
        return self._fn(*a, **k)

    def refresh(self, *a, **k):
        return self._fn(*a, **k)


class _RecordingUI:
    def __init__(self):
        self.calls = []
        self.timers = []
        self.navigate = SimpleNamespace(to=lambda *a, **k: None)

    def refreshable(self, fn):
        return _Refreshable(fn)

    def timer(self, _delay, callback=None, **k):
        if callback is not None:
            self.timers.append(callback)
        return _Node()

    def __getattr__(self, name):
        def _call(*a, **k):
            self.calls.append((name, a, k))
            return _Node()
        return _call

    def labels(self):
        return [str(a[0]) for n, a, _k in self.calls if n == "label" and a]


def _render(monkeypatch, vault_dir=None) -> _RecordingUI:
    """Execute the REAL ``render_health_banner`` against a recording ``ui``.

    Both refreshers are RECORDED, never started: a live one would run the scan
    on another thread and trip this file's tripwire non-deterministically.
    """
    from systemu.interface import dashboard_state             # noqa: F401
    from systemu.runtime import provider_snapshot as snap
    rec = _RecordingUI()
    rec.started = []
    monkeypatch.setattr(snap, "start_refresher",
                        lambda *a, **k: rec.started.append(("provider",)) or True)
    monkeypatch.setattr(rs, "start_refresher",
                        lambda *a, **k: rec.started.append(("render",)) or True)
    fake = ModuleType("nicegui")
    fake.ui = rec
    monkeypatch.setitem(sys.modules, "nicegui", fake)
    hb.render_health_banner(vault_dir)
    return rec


# ── (a) THE REACHABILITY PIN ────────────────────────────────────────────────

def test_build_health_state_walks_no_processes(no_process_scan, quiet_banner):
    """THE REPRO. Before the fix this reaches psutil.process_iter."""
    state = hb.build_health_state(vault_dir=None)
    assert no_process_scan == [], (
        "build_health_state walked every process on the render path: "
        + repr(len(no_process_scan)) + " calls")
    assert state is not None


def test_render_health_banner_walks_no_processes(monkeypatch, no_process_scan,
                                                 quiet_banner):
    """The whole captured stack, executed: _build_layout's actual call."""
    _render(monkeypatch)
    assert no_process_scan == [], (
        "render_health_banner walked every process: " + repr(no_process_scan))


def test_a_raising_process_iter_does_not_break_the_render(monkeypatch,
                                                          no_process_scan,
                                                          quiet_banner):
    """The weaker half of the pin, kept because it is what an operator sees:
    a machine whose process table cannot be read still gets a page."""
    rec = _render(monkeypatch)
    assert rec is not None


def test_the_count_wrapper_walks_no_processes(no_process_scan, quiet_banner):
    """``_count_systemu_daemons`` is the frame the profile named. Its TTL is
    expired here, so before the fix this is exactly where the scan happened."""
    assert hb._count_systemu_daemons() == 0
    assert no_process_scan == [], no_process_scan


# ── (b) with no snapshot the banner WITHHOLDS, it does not invent ───────────

def test_no_snapshot_yields_no_daemon_records(no_process_scan, quiet_banner):
    rs.clear(rs.DAEMON_SCAN)
    assert hb._daemon_processes() == ()
    assert no_process_scan == []


def test_no_snapshot_raises_no_multi_daemon_issue(no_process_scan, quiet_banner):
    """It can only ever fail to warn. Inventing a port race off an absent
    observation is the failure this direction is chosen to prevent."""
    rs.clear(rs.DAEMON_SCAN)
    assert _daemon_issues(hb.build_health_state(vault_dir=None)) == []


def test_the_render_nudges_the_scan_it_declined_to_run(monkeypatch,
                                                       no_process_scan,
                                                       quiet_banner):
    """Declining to scan is only honest if something else will."""
    rs.clear(rs.DAEMON_SCAN)
    nudges = []
    monkeypatch.setattr(rs, "request_refresh",
                        lambda *a, **k: nudges.append(1) or False)
    hb._daemon_processes()
    assert nudges, "the render found no snapshot and nudged nobody"


def test_the_render_starts_the_refresher_that_will_scan(monkeypatch,
                                                        no_process_scan,
                                                        quiet_banner):
    rec = _render(monkeypatch)
    assert ("render",) in rec.started, rec.started


# ── (c) the published snapshot IS what the banner reads ────────────────────

def test_a_published_snapshot_reaches_the_diagnosis(no_process_scan,
                                                    quiet_banner):
    _publish([_proc(100, 8765), _proc(200, 8765)])
    hb._daemon_probe_cache["ts"] = -1e9
    issues = _daemon_issues(hb.build_health_state(vault_dir=None))
    assert issues and issues[0].severity == "danger", issues
    assert "2 systemu daemon processes" in issues[0].message
    assert no_process_scan == [], "reading a snapshot must not scan"


def test_a_published_snapshot_carries_its_own_prune_evidence(quiet_banner):
    rs.publish(rs.DAEMON_SCAN, rs.DaemonScanSnapshot(
        taken_at=time.monotonic(), procs=(_proc(100, 8765),),
        pruned=(201, 202), launchers=(1234,)))
    assert set(hb.pruned_daemon_pids()) == {201, 202}
    assert set(hb.excluded_launcher_pids()) == {1234}


def test_a_snapshot_older_than_the_replay_bound_is_withheld(quiet_banner):
    """A dashboard left open beside a refresher that died must not answer from
    an observation nobody has refreshed in ten minutes."""
    _publish([_proc(100, 8765), _proc(200, 8765)],
             age_s=rs.MAX_REPLAY_AGE_S + 1.0)
    assert hb._daemon_processes() == ()


def test_a_stale_snapshot_is_still_shown_and_nudged(monkeypatch, quiet_banner):
    """Past the interval but inside the replay bound: the records are the best
    thing known, so they are used AND a refresh is asked for."""
    nudges = []
    monkeypatch.setattr(rs, "request_refresh",
                        lambda *a, **k: nudges.append(1) or False)
    _publish([_proc(100, 8765)], age_s=45.0, interval_s=30.0)
    assert [d.pid for d in hb._daemon_processes()] == [100]
    assert nudges, "a stale snapshot must ask to be refreshed"


# ── (d) the scan still works, on the refresher ─────────────────────────────

def test_running_the_scan_publishes_what_it_found(monkeypatch, quiet_banner):
    """The producer's contract. Whoever runs the scan publishes it, so a caller
    that runs it directly leaves the store holding the freshest observation."""
    import psutil
    monkeypatch.setattr(psutil, "process_iter", lambda *a, **k: [])
    rs.clear(rs.DAEMON_SCAN)
    hb._scan_daemon_processes()
    snap = rs.daemon_scan()
    assert snap is not None and snap.procs == (), snap


def test_a_scan_that_blows_up_publishes_an_empty_observation(monkeypatch,
                                                             quiet_banner):
    """Fail CLOSED, and VISIBLY: a scan that could not read the process table
    publishes 'no records', never leaves a previous count standing as if it
    had just been re-observed."""
    import psutil

    def _boom(*a, **k):
        raise RuntimeError("process table unreadable")

    _publish([_proc(100, 8765), _proc(200, 8765)])
    monkeypatch.setattr(psutil, "process_iter", _boom)
    hb._scan_daemon_processes()
    snap = rs.daemon_scan()
    assert snap is not None and snap.procs == (), snap


def test_the_refresher_runs_the_registered_producer(monkeypatch, quiet_banner):
    ran = []
    rs.register("test_producer", lambda: ran.append(1))
    try:
        assert rs.refresh_now("test_producer") == 1
        assert ran == [1]
    finally:
        rs._producers.pop("test_producer", None)


def test_a_producer_that_raises_does_not_strand_its_sibling(quiet_banner):
    ran = []
    rs.register("test_boom", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    rs.register("test_ok", lambda: ran.append(1))
    try:
        assert rs.refresh_now(None) >= 1
        assert ran == [1]
    finally:
        rs._producers.pop("test_boom", None)
        rs._producers.pop("test_ok", None)


def test_the_refresher_thread_starts_and_stops(quiet_banner):
    ran = []
    rs.register("test_thread", lambda: ran.append(1))
    try:
        assert rs.start_refresher(interval_s=0.05) is True
        assert rs.start_refresher(interval_s=0.05) is False, "not idempotent"
        deadline = time.monotonic() + 5.0
        while not ran and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ran, "the refresher thread never ran a producer"
        assert rs.request_refresh() is True
    finally:
        rs.stop_refresher()
        rs._producers.pop("test_thread", None)
    assert rs.request_refresh() is False, "a stopped refresher must nudge nobody"
