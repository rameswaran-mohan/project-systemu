"""D-3 (e2e 0.10.31): a render-time budget for the shared layout's builders.

WHY A TIMING PIN AND NOT ONLY A REACHABILITY PIN
------------------------------------------------
The two reachability pins beside this one (no process scan, no index rebuild)
each name ONE offender by the call it must not make. They cannot see the third
one, and there had already been three: D4 removed a socket from
``_build_layout`` in 0.10.31 and the renderer went on stalling, because two more
blocking sections were sitting in the same function. A budget is the pin that
notices the NEXT one -- it asserts a property of the whole synchronous section
rather than the absence of a named call.

WHAT IT MEASURES
----------------
The two builders ``_build_layout`` reaches on EVERY route, driven through their
real entry points against a recording ``ui``, on a vault whose reads are all
in-memory. Only the code's own cost is in frame; there is no disk wait to
blame.

THE VAULT IS WARM AND THE MACHINE IS NOT MOCKED. The corpus is 40 tools each
declaring a distinct absent pip distribution, which is the shape a real vault
has and the shape that made ``is_installed`` walk ``sys.path`` forty times per
paint. The process table is the real one -- there is nothing to fake about
"walks every process on the machine", and faking it would remove the very cost
this budget exists to keep out.

THE NUMBERS THIS WAS WRITTEN AGAINST
------------------------------------
This exact file, run against integration/p132 (ba2ec2ba) and against the fixed
tree. Median of 5, daemon-scan TTL expired before each run -- which is the state
one paint in every twenty is in::

    section                            before     after   budget
    render_health_banner               641.6 ms   2.7 ms   120 ms
    build_command_palette              275.3 ms   0.1 ms   120 ms
    both, as _build_layout calls them 1162.0 ms   2.8 ms   200 ms
    the same two, snapshots published    --       2.8 ms   200 ms
    palette_entries (per keystroke)      --       0.0 ms   120 ms

Five of the six below failed on the base tree; the sixth is the fixture pin.
Separately, the full warm ``_build_layout`` against a real 40-tool vault went
272.6 ms -> 12.3 ms, with the 1982 ms cold spike removed.

The budgets below are deliberately several times the measured "after" and
several times BELOW the measured "before": a timing assertion that is tight
enough to flap is a test that gets deleted, and one that is loose enough to
pass the defect is not a pin at all.

MEDIAN, NOT MEAN. One descheduled run must not fail the suite, and one lucky
run must not pass a defect.
"""
from __future__ import annotations

import statistics
import sys
import time
from types import ModuleType, SimpleNamespace

import pytest

from systemu.interface.components import command_palette as cp
from systemu.interface.components import health_banner as hb

#: No single synchronous builder may cost more than this on a warm vault.
#: The R-UX2 goal is 50 ms; this is the pin, set where a regression is
#: unambiguous rather than where a busy CI box is unlucky.
BUDGET_SECTION_MS = 120.0

#: And the shared layout's synchronous builders together.
BUDGET_TOTAL_MS = 200.0

#: How many times each section is run. The median is what is asserted.
_RUNS = 5


# ── a vault whose every read is in memory ───────────────────────────────────

class _WarmVault:
    """40 tools, each declaring its own absent pip distribution.

    Every method answers from a list built at construction time, so a slow
    section is slow because of what the code does, not because of a disk.
    """

    def __init__(self, n: int = 40):
        self._tools = [
            {"name": "tool_%02d" % i,
             "description": "a tool that does thing %d" % i,
             "enabled": True,
             "dependencies": ["systemu-absent-dep-%02d" % i]}
            for i in range(n)
        ]
        self._activities = [
            {"id": "act_%02d" % i, "name": "activity %d" % i, "status": "done"}
            for i in range(n)
        ]

    def list_tools(self):
        return list(self._tools)

    def load_index(self, kind, *a, **k):
        if kind == "activities":
            return list(self._activities)
        return []

    def load_all(self, *a, **k):
        return []

    def __getattr__(self, name):
        def _m(*a, **k):
            return []
        return _m


# ── a recording NiceGUI ─────────────────────────────────────────────────────

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


class _RecordingUI:
    def __init__(self):
        self.navigate = SimpleNamespace(to=lambda *a, **k: None)

    def refreshable(self, fn):
        return fn

    def timer(self, _delay, callback=None, **k):
        return _Node()

    def __getattr__(self, name):
        def _call(*a, **k):
            return _Node()
        return _call


@pytest.fixture()
def recording_ui(monkeypatch):
    fake = ModuleType("nicegui")
    fake.ui = _RecordingUI()
    monkeypatch.setitem(sys.modules, "nicegui", fake)
    return fake.ui


@pytest.fixture()
def warm(monkeypatch, tmp_path):
    """Everything a real first paint has, and nothing it does not.

    The refreshers are RECORDED, not started: a live one would run the very
    producers this budget is about, on another thread, mid-measurement.

    The ``render_snapshots`` half is optional ON PURPOSE. A budget is a claim
    about TIME, and it has to be runnable against a tree that does not have the
    mechanism yet -- otherwise "it was red before" degrades into "it did not
    import before", which is not a measurement of anything.
    """
    from systemu.runtime import provider_snapshot as psnap

    psnap.stop_refresher()
    monkeypatch.setattr(psnap, "start_refresher", lambda *a, **k: True)
    try:
        from systemu.runtime import render_snapshots as rs
    except Exception:
        rs = None
    if rs is not None:
        rs.stop_refresher()
        monkeypatch.setattr(rs, "start_refresher", lambda *a, **k: True)
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    yield SimpleNamespace(vault=_WarmVault(), vault_dir=vault_dir)
    psnap.stop_refresher()
    if rs is not None:
        rs.stop_refresher()


