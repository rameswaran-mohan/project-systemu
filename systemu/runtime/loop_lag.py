"""R-UX2 — the event-loop lag watchdog (SPEC Part II §15-UX **UX-9(a)**).

The nicegui dashboard is websocket-based: when the asyncio event loop is blocked
it misses heartbeats, the browser shows "Connection lost", and navigation janks.
UX-9(a) sets the rule (*the loop is never blocked >50ms*) and names two
mechanisms — an **offload lint** (``tools/lint_offload.py``) and this
**loop-lag watchdog**.

This module is also the **meter the DEC-20c pivot triggers read**:

* **PIV-1** fires when ``loop-lag p95 > 250ms in normal use`` — that is a
  *sustained* reading, so it is ``ring_p95_ms`` (the whole retained ring,
  ~2 minutes), never the 5-second chip window;
* **PIV-2** fires on sustained spikes *attributable to runtime work* — and the
  spec is explicit that the watchdog "logs the offending stacks — attribution is
  in the data, not in argument".

Attribution is why this is two cooperating parts rather than one timer:

* a **heartbeat coroutine** on the event loop measures how late
  ``asyncio.sleep(interval)`` actually returns (the overshoot *is* the lag); and
* a **sampler daemon thread** watches the heartbeat's timestamp go stale and,
  while the loop is *still* blocked, grabs the loop thread's Python stack via
  ``sys._current_frames()``.

A heartbeat alone can only say "300ms went missing"; by the time it runs again
the culprit has returned. The sampler is what turns a number into a name.

TWO INVARIANTS THIS MODULE EXISTS TO HOLD
=========================================

**1. Every number is stamped with the window it came from.** An earlier revision
of this file computed the chip's *state* from the last ~5 seconds, printed a p95
computed over the whole ~2-minute ring as that state's justification, and printed
an *all-time* breach counter beside both. Three windows, one chip. It rendered
``RESPONSIVE :: UI lag p95 900ms · 200 stall(s)`` after 200 samples at 900ms
followed by 50 calm ones, and ``UNDER LOAD :: UI lag p95 1ms`` for the inverse.
Both readings were internally consistent and both were nonsense.

So there is no bare ``p95_ms`` key here, and there never should be. Every metric
carries its window in its own name — ``recent_*`` (the ONE window the load state
is computed from), ``ring_*`` (the retained history PIV-1 reads), ``session_*``
(all-time counters) — and ``_load_chip`` may only print ``recent_*`` as the
justification for the state it is announcing.

**2. A frozen meter cannot emit the healthy value.** The heartbeat's ``except
Exception`` used to swallow its own death while leaving ``_running`` True, and
``snapshot()`` never consulted ``_last_beat`` — so a watchdog whose heartbeat
task had been killed served ``measured: True``, ``load_state: "normal"`` and a
p95 from a permanently frozen ring, forever. Liveness is therefore derived from
``_last_beat`` (stamped by :meth:`record`, i.e. by data actually arriving) and
**not** from the ``_running`` flag, which is precisely the signal that lied.
``measured`` and ``load_state == "normal"`` are both unreachable when the last
sample is older than ``stale_after_s``.

**The watchdog must never become its own blocker.** Everything on the hot path
is in-memory: a bounded ring buffer and a few counters under a short-lived lock.
There is no disk I/O here — ``MetricsStore.incr`` does a full read + atomic
re-write per call, so calling it from the heartbeat would add loop-blocking file
I/O to the thing measuring loop-blocking file I/O.
"""
from __future__ import annotations

import logging
import math
import sys
import threading
import time
import traceback
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, NamedTuple, Optional, Tuple

import asyncio

logger = logging.getLogger(__name__)

