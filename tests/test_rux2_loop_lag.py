"""R-UX2 / SPEC Part II §15-UX UX-9(a) — the event-loop lag watchdog.

The watchdog is the METER the DEC-20c pivot triggers read: PIV-1 fires on
``loop-lag p95 > 250ms in normal use``; PIV-2 fires on ``sustained lag spikes
attributable to runtime work`` — and the spec is explicit that "the watchdog
logs the offending stacks — **attribution is in the data, not in argument**".

Three classes of test here:

* hermetic unit tests over the pure accounting, driven through ``record()`` on
  an INJECTED CLOCK so window boundaries and staleness — which are *time*
  predicates — are exercised deterministically rather than slept through;
* **regression tests for the two readings that refuted the first cut** (a
  three-window chip, and a healthy value served off a dead heartbeat); and
* a **real-path integration test** that starts the watchdog on a REAL asyncio
  loop, REALLY blocks that loop, and asserts both the observed lag AND that the
  captured stack names the offending function. A percentile helper that is
  never actually driven by a blocked loop would be indistinguishable from one
  that is, which is the defect class this repo keeps shipping.
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest

from systemu.runtime.loop_lag import LoopLagWatchdog, get_watchdog, reset_watchdog


class _Clock:
    """A monotonic clock we control. ``record()`` stamps liveness from it, so
    advancing it is how a test makes a meter go stale without sleeping."""

    def __init__(self, t: float = 10_000.0) -> None:
        self.t = float(t)

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += float(dt)


def _feed_cpu(wd: LoopLagWatchdog, clock: _Clock, pct: float) -> None:
    """Add one CPU reading, rate-limited exactly like ``_refresh_cpu``.

    Timestamps are threaded through the same way production does it —
    ``covers_from`` is the PREVIOUS reading's stamp — so the span the watchdog
    reports here is computed from the same inputs it computes from live.
    """
    now = clock()
    with wd._lock:
        last = wd._cpu_readings[-1].ts if wd._cpu_readings else None
    if last is not None and (now - last) < wd._cpu_poll_s:
        return
    wd._add_cpu_reading(
        pct, ts=now,
        covers_from=(now - wd._cpu_poll_s) if last is None else last)


def _drive(wd: LoopLagWatchdog, clock: _Clock, lags, *,
           interval_s: float = 0.1, cpu: "float | None" = 5.0):
    """Feed lags the way the heartbeat really does — and CPU alongside them.

    A beat that observed ``lag`` ms of overshoot took ``interval + lag`` of wall
    clock to come back, so the clock advances by that much — not by the nominal
    interval. This matters: a heavily lagged loop produces FEWER samples per
    second, and a fixture that advanced by a flat interval would quietly test a
    sample cadence production never sees.

    ``cpu`` feeds the OTHER half of the meter on its own poll cadence. That is
    not decoration: ``load_state`` is a composite of lag AND CPU, ``psutil`` is a
    hard dependency in ``pyproject.toml``, and the real sampler refreshes CPU on
    every tick — so a fixture that fed lag alone was a half-fed watchdog no
    production install has, and could not tell a measured verdict from an
    unmeasured one. Pass ``cpu=None`` to exercise a genuinely unmeasured CPU.
    """
    for lag in lags:
        clock.advance(interval_s + (lag / 1000.0))
        wd.record(float(lag))
        if cpu is not None:
            _feed_cpu(wd, clock, float(cpu))


# ── the shape of the contract ────────────────────────────────────────────────

def test_no_samples_reports_not_measured_never_healthy():
    """A watchdog that has never run must NOT report a reassuring zero.

    /health already shipped this failure class once (see the `_needs_you_section`
    docstring): with no data we do not KNOW the loop is responsive, and the page
    is reachable precisely when the install is broken. "not measured" and
    "measured, and fine" are different facts and must render differently.
    """
    wd = LoopLagWatchdog()
    snap = wd.snapshot()
    assert snap["measured"] is False
    assert snap["load_state"] == "unknown"
    assert snap["ring_samples"] == 0
    assert snap["recent_p95_ms"] is None
    assert snap["ring_p95_ms"] is None


def test_the_ambiguous_keys_are_GONE_not_merely_corrected():
    """The three-window chip was possible because ``p95_ms`` did not say *over
    what* and ``under_load`` answered a three-valued question with a bool.

    Pinning "the windows now agree" would leave both names in place for the next
    caller to misread. They are deleted instead, and this is the pin that keeps
    them deleted — a second path removed beats two paths asserted equal.
    """
    wd = LoopLagWatchdog()
    wd.record(5.0)
    snap = wd.snapshot()
    for gone in ("p95_ms", "p50_ms", "under_load", "samples", "breaches"):
        assert gone not in snap, f"{gone!r} came back: {sorted(snap)}"
    assert not hasattr(wd, "under_load"), "the tri-state-as-bool accessor is back"
    # every percentile/counter names its window
    assert {"recent_p95_ms", "ring_p95_ms", "session_breaches",
            "recent_breaches", "recent_window_s"} <= set(snap)


def test_snapshot_is_a_plain_json_safe_dict():
    """It crosses the nicegui boundary; no exotic types."""
    import json
    clock = _Clock()
    wd = LoopLagWatchdog(clock=clock)
    _drive(wd, clock, [12.0] * 20)
    json.dumps(wd.snapshot())            # must not raise


# ── ONE window (the first refutation) ────────────────────────────────────────

def test_a_recovered_loop_reads_responsive_AND_cannot_cite_the_old_spike():
    """REGRESSION — reproduced reading:
    ``RESPONSIVE :: UI lag p95 900ms · 200 stall(s)``.

    200 samples at 900ms followed by 50 calm ones. The state came from the last
    ~5s (calm ⇒ "normal") while the p95 printed beside it came from the whole
    ~2-minute ring (900ms) and the stall count was all-time. Every number was
    real; the sentence was false.

    The recovery itself is correct behaviour — a spike two minutes ago must not
    pin the chip to "busy" forever. What must not happen is the chip citing that
    spike as the evidence for "responsive".
    """
    clock = _Clock()
    wd = LoopLagWatchdog(clock=clock)
    _drive(wd, clock, [900.0] * 200)
    _drive(wd, clock, [1.0] * 50)

    snap = wd.snapshot()
    assert snap["load_state"] == "normal"
    # the number backing the state is from the SAME window as the state
    assert snap["recent_p95_ms"] == pytest.approx(1.0)
    assert snap["recent_breaches"] == 0
    # the long-horizon figure still exists — under a name that says so
    assert snap["ring_p95_ms"] > 800.0
    assert snap["session_breaches"] == 200


def test_a_freshly_slow_loop_reads_busy_AND_cites_the_slowness_it_means():
    """REGRESSION — the inverse reading: ``UNDER LOAD :: UI lag p95 1ms``.

    1200 quiet samples then 5 slow ones: the ~5s window saw the spike and said
    "busy", the ring p95 printed beside it was still 1ms. A chip announcing
    trouble while displaying a healthy number is worse than no chip.
    """
    clock = _Clock()
    wd = LoopLagWatchdog(clock=clock)
    _drive(wd, clock, [1.0] * 1200)
    _drive(wd, clock, [900.0] * 5)

    snap = wd.snapshot()
    assert snap["load_state"] == "busy"
    assert snap["recent_p95_ms"] > 100.0, snap
    assert snap["recent_breaches"] == 5
    assert snap["ring_p95_ms"] is not None    # still published, still named


def test_recent_and_ring_are_computed_over_genuinely_different_spans():
    """Otherwise the two tests above could both pass on an implementation that
    quietly used one window for everything — which would re-break PIV-1, whose
    trigger is a SUSTAINED p95 and must not be read off five seconds."""
    clock = _Clock()
    wd = LoopLagWatchdog(clock=clock, load_window_s=5.0, capacity=1200)
    _drive(wd, clock, [400.0] * 100)      # ~50s of bad
    _drive(wd, clock, [2.0] * 60)         # ~6s of good

    snap = wd.snapshot()
    assert snap["recent_p95_ms"] < 50.0, snap
    assert snap["ring_p95_ms"] > 300.0, snap
    assert snap["ring_span_s"] > snap["recent_window_s"]


def test_percentiles_are_nearest_rank_over_the_recent_window():
    clock = _Clock()
    # a wide window so all 100 samples are "recent"
    wd = LoopLagWatchdog(clock=clock, load_window_s=10_000.0, load_lag_ms=1e9)
    _drive(wd, clock, [float(v) for v in range(1, 101)], interval_s=0.0)
    snap = wd.snapshot()
    assert snap["measured"] is True
    assert snap["recent_samples"] == 100
    assert snap["recent_p50_ms"] == pytest.approx(50.0, abs=1.0)
    assert snap["recent_p95_ms"] == pytest.approx(95.0, abs=1.0)
    assert snap["recent_max_ms"] == pytest.approx(100.0)


def test_breach_counts_only_samples_over_the_threshold():
    clock = _Clock()
    wd = LoopLagWatchdog(clock=clock, breach_ms=250.0, load_window_s=10_000.0,
                         min_window_samples=1)
    _drive(wd, clock, [10.0, 249.0, 250.0, 251.0, 900.0], interval_s=0.0)
    snap = wd.snapshot()
    # strictly greater than the threshold: 251 and 900
    assert snap["session_breaches"] == 2
    assert snap["recent_breaches"] == 2
    assert snap["breach_ms"] == 250.0


def test_ring_buffer_is_bounded_so_a_long_uptime_cannot_grow_without_limit():
    clock = _Clock()
    wd = LoopLagWatchdog(clock=clock, capacity=10)
    _drive(wd, clock, [float(v) for v in range(100)])
    snap = wd.snapshot()
    assert snap["ring_samples"] == 10
    # session_max_lag_ms is an ALL-TIME high-water mark, not a window max: a
    # spike that aged out of the ring is still a fact about this process.
    assert snap["session_max_lag_ms"] == pytest.approx(99.0)
    assert snap["session_samples"] == 100


def test_a_thin_window_will_not_claim_responsive_but_will_still_report_trouble():
    """The min-sample guard is deliberately ONE-SIDED.

    It blocks the healthy value on thin evidence. It must not block the unhealthy
    one — a badly lagged loop produces fewer samples per second by construction
    (each beat costs interval + lag), so a symmetric guard would silence the
    chip exactly when it matters most.
    """
    clock = _Clock()
    calm = LoopLagWatchdog(clock=clock, min_window_samples=10)
    _drive(calm, clock, [2.0] * 3)
    snap = calm.snapshot()
    assert snap["load_state"] == "unknown"
    assert snap["recent_p95_ms"] is None, "no percentile published for a claim not made"
    assert "3 sample" in (snap["load_reason"] or "")

    clock2 = _Clock()
    slow = LoopLagWatchdog(clock=clock2, min_window_samples=10)
    _drive(slow, clock2, [900.0] * 3)
    snap2 = slow.snapshot()
    assert snap2["load_state"] == "busy", snap2
    assert snap2["recent_p95_ms"] > 100.0


# ── a dead meter (the second refutation) ─────────────────────────────────────

def test_a_frozen_ring_cannot_serve_a_healthy_reading():
    """REGRESSION — reproduced: ``running=True measured=True p95=3.0
    chip=RESPONSIVE`` after the heartbeat task had been killed.

    ``_heartbeat`` swallows its own death and leaves ``_running`` True, and the
    old ``snapshot()`` never looked at ``_last_beat`` — so the ring froze with
    healthy numbers in it and the chip read "responsive" indefinitely.
    """
    clock = _Clock()
    wd = LoopLagWatchdog(clock=clock, stale_after_s=2.0)
    wd._running = True                      # as the swallowed exception leaves it
    _drive(wd, clock, [3.0] * 30)
    assert wd.snapshot()["load_state"] == "normal"   # live: correct

    clock.advance(3600.0)                   # the heartbeat stops; an hour passes
    snap = wd.snapshot()
    assert snap["running"] is True, "precondition: the flag still lies"
    assert snap["live"] is False
    assert snap["stale"] is True
    assert snap["measured"] is False
    assert snap["load_state"] == "unknown"
    assert snap["recent_p95_ms"] is None, "a frozen ring must not serve a percentile"
    assert snap["sample_age_s"] == pytest.approx(3600.0, abs=1.0)


def test_staleness_is_measured_from_the_last_SAMPLE_not_the_running_flag():
    """`_running` is the signal that lied, so it must not be the one consulted.

    A watchdog that was never "started" but is receiving samples is live; a
    watchdog flagged running whose samples stopped is not.
    """
    clock = _Clock()
    wd = LoopLagWatchdog(clock=clock, stale_after_s=2.0)
    _drive(wd, clock, [2.0] * 20)
    assert wd.running is False, "precondition: never start()ed"
    assert wd.snapshot()["live"] is True
    assert wd.snapshot()["load_state"] == "normal"

    clock.advance(2.5)
    assert wd.snapshot()["live"] is False
    assert wd.snapshot()["load_state"] == "unknown"


def test_a_dying_heartbeat_leaves_running_true_on_the_REAL_object():
    """The precondition the test above assumes, asserted on the real coroutine
    rather than taken on trust: the swallow is still there, so staleness is
    still the only thing standing between a dead meter and a healthy reading."""
    wd = LoopLagWatchdog(interval_s=0.01, capture_stacks=False)

    async def _main():
        wd.start()
        assert wd.running is True
        # kill the heartbeat the way an unexpected exception would
        wd._heartbeat_task.cancel()
        try:
            await wd._heartbeat_task
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0.05)

    asyncio.run(_main())
    assert wd.running is True, "precondition changed — re-read the staleness design"
    wd._stop_event.set()


# ── CPU: smoothed, and over a psutil-valid interval ──────────────────────────

def test_cpu_pressure_alone_marks_busy():
    """UX-9(f): the chip is loop-lag *and* CPU."""
    clock = _Clock()
    wd = LoopLagWatchdog(clock=clock, load_lag_ms=100.0, load_cpu_pct=85.0)
    _drive(wd, clock, [1.0] * 20)                # loop is fine
    assert wd.snapshot()["load_state"] == "normal"
    for _ in range(5):
        wd._add_cpu_reading(97.0)                # ...but the box is pegged
    assert wd.snapshot()["load_state"] == "busy"


def test_one_noisy_cpu_spike_does_not_flip_the_gate():
    """REGRESSION — ``psutil.cpu_percent(interval=None)`` polled every 50ms was
    measuring a 50ms slice and being tested raw against 85%.

    Measured on an idle box: 40 consecutive 50ms polls spanned 9.4%–91.7% while
    the 1-second reading over the same window was 50.4%. One of those forty
    crossed the threshold on its own, so the chip could announce "UNDER LOAD"
    from noise. The reported figure is now a mean over several psutil-valid
    intervals, so a single outlier cannot carry it.
    """
    clock = _Clock()
    wd = LoopLagWatchdog(clock=clock, load_cpu_pct=85.0, cpu_smoothing=5)
    _drive(wd, clock, [1.0] * 20)
    for pct in (12.0, 9.0, 91.7, 14.0, 11.0):    # one real outlier, four calm
        wd._add_cpu_reading(pct)
    snap = wd.snapshot()
    assert snap["cpu_percent"] == pytest.approx(27.5, abs=0.1)
    assert snap["load_state"] == "normal", snap
    # sustained load still gets through
    for _ in range(5):
        wd._add_cpu_reading(93.0)
    assert wd.snapshot()["load_state"] == "busy"


def test_cpu_is_polled_no_faster_than_the_psutil_valid_interval():
    """The rate limit is the half of the fix that keeps each reading meaningful;
    smoothing alone would just average forty 50ms slices."""
    clock = _Clock()
    wd = LoopLagWatchdog(clock=clock, cpu_poll_s=1.0)
    calls = []

    class _FakePsutil:
        @staticmethod
        def cpu_percent(interval=None):
            calls.append(interval)
            return 50.0

    import sys
    real = sys.modules.get("psutil")
    sys.modules["psutil"] = _FakePsutil
    try:
        for _ in range(40):                      # 40 sampler ticks @50ms = 2s
            wd._refresh_cpu()
            clock.advance(0.05)
    finally:
        if real is not None:
            sys.modules["psutil"] = real
        else:
            sys.modules.pop("psutil", None)

    assert len(calls) == 2, f"polled {len(calls)}x in 2s — the rate limit is gone"
    assert all(i is None for i in calls), "must stay the NON-blocking form"


def test_the_first_cpu_reading_is_discarded_because_psutil_documents_it_as_meaningless():
    """``cpu_percent(interval=None)`` returns 0.0 on its first call. 0.0 is a
    healthy-looking number, and averaging it in would drag the mean toward
    "not loaded" for the first few seconds of every process."""
    clock = _Clock()
    wd = LoopLagWatchdog(clock=clock, cpu_poll_s=1.0)

    class _FakePsutil:
        vals = iter([0.0, 90.0, 90.0])

        @staticmethod
        def cpu_percent(interval=None):
            return next(_FakePsutil.vals)

    import sys
    real = sys.modules.get("psutil")
    sys.modules["psutil"] = _FakePsutil
    try:
        wd._refresh_cpu()                       # priming call -> discarded
        assert wd._cpu_view()[0] is None
        clock.advance(1.0)
        wd._refresh_cpu()
        clock.advance(1.0)
        wd._refresh_cpu()
    finally:
        if real is not None:
            sys.modules["psutil"] = real
        else:
            sys.modules.pop("psutil", None)

    view = wd._cpu_view()
    assert view.percent == pytest.approx(90.0), "the bogus 0.0 was averaged in"
    assert view.readings == 2
    # The span is MEASURED end to end (the second reading's stamp back to the
    # first one's interval start), not `len(readings) * cpu_poll_s`.
    assert view.span_s == pytest.approx(2.0)


def test_a_missing_psutil_reports_no_cpu_rather_than_zero():
    clock = _Clock()
    wd = LoopLagWatchdog(clock=clock)
    _drive(wd, clock, [1.0] * 20, cpu=None)
    snap = wd.snapshot()                        # no readings ever added
    assert snap["cpu_percent"] is None
    assert snap["cpu_window_s"] is None
    assert snap["cpu_measured"] is False
    # THE POINT. This previously asserted "normal" — pinning the defect rather
    # than the fix. `load_state` is a composite of lag AND CPU; with CPU
    # unmeasured the honest answer is that we do not know, and the healthy
    # value must be unreachable. "decided on lag alone" was never a decision
    # about system load, it was a decision about half of it.
    assert snap["load_state"] == "unknown", snap
    assert snap["measured"] is True, "the LAG half is measured and stays published"
    assert snap["recent_p95_ms"] is not None


def test_an_unmeasured_cpu_cannot_produce_the_healthy_value_however_calm_the_lag():
    """MUTATION TARGET (defect 3). A perfectly calm loop is not evidence the
    system is not under load — it is evidence about one of the two inputs.

    Driven across a wide range of calm lags so this cannot pass by accident on
    one lucky percentile.
    """
    for lag in (0.1, 1.0, 5.0, 20.0):
        clock = _Clock()
        wd = LoopLagWatchdog(clock=clock)
        _drive(wd, clock, [lag] * 40, cpu=None)
        snap = wd.snapshot()
        assert snap["load_state"] == "unknown", (lag, snap)
        assert snap["load_state"] != "normal", (lag, snap)


def test_a_cpu_reading_older_than_the_max_age_forces_unknown_not_normal():
    """MUTATION TARGET (defect 2). The readings deque was untimestamped, so it
    could not expire: 6s of 10% readings, psutil then dies, 600 simulated
    seconds of calm — and the chip still reported ``load_state="normal"``,
    ``cpu_percent=10.0``, ``cpu_window_s=5.0``. A ten-minute-old number was
    being served, and labelled a 5-second average.
    """
    clock = _Clock()
    wd = LoopLagWatchdog(clock=clock, capacity=100_000)
    for _ in range(6):
        _feed_cpu(wd, clock, 10.0)
        clock.advance(1.0)
    assert wd.snapshot()["cpu_measured"] is True, "precondition: CPU was measured"

    # psutil is dead from here: lag keeps flowing, CPU does not.
    _drive(wd, clock, [1.0] * 6000, cpu=None)

    snap = wd.snapshot()
    assert snap["cpu_percent"] is None, snap
    assert snap["cpu_window_s"] is None, snap
    assert snap["load_state"] == "unknown", snap
    assert snap["load_state"] != "normal", snap


def test_a_stale_HIGH_cpu_reading_does_not_pin_the_chip_to_busy_forever():
    """The symmetric false POSITIVE of the same defect — a meter that lies in
    the alarming direction is still a meter that lies. One 99% reading, psutil
    then dead for 600 simulated seconds, previously left ``load_state="busy"``
    permanently, still labelled a 5s average."""
    clock = _Clock()
    wd = LoopLagWatchdog(clock=clock, capacity=100_000)
    # a calm loop on a pegged box: genuinely busy, and busy for the CPU reason
    _drive(wd, clock, [1.0] * 40, cpu=99.0)
    pre = wd.snapshot()
    assert pre["load_state"] == "busy", pre
    assert pre["cpu_percent"] == pytest.approx(99.0), pre

    # psutil dies here; the loop stays calm for 600 simulated seconds.
    _drive(wd, clock, [1.0] * 6000, cpu=None)

    snap = wd.snapshot()
    assert snap["load_state"] != "busy", snap
    assert snap["load_state"] == "unknown", snap
    assert snap["cpu_percent"] is None, snap


def test_the_reported_cpu_window_is_a_measured_span_not_a_reading_COUNT():
    """MUTATION TARGET. ``len(readings) * cpu_poll_s`` is the exact pattern the
    lag half rejects at ``_samples``: it reports the window it ASSUMED, so a
    reading cadence that slipped was still labelled with the nominal figure.

    Here the readings really are 3s apart, so a count-based window would say
    ``2 * 1.0 = 2.0`` while the truth is 6.0.
    """
    clock = _Clock()
    wd = LoopLagWatchdog(clock=clock, cpu_poll_s=1.0, cpu_max_age_s=1000.0)
    _feed_cpu(wd, clock, 40.0)
    clock.advance(3.0)
    _feed_cpu(wd, clock, 40.0)
    clock.advance(3.0)
    _feed_cpu(wd, clock, 40.0)

    view = wd._cpu_view()
    assert view.readings == 3
    assert view.span_s == pytest.approx(7.0), (
        "span must be measured end-to-end, not len(readings) * cpu_poll_s")
    assert view.span_s != pytest.approx(3.0 * wd._cpu_poll_s)


def test_a_persistently_failing_psutil_still_obeys_the_rate_limit():
    """MUTATION TARGET (defect 4). Both ``except`` paths returned BEFORE
    stamping ``_cpu_polled_at``, so the failure path bypassed the very limit
    that exists to protect it: measured at 100 calls over 5 simulated seconds
    against a 1.0s limit, each one formatting a traceback via ``exc_info=True``.
    """
    import sys
    import types as _types

    clock = _Clock()
    wd = LoopLagWatchdog(clock=clock, cpu_poll_s=1.0)
    calls = []

    fake = _types.ModuleType("psutil")

    def _boom(interval=None):
        calls.append(interval)
        raise RuntimeError("psutil is broken")

    fake.cpu_percent = _boom
    real = sys.modules.get("psutil")
    sys.modules["psutil"] = fake
    try:
        for _ in range(100):                # 100 sampler ticks = 5 simulated s
            wd._refresh_cpu()
            clock.advance(0.05)
    finally:
        if real is not None:
            sys.modules["psutil"] = real
        else:
            sys.modules.pop("psutil", None)

    assert len(calls) <= 6, f"polled {len(calls)}x in 5s against a 1.0s limit"
    assert len(calls) >= 4, f"polled only {len(calls)}x — it stopped retrying"
    assert wd.snapshot()["cpu_measured"] is False


# ── real path: a genuinely blocked asyncio loop ──────────────────────────────

def _the_offending_call(seconds: float) -> None:
    """A deliberately-blocking sync call, named so the captured stack can be
    asserted on by name (this is the PIV-2 attribution contract)."""
    time.sleep(seconds)


def test_watchdog_observes_a_real_blocked_loop_and_attributes_the_stack():
    wd = LoopLagWatchdog(interval_s=0.02, breach_ms=150.0,
                         sampler_interval_s=0.02)

    async def _main():
        wd.start()
        await asyncio.sleep(0.15)        # settle; heartbeat establishes a baseline
        _the_offending_call(0.6)         # <-- REALLY blocks the loop
        await asyncio.sleep(0.25)        # let the heartbeat observe the overshoot
        await wd.stop()

    asyncio.run(_main())

    snap = wd.snapshot()
    assert snap["session_max_lag_ms"] > 150.0, snap
    assert snap["session_breaches"] >= 1, snap
    # attribution: the sampler thread caught the loop thread mid-block
    stacks = "\n".join(s["stack"] for s in snap["worst_stacks"])
    assert "_the_offending_call" in stacks, stacks
    # REGRESSION (real bug this test caught): the watchdog briefly stripped
    # stack lines containing its own module filename. That is a SUBSTRING
    # match, and THIS FILE is named ``test_rux2_loop_lag.py`` — it ends with
    # ``loop_lag.py``, so the culprit frame was erased from its own report and
    # the stall attributed to nothing. The blocking frame must survive.
    assert "test_rux2_loop_lag.py" in stacks, stacks


def test_the_real_loop_reads_busy_after_a_real_stall():
    """End-to-end on a REAL loop: the state and the number the chip would print
    both come out of the same window, with no injected clock anywhere."""
    wd = LoopLagWatchdog(interval_s=0.02, breach_ms=150.0,
                         sampler_interval_s=0.02, load_lag_ms=100.0,
                         min_window_samples=3, capture_stacks=False)

    async def _main():
        wd.start()
        await asyncio.sleep(0.1)
        _the_offending_call(0.5)
        await asyncio.sleep(0.2)
        await wd.stop()

    asyncio.run(_main())
    snap = wd.snapshot()
    assert snap["load_state"] == "busy", snap
    assert snap["recent_p95_ms"] is not None and snap["recent_p95_ms"] > 100.0, snap


def test_an_unblocked_loop_records_low_lag_and_no_breach(monkeypatch):
    """The negative half — otherwise the tests above pass on a broken watchdog
    that reports a breach unconditionally.

    The REAL sampler thread runs against a REAL asyncio loop here; only
    ``psutil.cpu_percent``'s return VALUE is pinned, so the box's own load
    cannot decide the assertion. Everything the fix touches — the rate limit,
    the timestamps, the expiry, the composite — is exercised for real.

    ``cpu_poll_s`` is shortened because the healthy verdict is now genuinely
    unreachable until CPU has been measured, and at the 1.0s default this 0.6s
    run would end on the priming call with CPU still unknown. That is not a
    test artefact: for its first poll interval a freshly-started watchdog really
    does report "unknown" rather than "normal", which is the honest answer.
    """
    import psutil
    monkeypatch.setattr(psutil, "cpu_percent", lambda interval=None: 5.0)
    wd = LoopLagWatchdog(interval_s=0.02, breach_ms=150.0,
                         sampler_interval_s=0.02, cpu_poll_s=0.05)

    async def _main():
        wd.start()
        await asyncio.sleep(0.6)         # cooperative the whole way
        await wd.stop()

    asyncio.run(_main())

    snap = wd.snapshot()
    assert snap["measured"] is True, snap
    assert snap["cpu_measured"] is True, snap   # the sampler really polled
    assert snap["load_state"] == "normal", snap
    assert snap["session_breaches"] == 0, snap
    assert snap["worst_stacks"] == []
    assert snap["session_max_lag_ms"] < 150.0, snap


def test_capture_is_the_deepest_observation_of_the_stall_not_its_leading_edge():
    """One entry per stall episode, recorded at its worst — not at ``breach_ms``.

    Sampling only at the leading edge attributes a stall to whatever ran just
    before the culprit (often an idle loop between callbacks), which is how the
    attribution above first came back empty.
    """
    wd = LoopLagWatchdog(interval_s=0.02, breach_ms=150.0,
                         sampler_interval_s=0.02)

    async def _main():
        wd.start()
        await asyncio.sleep(0.15)
        _the_offending_call(0.6)
        await asyncio.sleep(0.25)
        await wd.stop()

    asyncio.run(_main())

    snap = wd.snapshot()
    worst = snap["worst_stacks"]
    # one stall happened ⇒ exactly one episode is reported (not ~25 ticks of it)
    assert len(worst) == 1, worst
    assert len({s["episode"] for s in worst}) == 1
    # ...and it is recorded near the END of the ~600ms stall, not at 150ms
    assert worst[0]["stall_ms"] > 400.0, worst


def test_stop_is_safe_when_never_started_and_start_is_idempotent():
    wd = LoopLagWatchdog(interval_s=0.02)

    async def _main():
        await wd.stop()                  # never started
        wd.start()
        wd.start()                       # second start must not add a 2nd heartbeat
        await asyncio.sleep(0.1)
        assert wd._heartbeat_task is not None
        await wd.stop()
        assert wd.running is False

    asyncio.run(_main())


def test_the_sampler_thread_does_not_outlive_stop():
    wd = LoopLagWatchdog(interval_s=0.02, sampler_interval_s=0.02)
    before = threading.active_count()

    async def _main():
        wd.start()
        await asyncio.sleep(0.1)
        await wd.stop()

    asyncio.run(_main())
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and threading.active_count() > before:
        time.sleep(0.02)
    assert threading.active_count() <= before


# ── the process-wide accessor ────────────────────────────────────────────────

def test_get_watchdog_is_a_singleton_and_reset_clears_it():
    reset_watchdog()
    try:
        a = get_watchdog()
        b = get_watchdog()
        assert a is b
        reset_watchdog()
        assert get_watchdog() is not a
    finally:
        reset_watchdog()