def _median_ms(fn, *, before_each=None) -> float:
    fn()                                       # warm imports, not measured
    samples = []
    for _ in range(_RUNS):
        if before_each is not None:
            before_each()
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000.0)
    return statistics.median(samples)


def _first_paint():
    """Put the process back in the state a FRESHLY STARTED daemon is in:
    nothing observed yet, every memo cold.

    THIS IS THE STATE THAT MATTERS, and getting it wrong cost this pin its
    teeth once already. The first draft only expired the two TTL dicts. A
    mutation that put ``_scan_daemon_processes()`` back on the render path
    still PASSED, because the scan publishes -- so run 1 paid the walk and
    runs 2..5 read what run 1 had just published, and the median never saw it.
    Clearing the published snapshots too is what makes a re-introduced
    synchronous fallback visible: the mutation is red again below.
    """
    hb._daemon_probe_cache["ts"] = -1e9
    hb._daemon_procs_cache["ts"] = -1e9
    try:
        from systemu.runtime import optional_deps as od
        od.clear_probe_cache()
    except Exception:
        pass                # a tree without the memo has nothing to clear
    try:
        from systemu.runtime import render_snapshots as rs
    except Exception:
        return              # a tree without the mechanism has nothing to clear
    rs.clear()


# ── the budget ──────────────────────────────────────────────────────────────

def test_the_health_banner_render_is_within_budget(recording_ui, warm):
    """Measured on the base tree: 1138.9 ms, every 20 s, on every route."""
    took = _median_ms(lambda: hb.render_health_banner(warm.vault_dir),
                      before_each=_first_paint)
    assert took < BUDGET_SECTION_MS, (
        "render_health_banner took %.1f ms of the event loop (budget %.0f ms)"
        % (took, BUDGET_SECTION_MS))


def test_the_command_palette_mount_is_within_budget(recording_ui, warm):
    """Measured on the base tree: 679.7 ms, on every route, for an overlay the
    operator has not opened."""
    took = _median_ms(lambda: cp.build_command_palette(warm.vault),
                      before_each=_first_paint)
    assert took < BUDGET_SECTION_MS, (
        "build_command_palette took %.1f ms of the event loop (budget %.0f ms)"
        % (took, BUDGET_SECTION_MS))


def test_the_shared_layout_builders_are_within_budget_together(recording_ui,
                                                               warm):
    """The property the two pins beside this one cannot state: the WHOLE
    synchronous section, so the next offender to land here is caught by its
    cost rather than by its name."""
    def _both():
        hb.render_health_banner(warm.vault_dir)
        cp.build_command_palette(warm.vault)

    took = _median_ms(_both, before_each=_first_paint)
    assert took < BUDGET_TOTAL_MS, (
        "the shared layout's builders took %.1f ms of the event loop "
        "(budget %.0f ms)" % (took, BUDGET_TOTAL_MS))


def test_the_builders_stay_within_budget_once_snapshots_exist(recording_ui,
                                                              warm):
    """The steady state, as distinct from first paint: with both observations
    published, a render is a snapshot read. This is the case that runs
    thousands of times; the one above is the case a fallback hides in."""
    from systemu.runtime import render_snapshots as rs
    _first_paint()
    rs.register(rs.PALETTE_INDEX, lambda: cp.publish_index(warm.vault))
    rs.register(rs.DAEMON_SCAN, hb._scan_daemon_processes)
    assert rs.refresh_now(None) == 2
    assert rs.daemon_scan() is not None and rs.palette_index() is not None

    def _both():
        hb.render_health_banner(warm.vault_dir)
        cp.build_command_palette(warm.vault)

    took = _median_ms(_both)
    rs._producers.pop(rs.PALETTE_INDEX, None)
    rs._producers.pop(rs.DAEMON_SCAN, None)
    assert took < BUDGET_TOTAL_MS, (
        "reading the published snapshots took %.1f ms of the event loop "
        "(budget %.0f ms)" % (took, BUDGET_TOTAL_MS))


def test_the_palette_keystroke_path_is_within_budget(recording_ui, warm):
    """Typing in the palette runs per keystroke. It reads a published index;
    it must never assemble one."""
    took = _median_ms(cp.palette_entries, before_each=_first_paint)
    assert took < BUDGET_SECTION_MS, (
        "palette_entries took %.1f ms per keystroke (budget %.0f ms)"
        % (took, BUDGET_SECTION_MS))


def test_the_budget_is_measured_against_a_vault_that_really_answers(warm):
    """A budget passed by a vault that returns nothing is not a budget.

    Pins the fixture itself: 40 tools, each declaring a distinct absent
    distribution, is the corpus the numbers above were taken against.
    """
    tools = warm.vault.list_tools()
    assert len(tools) == 40, len(tools)
    deps = {d for t in tools for d in t["dependencies"]}
    assert len(deps) == 40, "the corpus must exercise 40 DISTINCT lookups"
    from systemu.runtime import optional_deps as od
    assert od.missing_packages(sorted(deps)) == tuple(sorted(deps)), (
        "every declared dependency must really be absent, or the probe never "
        "pays for the miss this budget is about")