# UX-9(a): the loop is never blocked >50ms; >250ms is a logged breach.
_DEFAULT_INTERVAL_S = 0.1
_DEFAULT_BREACH_MS = 250.0
_DEFAULT_SAMPLER_INTERVAL_S = 0.05
# ~2 minutes of history at the default 100ms heartbeat. This is the ring PIV-1
# reads; it is deliberately much longer than the chip window.
_DEFAULT_CAPACITY = 1200
# UX-9(f): the "system under load" chip — loop-lag OR CPU.
_DEFAULT_LOAD_LAG_MS = 100.0
_DEFAULT_LOAD_CPU_PCT = 85.0
# THE window. Defined in seconds, not samples, so "recent" means the same thing
# whatever the heartbeat interval is — and so a meter that stops feeding it
# empties the window instead of freezing it.
_DEFAULT_LOAD_WINDOW_S = 5.0
# Below this many samples in the window we do not claim a state at all: a p95
# over three observations is not a measurement, and "unknown" is the honest
# answer. It must never round down to "normal".
_DEFAULT_MIN_WINDOW_SAMPLES = 10
# No sample for this long ⇒ the meter is dead OR the loop is blocked right now.
# Either way we do not know that the UI is responsive.
_DEFAULT_STALE_AFTER_S = 2.0
# psutil.cpu_percent(interval=None) reports utilisation *since the previous
# call*. Polled every 50ms it is measuring a 50ms slice, which is below any
# useful reliability floor: on an idle box 40 consecutive 50ms polls spanned
# 9.4%–91.7% while the 1-second reading over the same period was 50.4%. One of
# those forty readings crossed the 85% load threshold on its own. So the poll is
# rate-limited to a psutil-valid interval and the reported figure is a mean.
_DEFAULT_CPU_POLL_S = 1.0
_DEFAULT_CPU_SMOOTHING = 5          # ⇒ a ~5s mean at the default poll interval
_DEFAULT_MAX_STACKS = 5

_LOAD_STATES = ("normal", "busy", "unknown")


class _CpuReading(NamedTuple):
    """One CPU observation and the interval it actually covers.

    ``psutil.cpu_percent(interval=None)`` reports utilisation *since the previous
    call*, so a reading is a statement about the span ``(covers_from, ts]`` — not
    about the instant ``ts``. Both ends are recorded because both are needed and
    neither may be assumed: ``ts`` is what EXPIRES the reading, and
    ``covers_from`` is what makes the reported window a measured span rather than
    a count multiplied by a nominal interval.
    """
    covers_from: float
    ts: float
    pct: float


class _CpuView(NamedTuple):
    """What the retained CPU readings support. ``percent is None`` ⇒ UNMEASURED.

    Deliberately NOT a bare ``(percent, window)`` pair: the count is reported as
    a count and the span as a span, because conflating them is the defect this
    type exists to prevent.
    """
    percent: Optional[float]
    span_s: Optional[float]
    age_s: Optional[float]
    readings: int


def _percentile(values: List[float], q: float) -> float:
    """Nearest-rank percentile over an already-sorted list (q in 0..100)."""
    if not values:
        raise ValueError("percentile of an empty sequence")
    rank = int(math.ceil((q / 100.0) * len(values)))
    return values[max(0, min(len(values) - 1, rank - 1))]


def _round1(v: Optional[float]) -> Optional[float]:
    return None if v is None else round(float(v), 1)


