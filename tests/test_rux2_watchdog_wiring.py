"""R-UX2 / SPEC §15-UX UX-9(a) — the loop-lag watchdog is wired to the REAL app.

A watchdog that is correct but never started is indistinguishable, from the
suite's point of view, from one that runs — and it would report a permanently
healthy install forever. That is the dropped-call-site class this repo has hit
repeatedly, so the hooks are exercised here rather than merely registered.
"""
from __future__ import annotations

import ast
import asyncio
import inspect

from systemu.interface import dashboard
from systemu.runtime import loop_lag


class _FakeApp:
    """Records what ``_install_loop_lag_watchdog`` registers, so the handlers
    can then actually be RUN (a registration count alone proves nothing)."""

    def __init__(self) -> None:
        self.startup = []
        self.shutdown = []

    def on_startup(self, handler):
        self.startup.append(handler)
        return handler

    def on_shutdown(self, handler):
        self.shutdown.append(handler)
        return handler


def test_install_registers_exactly_one_startup_and_one_shutdown_hook():
    app = _FakeApp()
    dashboard._install_loop_lag_watchdog(app)
    assert len(app.startup) == 1
    assert len(app.shutdown) == 1


def test_the_registered_hooks_really_start_and_stop_a_MEASURING_watchdog():
    """Runs on the REAL default configuration — 100ms heartbeat, 10-sample
    minimum — so the warm-up it implies is asserted rather than tuned away.

    For roughly the first second of a dashboard's life there are fewer than ten
    samples in the window, and /health says "LOAD NOT MEASURED". That is the
    intended answer, not a gap: we genuinely do not know yet, and the one thing
    a warm-up must never do is start out green.
    """
    loop_lag.reset_watchdog()
    try:
        app = _FakeApp()
        dashboard._install_loop_lag_watchdog(app)
        seen = {}

        async def _main():
            assert loop_lag.get_watchdog().running is False
            await app.startup[0]()
            assert loop_lag.get_watchdog().running is True
            # It must genuinely MEASURE, not merely flip a flag: give the
            # heartbeat long enough to record real samples on this real loop.
            await asyncio.sleep(0.35)
            seen["warming"] = loop_lag.get_watchdog().snapshot()
            await asyncio.sleep(1.1)
            seen["warm"] = loop_lag.get_watchdog().snapshot()
            await app.shutdown[0]()
            assert loop_lag.get_watchdog().running is False

        asyncio.run(_main())

        warming = seen["warming"]
        assert 0 < warming["ring_samples"] < 10, warming
        # THE invariant, and the one the docstring names: a warm-up must never
        # start out GREEN. It is not necessarily "unknown" — the min-samples
        # guard is deliberately ONE-SIDED, so a loop that is genuinely lagging
        # reports "busy" on thin data instead of having the trouble suppressed.
        # Asserting "unknown" exactly pinned the idle-box outcome rather than
        # the rule, and failed in the full suite on a box loaded by other test
        # runs, where the real loop really was lagging and "busy" was honest.
        assert warming["load_state"] != "normal", warming
        assert warming["load_state"] in ("unknown", "busy"), warming
        if warming["load_state"] == "busy":
            # ...and "busy" is not an escape hatch for thin data: it has to be
            # backed by an observed p95 over the real threshold.
            assert warming["recent_p95_ms"] is not None, warming
            assert warming["recent_p95_ms"] >= loop_lag._DEFAULT_LOAD_LAG_MS, warming

        warm = seen["warm"]
        assert warm["ring_samples"] >= 10, warm
        assert warm["measured"] is True, warm
        assert warm["recent_p95_ms"] is not None, warm
        # `load_state` is a COMPOSITE, so it is only allowed to reach a verdict
        # once BOTH inputs are in. Tied to `cpu_measured` rather than widened to
        # accept all three states, which would have asserted nothing.
        if warm["cpu_measured"]:
            assert warm["load_state"] in ("normal", "busy"), warm
        else:
            assert warm["load_state"] == "unknown", warm
    finally:
        loop_lag.reset_watchdog()


def test_a_watchdog_that_raises_on_start_does_not_take_the_dashboard_down():
    """A broken meter must degrade, not become an outage."""
    loop_lag.reset_watchdog()
    try:
        app = _FakeApp()
        dashboard._install_loop_lag_watchdog(app)
        wd = loop_lag.get_watchdog()

        def _boom(*a, **k):
            raise RuntimeError("watchdog exploded")

        wd.start = _boom

        async def _main():
            await app.startup[0]()      # must not raise

        asyncio.run(_main())
    finally:
        loop_lag.reset_watchdog()


def test_a_watchdog_that_failed_to_start_reads_UNKNOWN_not_healthy():
    """The swallow above is only survivable because of what it leaves behind.

    A meter that never started has no samples, so /health says "LOAD NOT
    MEASURED". If the failure path could instead produce the healthy value, the
    ``except Exception: log.debug(...)`` would be converting an outage into a
    reassuring green chip — which is the exact shape this release was refuted
    for the first time around.
    """
    loop_lag.reset_watchdog()
    try:
        app = _FakeApp()
        dashboard._install_loop_lag_watchdog(app)
        loop_lag.get_watchdog().start = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("nope"))

        asyncio.run(app.startup[0]())

        snap = loop_lag.get_watchdog().snapshot()
        assert snap["measured"] is False
        assert snap["load_state"] == "unknown"
        assert snap["recent_p95_ms"] is None
    finally:
        loop_lag.reset_watchdog()


def test_run_dashboard_actually_calls_the_installer():
    """The last inch of the wiring, checked on the AST rather than the text.

    A substring search over the source would also match the call appearing in a
    comment or a docstring — this repo has shipped a pin that passed on the
    strength of a comment. Only a real ``Call`` node counts.
    """
    tree = ast.parse(inspect.getsource(dashboard.run_dashboard))
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "_install_loop_lag_watchdog" in called, sorted(called)