class LoopLagWatchdog:
    """Measures asyncio event-loop lag and attributes stalls to a stack.

    Every knob is injectable so tests can drive it fast and hermetically; the
    defaults are the spec's numbers. ``clock`` is injectable because the two
    invariants above (window boundaries, staleness) are *time* predicates —
    testing them against a real clock would mean sleeping for seconds per case.
    """

    def __init__(
        self,
        *,
        interval_s: float = _DEFAULT_INTERVAL_S,
        breach_ms: float = _DEFAULT_BREACH_MS,
        sampler_interval_s: float = _DEFAULT_SAMPLER_INTERVAL_S,
        capacity: int = _DEFAULT_CAPACITY,
        load_lag_ms: float = _DEFAULT_LOAD_LAG_MS,
        load_cpu_pct: float = _DEFAULT_LOAD_CPU_PCT,
        load_window_s: float = _DEFAULT_LOAD_WINDOW_S,
        min_window_samples: int = _DEFAULT_MIN_WINDOW_SAMPLES,
        stale_after_s: float = _DEFAULT_STALE_AFTER_S,
        cpu_poll_s: float = _DEFAULT_CPU_POLL_S,
        cpu_smoothing: int = _DEFAULT_CPU_SMOOTHING,
        cpu_max_age_s: Optional[float] = None,
        max_stacks: int = _DEFAULT_MAX_STACKS,
        capture_stacks: bool = True,
        clock=time.monotonic,
    ) -> None:
        self._interval_s = float(interval_s)
        self._breach_ms = float(breach_ms)
        self._sampler_interval_s = float(sampler_interval_s)
        self._load_lag_ms = float(load_lag_ms)
        self._load_cpu_pct = float(load_cpu_pct)
        self._load_window_s = float(load_window_s)
        self._min_window_samples = int(min_window_samples)
        self._stale_after_s = float(stale_after_s)
        self._cpu_poll_s = float(cpu_poll_s)
        self._max_stacks = int(max_stacks)
        self._capture_stacks = bool(capture_stacks)
        self._clock = clock

        self._lock = threading.Lock()
        # (timestamp, lag_ms) — the timestamp is what makes "the last 5 seconds"
        # a real window rather than a sample count standing in for one.
        self._samples: Deque[Tuple[float, float]] = deque(maxlen=int(capacity))
        self._session_samples = 0
        self._session_breaches = 0
        self._session_max_lag_ms: Optional[float] = None
        self._stacks: List[Dict[str, Any]] = []
        # (covers_from, ts, pct) — timestamped for the SAME reason `_samples` is:
        # "the last 5 seconds" has to be a real window. An untimestamped deque
        # cannot expire, so a reading taken before psutil died was still being
        # served, and still labelled a 5s average, ten minutes later.
        self._cpu_readings: Deque[_CpuReading] = deque(
            maxlen=max(1, int(cpu_smoothing)))
        self._cpu_max_age_s = float(
            cpu_poll_s * max(1, int(cpu_smoothing))
            if cpu_max_age_s is None else cpu_max_age_s)
        # When we last ATTEMPTED a poll (the rate limit) and when psutil last
        # actually RETURNED (the interval a reading covers). Separate, because
        # conflating them let a failing psutil bypass its own rate limit.
        self._cpu_polled_at: Optional[float] = None
        self._cpu_measured_at: Optional[float] = None

        # cross-thread heartbeat liveness (written by record(), read by the
        # sampler AND by snapshot()). This is the ONLY liveness signal: see the
        # module docstring on why `_running` is not trusted for it.
        self._last_beat: Optional[float] = None
        self._loop_thread_ident: Optional[int] = None
        self._stall_logged = False
        self._episode = 0

        self._stop_event = threading.Event()
        self._sampler_thread: Optional[threading.Thread] = None
        self._heartbeat_task: Optional["asyncio.Task"] = None
        self._running = False

    # ── lifecycle ────────────────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        """Whether :meth:`start` has been called and :meth:`stop` has not.

        NOT a liveness signal, and deliberately not used as one: this flag stayed
        True across a dead heartbeat, which is the whole reason ``snapshot()``
        keys off ``_last_beat`` instead.
        """
        return self._running

    def start(self, loop: "asyncio.AbstractEventLoop | None" = None) -> None:
        """Schedule the heartbeat on ``loop`` and start the sampler thread.

        Idempotent: a second call while running is a no-op (so wiring it from
        both a startup hook and a test cannot double-count lag).
        """
        if self._running:
            return
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                logger.debug("[LoopLag] no running event loop — watchdog not started")
                return

        self._stop_event.clear()
        self._stall_logged = False
        self._last_beat = self._clock()
        self._loop_thread_ident = threading.get_ident()
        self._running = True
        self._heartbeat_task = loop.create_task(self._heartbeat())
        self._sampler_thread = threading.Thread(
            target=self._sampler, name="loop-lag-sampler", daemon=True)
        self._sampler_thread.start()

    async def stop(self) -> None:
        """Stop the heartbeat and join the sampler. Safe if never started.

        ``_last_beat`` is deliberately left alone: the last few seconds of
        readings were real, and they age out on their own via ``stale_after_s``.
        """
        self._running = False
        self._stop_event.set()
        task, self._heartbeat_task = self._heartbeat_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: B014 - best effort
                pass
        thread, self._sampler_thread = self._sampler_thread, None
        if thread is not None:
            thread.join(timeout=2.0)

    # ── the two cooperating parts ────────────────────────────────────────────

    async def _heartbeat(self) -> None:
        """On the event loop: sleep a known interval, record the overshoot."""
        try:
            while not self._stop_event.is_set():
                t0 = self._clock()
                await asyncio.sleep(self._interval_s)
                elapsed = self._clock() - t0
                lag_ms = max(0.0, (elapsed - self._interval_s) * 1000.0)
                # A new episode number retires the stall we just came out of, so
                # the next stall is attributed independently of this one.
                self._stall_logged = False
                self._episode += 1
                # record() stamps `_last_beat`: liveness IS "a sample arrived",
                # so the two can never drift apart.
                self.record(lag_ms)
        except asyncio.CancelledError:  # normal shutdown
            raise
        except Exception:               # a dead watchdog must not kill the app
            # This swallow is why `snapshot()` must not trust `_running`: control
            # reaches here, the flag stays True, and nothing else marks the meter
            # dead. Staleness is what makes that survivable — samples stop
            # arriving, `_last_beat` ages out, and the reading goes to "unknown"
            # instead of staying "normal" forever.
            logger.warning("[LoopLag] heartbeat stopped on error — loop-lag "
                           "readings will go stale and report as unknown",
                           exc_info=True)

    def _sampler(self) -> None:
        """On a daemon thread: catch the loop *while* it is blocked and name it."""
        while not self._stop_event.wait(self._sampler_interval_s):
            try:
                self._sample_once()
            except Exception:
                logger.debug("[LoopLag] sampler iteration failed", exc_info=True)

    def _sample_once(self) -> None:
        self._refresh_cpu()
        last = self._last_beat
        if last is None or not self._capture_stacks:
            return
        stall_ms = (self._clock() - last) * 1000.0
        if stall_ms <= self._breach_ms:
            return
        # Re-sample for the WHOLE stall, keeping that episode's deepest
        # observation (see _add_stack). Capturing only the first observation
        # attributes the stall to whatever happened to be running `breach_ms`
        # in — which is frequently the frame *before* the culprit, or an idle
        # loop between callbacks. The culprit is reliably on the stack later in
        # the stall, not at its leading edge.
        episode = self._episode
        stack = self._loop_thread_stack()
        if stack:
            self._add_stack(stack, stall_ms, episode)
        if not self._stall_logged:      # log once per episode, not per tick
            self._stall_logged = True
            logger.warning("[LoopLag] event loop blocked >%.0fms by:\n%s",
                           self._breach_ms, stack)

    def _loop_thread_stack(self) -> str:
        """The loop thread's Python stack, verbatim and UNFILTERED.

        An earlier revision stripped frames whose text contained the watchdog's
        own filename. That was both buggy and pointless. Buggy: it was a
        substring match, so it also erased frames from any *other* file whose
        path merely ends with the same name — the suite caught this, because
        ``tests/test_rux2_loop_lag.py`` is exactly such a file and the culprit
        frame vanished from its own attribution test. Pointless: a watchdog
        frame can only appear on the loop thread's stack when the watchdog
        ITSELF is what blocked the loop, which is precisely the thing an
        attribution report must not hide.
        """
        ident = self._loop_thread_ident
        if ident is None:
            return ""
        frame = sys._current_frames().get(ident)
        if frame is None:
            return ""
        return "".join(traceback.format_stack(frame)).strip()

    # ── CPU (rate-limited to a psutil-valid interval, then smoothed) ──────────

    def _refresh_cpu(self) -> None:
        """Poll CPU at most once per ``cpu_poll_s`` and keep a rolling mean.

        Called from the 50ms sampler tick, so the rate limit is what stops us
        asking psutil for a 50ms slice — see ``_DEFAULT_CPU_POLL_S`` for the
        measurement that motivates it. The first reading after a gap is
        DISCARDED: ``cpu_percent(interval=None)`` is documented to be
        meaningless on its first call (it returns 0.0), and 0.0 is a
        healthy-looking number that would bias the chip toward "normal".

        **The rate limit is charged to the ATTEMPT, not to the success.** Both
        failure paths below used to return before stamping ``_cpu_polled_at``,
        so a persistently failing psutil defeated the very limit meant to
        protect it: measured at 100 calls over 5 simulated seconds against a
        1.0s limit, each one formatting a traceback via ``exc_info=True``.
        """
        now = self._clock()
        last_poll = self._cpu_polled_at
        if last_poll is not None and (now - last_poll) < self._cpu_poll_s:
            return
        self._cpu_polled_at = now       # BEFORE the fallible work — see above
        try:
            import psutil
        except Exception:
            logger.debug("[LoopLag] psutil unavailable — CPU unmeasured")
            return
        try:
            pct = float(psutil.cpu_percent(interval=None))
        except Exception:
            logger.debug("[LoopLag] cpu_percent unavailable", exc_info=True)
            return

        measured_at, self._cpu_measured_at = self._cpu_measured_at, now
        if measured_at is None:
            return                      # priming call — covers an unknown span
        if (now - measured_at) > 2.0 * self._cpu_poll_s:
            # psutil was failing or the process stalled, so this reading is an
            # average over a much longer span than the one we would file it
            # under. Same reason as the priming discard: re-prime instead.
            return
        self._add_cpu_reading(pct, ts=now, covers_from=measured_at)

    def _add_cpu_reading(
        self,
        pct: float,
        *,
        ts: Optional[float] = None,
        covers_from: Optional[float] = None,
    ) -> None:
        """Append one reading. ``covers_from`` is the previous poll's timestamp.

        Production always passes the REAL previous poll time. It defaults to one
        nominal poll interval back only for direct injection in tests, and that
        default is the sole place a nominal interval is used at all.
        """
        at = self._clock() if ts is None else float(ts)
        frm = (at - self._cpu_poll_s) if covers_from is None else float(covers_from)
        with self._lock:
            self._cpu_readings.append(_CpuReading(frm, at, float(pct)))

    def _cpu_view(self, now: Optional[float] = None) -> _CpuView:
        """The mean over readings still inside ``cpu_max_age_s``, and its SPAN.

        Readings expire by TIME, exactly like ``_samples``. Before that, they
        expired never: a reading taken just before psutil died was still being
        served ten minutes later, still labelled a 5s average — and one 99%
        reading pinned the chip to "busy" for the life of the process.

        ``span_s`` is measured end-to-end across the retained readings
        (``newest.ts - oldest.covers_from``), not ``len(readings) * poll_s``.
        The count is returned separately, as a count.
        """
        at = self._clock() if now is None else float(now)
        cutoff = at - self._cpu_max_age_s
        with self._lock:
            while self._cpu_readings and self._cpu_readings[0].ts < cutoff:
                self._cpu_readings.popleft()
            readings = list(self._cpu_readings)
        if not readings:
            return _CpuView(None, None, None, 0)
        mean = sum(r.pct for r in readings) / len(readings)
        return _CpuView(
            percent=mean,
            span_s=readings[-1].ts - readings[0].covers_from,
            age_s=max(0.0, at - readings[-1].ts),
            readings=len(readings),
        )

    # ── recording ────────────────────────────────────────────────────────────

    def record(self, lag_ms: float, *, ts: Optional[float] = None) -> None:
        """Record one lag observation (also the injection seam for tests).

        Stamps ``_last_beat``. That is not bookkeeping — it is the definition of
        liveness this module relies on: the meter is live exactly when samples
        are still arriving, so a test that drives ``record()`` exercises the
        same liveness path production does.
        """
        lag = float(lag_ms)
        at = self._clock() if ts is None else float(ts)
        with self._lock:
            self._samples.append((at, lag))
            self._session_samples += 1
            if self._session_max_lag_ms is None or lag > self._session_max_lag_ms:
                self._session_max_lag_ms = lag
            if lag > self._breach_ms:
                self._session_breaches += 1
        self._last_beat = at

    def _add_stack(self, stack: str, stall_ms: float, episode: int) -> None:
        """Record one stall observation: at most one entry per stall episode
        (its deepest), and overall the ``max_stacks`` WORST episodes."""
        entry = {
            "episode": int(episode),
            "stack": stack,
            "stall_ms": round(float(stall_ms), 1),
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        with self._lock:
            for existing in self._stacks:
                if existing["episode"] == episode:
                    if entry["stall_ms"] > existing["stall_ms"]:
                        existing.update(entry)
                    break
            else:
                self._stacks.append(entry)
            # keep the WORST offenders, not merely the most recent
            self._stacks.sort(key=lambda e: e["stall_ms"], reverse=True)
            del self._stacks[self._max_stacks:]

    # ── reading ──────────────────────────────────────────────────────────────

    def snapshot(self) -> Dict[str, Any]:
        """A JSON-safe render-data dict (it crosses the nicegui boundary).

        There is no ``p95_ms`` key and no ``under_load`` bool. Both were removed
        on purpose:

        * ``p95_ms`` did not say *over what*, and the chip printed it as the
          justification for a state computed from a different window;
        * ``under_load`` was a two-valued answer to a three-valued question, so
          "we do not know" and "the loop is fine" were the same ``False``. The
          single source of truth is ``load_state`` ∈ {normal, busy, unknown}.

        ``measured`` means "we have LAG samples AND they are still arriving"; it
        is what gates the ``recent_*`` fields. A watchdog whose heartbeat has
        died therefore reports ``measured: False`` and ``load_state: "unknown"``
        — never the healthy value — rather than serving a percentile off a
        permanently frozen ring.

        ``load_state`` is a COMPOSITE of two independently-measurable inputs
        (loop lag and CPU), so it has its own liveness and is not the same fact
        as ``measured``. ``measured: True`` with ``load_state: "unknown"`` is a
        real and useful combination: the lag numbers below it are genuine, and
        the state is withheld because CPU — the other input — is unmeasured.
        Observed pressure on EITHER input still yields "busy"; only the healthy
        value requires both. ``cpu_measured`` says which case applies, so a
        consumer can declare the gap instead of quietly dropping it.

        KNOWN BOUNDARY, stated rather than papered over: a stale reading is
        ambiguous. "The heartbeat task is dead" and "the loop is blocked right
        now" produce the identical observation — no sample for a while — and
        nothing here can tell them apart, so both render "unknown" and the chip
        names both possibilities. In particular a stall longer than
        ``stale_after_s`` *per beat* reads as "unknown" rather than "busy" while
        it is happening; the moment beats resume, those very large samples land
        in the window and it reads "busy". The one thing neither case can
        produce is "normal".
        """
        now = self._clock()
        with self._lock:
            pairs = list(self._samples)
            session_samples = self._session_samples
            session_breaches = self._session_breaches
            session_max = self._session_max_lag_ms
            stacks = [dict(s) for s in self._stacks]
        cpu_view = self._cpu_view(now)
        cpu = cpu_view.percent
        last_beat = self._last_beat

        age_s = None if last_beat is None else max(0.0, now - last_beat)
        live = age_s is not None and age_s <= self._stale_after_s

        base: Dict[str, Any] = {
            # liveness
            "running": self._running,
            "live": live,
            "stale": bool(pairs) and not live,
            "sample_age_s": _round1(age_s),
            "stale_after_s": self._stale_after_s,
            # the ONE window the load state is computed from
            "recent_window_s": self._load_window_s,
            "recent_samples": 0,
            "recent_span_s": None,
            "recent_p50_ms": None,
            "recent_p95_ms": None,
            "recent_max_ms": None,
            "recent_breaches": None,
            # the retained ring — PIV-1's meter, NOT the chip's
            "ring_samples": len(pairs),
            "ring_span_s": None,
            "ring_p95_ms": None,
            # all-time counters (not bounded by the ring)
            "session_samples": session_samples,
            "session_breaches": session_breaches,
            "session_max_lag_ms": _round1(session_max),
            # shared
            "breach_ms": self._breach_ms,
            # CPU is an INPUT to load_state, and it has its own liveness.
            # `cpu_measured` is published so the chip can DECLARE an unmeasured
            # CPU instead of silently omitting it from the justification.
            "cpu_measured": cpu is not None,
            "cpu_percent": _round1(cpu),
            "cpu_window_s": _round1(cpu_view.span_s),
            "cpu_age_s": _round1(cpu_view.age_s),
            "cpu_readings": cpu_view.readings,
            "cpu_max_age_s": self._cpu_max_age_s,
            "min_window_samples": self._min_window_samples,
            "worst_stacks": stacks,
        }

        if not pairs:
            base.update(measured=False, load_state="unknown",
                        load_reason="no samples recorded yet")
            return base

        ring_lags = sorted(lag for _ts, lag in pairs)
        base["ring_span_s"] = _round1(pairs[-1][0] - pairs[0][0])
        base["ring_p95_ms"] = _round1(_percentile(ring_lags, 95.0))

        if not live:
            # A frozen ring must not be able to produce a percentile that reads
            # as a live measurement. The recent-window fields stay None.
            base.update(measured=False, load_state="unknown",
                        load_reason="no sample for "
                                    f"{base['sample_age_s']}s "
                                    f"(stale after {self._stale_after_s}s)")
            return base

        cutoff = now - self._load_window_s
        recent_pairs = [(ts, lag) for ts, lag in pairs if ts >= cutoff]
        recent = sorted(lag for _ts, lag in recent_pairs)
        base["recent_samples"] = len(recent)
        if recent_pairs:
            base["recent_span_s"] = _round1(
                recent_pairs[-1][0] - recent_pairs[0][0])

        if not recent:
            base.update(measured=False, load_state="unknown",
                        load_reason="no samples in the last "
                                    f"{self._load_window_s}s")
            return base

        # EVERY number below comes from `recent` — the same window the state is
        # decided on. That identity is the fix for the three-window chip.
        recent_p95 = _percentile(recent, 95.0)
        lag_pressure = recent_p95 >= self._load_lag_ms
        cpu_pressure = cpu is not None and cpu >= self._load_cpu_pct

        if not (lag_pressure or cpu_pressure) \
                and len(recent) < self._min_window_samples:
            # Too little evidence to claim the HEALTHY value. Note the guard is
            # deliberately one-sided: thin data may still establish *pressure*
            # (a heavily lagged loop produces FEWER samples per second, because
            # each beat takes interval + lag — so "busy" is exactly the state a
            # sample-count guard would otherwise suppress). It exists to stop us
            # saying "responsive" on three observations, not to hide trouble.
            #
            # No percentile is published here either: a p95 over three samples
            # is not a measurement, and an unpublished number cannot be printed
            # as the justification for anything.
            base.update(measured=False, load_state="unknown",
                        load_reason=f"only {len(recent)} sample(s) in the last "
                                    f"{self._load_window_s}s "
                                    f"(need {self._min_window_samples})")
            return base

        # The lag half IS measured, so publish its numbers whatever the
        # composite concludes — withholding real measurements would only make
        # the "unknown" below less informative.
        base.update(
            measured=True,
            recent_p50_ms=_round1(_percentile(recent, 50.0)),
            recent_p95_ms=_round1(recent_p95),
            recent_max_ms=_round1(recent[-1]),
            recent_breaches=sum(1 for lag in recent if lag > self._breach_ms),
        )

        # THREE states, because `load_state` is a composite of TWO inputs and a
        # half-measured composite is not a healthy one. `cpu_pressure` was
        # `cpu is not None and cpu >= threshold`, which reads an UNMEASURED CPU
        # as "no pressure" — so with no psutil at all the whole chip reported
        # "normal", measured=True, on a single measured input.
        #
        # The asymmetry is deliberate and matches the min-samples guard above:
        # observed pressure is CONCLUSIVE (a lagging loop is lagging whatever
        # the CPU is doing), but the healthy value requires BOTH inputs.
        if lag_pressure or cpu_pressure:
            base.update(load_state="busy", load_reason=None)
        elif cpu is None:
            base.update(
                load_state="unknown",
                load_reason=(
                    "UI lag is fine, but CPU is not being measured "
                    f"(no reading in the last {_round1(self._cpu_max_age_s)}s — "
                    "psutil missing or failing), so system load is only half "
                    "observed"),
            )
        else:
            base.update(load_state="normal", load_reason=None)
        return base


# ── process-wide accessor ────────────────────────────────────────────────────

_WATCHDOG: Optional[LoopLagWatchdog] = None
_WATCHDOG_LOCK = threading.Lock()


def get_watchdog() -> LoopLagWatchdog:
    """The one process-wide watchdog (created on first use, never auto-started)."""
    global _WATCHDOG
    with _WATCHDOG_LOCK:
        if _WATCHDOG is None:
            _WATCHDOG = LoopLagWatchdog()
        return _WATCHDOG


def reset_watchdog() -> None:
    """Drop the singleton (tests; also signals any live sampler to stop)."""
    global _WATCHDOG
    with _WATCHDOG_LOCK:
        wd, _WATCHDOG = _WATCHDOG, None
    if wd is not None:
        wd._running = False
        wd._stop_event.set()
