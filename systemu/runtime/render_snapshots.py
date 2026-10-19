"""The observations the render READS instead of computing (D-3).

WHY THIS MODULE EXISTS
----------------------
``provider_snapshot`` took ONE blocking call off the dashboard render path (D4:
a loopback socket connect inside ``_build_layout``). The renderer kept stalling
on 0.10.31 anyway -- six 30 s CDP screenshot timeouts, clicks eaten while the
sidebar showed the reconnect spinner -- so the loop was still being blocked by
something else. Two things, measured on this box:

* ``health_banner._scan_daemon_processes`` -- ``psutil.process_iter`` over
  EVERY process on the machine, plus ``cwd()``/``exe()``/liveness per candidate.
  1622 ms inside one ``_build_layout``, and 695-1192 ms on repeat. A 20 s TTL
  sat in front of it, which is precisely the D4 shape: the memo bounds how
  OFTEN the cost is paid, not WHERE. Every 20 s one unlucky page paint froze
  the event loop for over a second.
* ``command_palette.build_index`` -- the whole capability index plus the table
  projection, rebuilt from the vault on EVERY route. 286 ms per render, with no
  memo at all.

Together with the availability-probe memo they accounted for a warm
``_build_layout`` of 272.6 ms and cold spikes to 1.98 s, on every page.

THE PROPERTY
------------
    NO RENDER RECOMPUTES AN OBSERVATION IT COULD HAVE READ.

    A producer runs on the refresher thread and PUBLISHES a frozen snapshot;
    the render reads that snapshot and never runs the producer. A render that
    finds no snapshot says so (or shows less), NUDGES a refresh, and returns --
    it never pays the cost itself, because paying it "just this once" is how
    both offenders above got onto the loop in the first place.

WHY MOVING IT OFF THE LOOP IS NOT A LIE
---------------------------------------
Measured, not assumed: the psutil scan takes 1251 ms on a worker thread, and
during it an ``asyncio.sleep(0.01)`` heartbeat on the main loop drifted by a
median of 22 ms and a worst case of 36 ms (idle baseline 6 ms / 8 ms). psutil
releases the GIL for the Win32 calls that dominate, so a 1251 ms freeze becomes
a 36 ms hiccup. A producer that did NOT release the GIL would have to be moved
to a process, and this module would be the wrong answer for it.

WHAT A MISSING SNAPSHOT MEANS
-----------------------------
"We have not looked yet" -- the same third answer ``provider_snapshot`` keeps,
and for the same reason: it can only ever WITHHOLD a claim. An absent
daemon-scan snapshot yields NO daemon records, so the multi-daemon diagnosis
does not fire; it cannot invent a port race. An absent palette index yields the
static navigation entries, so the palette still navigates; it cannot invent a
tool.

ONE THREAD, NOT ONE PER PRODUCER
--------------------------------
Both producers want the same cadence and neither is urgent, so they share a
thread and a nudge. A slow producer therefore delays its sibling by its own
duration -- acceptable at ~1.5 s against a 30 s interval, and stated here so
the tradeoff is not rediscovered as a bug.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

#: The registered producer names. Strings rather than an enum because they are
#: also the keys a test reads back, and a typo in a test must fail loudly.
DAEMON_SCAN = "daemon_scan"
PALETTE_INDEX = "palette_index"

#: Beyond this a snapshot is not replayed at all -- a dashboard left open
#: overnight beside a refresher that died must not answer from yesterday.
#: Same bound, same reason, as ``provider_snapshot.MAX_REPLAY_AGE_S``.
MAX_REPLAY_AGE_S = 600.0


def refresh_interval_s() -> float:
    """The shared cadence. ONE source, so the two snapshot families cannot
    drift apart and an operator has one knob, not three."""
    from systemu.runtime import provider_snapshot as _ps
    return _ps.refresh_interval_s()


# ── the snapshot types ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class _Taken:
    """The half every snapshot shares: WHEN it was observed."""

    taken_at: float = 0.0
    interval_s: float = 30.0

    def age_s(self, now: Optional[float] = None) -> float:
        """Seconds since the observation. Never negative."""
        current = time.monotonic() if now is None else now
        return max(0.0, float(current) - float(self.taken_at))

    @property
    def stale(self) -> bool:
        """Older than the interval it was supposed to be refreshed within."""
        return self.age_s() > float(self.interval_s)


@dataclass(frozen=True)
class DaemonScanSnapshot(_Taken):
    """ONE OBSERVED pass of ``health_banner._scan_daemon_processes``.

    ``procs`` holds the ``DaemonProcess`` records exactly as the scan produced
    them -- PID-verified-live and de-duplicated there, not re-derived here.
    ``pruned`` and ``launchers`` ride along because they are evidence ABOUT
    this same pass: a support conversation that reads a count from one
    observation and a prune list from another is back to arguing about
    processes that came and went in between.
    """

    procs: Tuple = ()
    pruned: Tuple = ()
    launchers: Tuple = ()

    @property
    def count(self) -> int:
        return len(self.procs)


@dataclass(frozen=True)
class PaletteIndexSnapshot(_Taken):
    """ONE OBSERVED build of the Ctrl+K palette index.

    ``entries`` are ``command_palette.PaletteEntry`` values as that module
    minted them. This side never invents an entry: an absent snapshot renders
    the static navigation rows only, which is a SMALLER palette, never a wrong
    one.
    """

    entries: Tuple = ()


# ── the store ───────────────────────────────────────────────────────────────

_lock = threading.RLock()
_snapshots: Dict[str, object] = {}
_producers: Dict[str, Callable[[], None]] = {}
_refresher: dict = {"thread": None, "stop": None, "wake": None}


def register(name: str, produce: Callable[[], None]) -> None:
    """Register the producer for ``name``. Idempotent, and cheap enough to call
    from a render -- it is a dict write under a lock.

    A producer PUBLISHES; its return value is ignored. That is deliberate: the
    daemon scan already had to publish from inside itself so that a caller
    which runs the scan directly (five existing test files do) leaves the store
    holding the freshest observation rather than an older one.
    """
    if type(name) is not str or not name:
        return
    if not callable(produce):
        return
    with _lock:
        _producers[name] = produce


def publish(name: str, snapshot) -> None:
    """Store ``snapshot`` as the current observation for ``name``."""
    if type(name) is not str or not name:
        return
    with _lock:
        _snapshots[name] = snapshot


def current(name: str, *, _now: Optional[float] = None):
    """The published snapshot for ``name``, or None -- "we have not looked yet".

    A snapshot past :data:`MAX_REPLAY_AGE_S` is withheld rather than replayed.
    Both refusals can only WITHHOLD a claim.
    """
    with _lock:
        snap = _snapshots.get(name)
    if snap is None:
        return None
    try:
        if snap.age_s(_now) > MAX_REPLAY_AGE_S:
            return None
    except Exception:
        return None
    return snap


def clear(name: Optional[str] = None) -> None:
    """Drop one published snapshot, or all of them (tests, shutdown)."""
    with _lock:
        if name is None:
            _snapshots.clear()
        else:
            _snapshots.pop(name, None)


def daemon_scan() -> Optional[DaemonScanSnapshot]:
    """The daemon-scan snapshot, or None. Type-pinned in THIS frame (DEC-36):
    anything else in the slot is not an answer about running daemons."""
    snap = current(DAEMON_SCAN)
    return snap if type(snap) is DaemonScanSnapshot else None


def palette_index() -> Optional[PaletteIndexSnapshot]:
    """The palette-index snapshot, or None. Type-pinned in THIS frame."""
    snap = current(PALETTE_INDEX)
    return snap if type(snap) is PaletteIndexSnapshot else None


def taken_at_key() -> tuple:
    """``(name, taken_at)`` for every published snapshot, ordered.

    THE CHANGE DETECTOR the open-page timers compare against (D-11). It reads
    only the timestamps, so a timer tick that finds nothing new costs a lock
    and a tuple build -- never a rebuild of anything.
    """
    with _lock:
        items = list(_snapshots.items())
    out = []
    for name, snap in items:
        taken = getattr(snap, "taken_at", None)
        out.append((name, taken if type(taken) is float else None))
    return tuple(sorted(out, key=lambda pair: pair[0]))


# ── running the producers ───────────────────────────────────────────────────

def refresh_now(name: Optional[str] = None) -> int:
    """Run the registered producer(s) synchronously. NEVER call from a render.

    Returns how many producers ran to completion. A producer that raises is
    logged and skipped: one broken observation must not strand the others at
    "we have not looked yet".
    """
    with _lock:
        if name is None:
            todo = list(_producers.items())
        else:
            fn = _producers.get(name)
            todo = [] if fn is None else [(name, fn)]
    done = 0
    for producer_name, fn in todo:
        try:
            fn()
            done += 1
        except Exception:
            logger.debug("[RenderSnapshots] producer %r failed", producer_name,
                         exc_info=True)
    return done


def _loop(interval_s: float, stop_ev: threading.Event,
          wake_ev: threading.Event) -> None:
    """Refresh, then wait for the interval OR an on-demand nudge.

    The nudge is CLEARED BEFORE the refresh, exactly as in
    ``provider_snapshot._loop`` and for the same reason: clearing it afterwards
    swallows every nudge that arrived while a producer was in flight, and a
    producer here runs for over a second.
    """
    while not stop_ev.is_set():
        wake_ev.clear()
        try:
            refresh_now(None)
        except Exception:
            logger.debug("[RenderSnapshots] refresh round failed", exc_info=True)
        wake_ev.wait(interval_s)


def start_refresher(interval_s: Optional[float] = None) -> bool:
    """Start the once-per-process refresher thread. True if THIS call started it.

    Idempotent and cheap enough to call from a render: an already-running
    refresher costs a lock and a liveness read. A daemon thread, so it can
    never hold the process open.
    """
    global _refresher
    every = refresh_interval_s() if interval_s is None else float(interval_s)
    with _lock:
        thread = _refresher.get("thread")
        if thread is not None and thread.is_alive():
            return False
        stop_ev, wake_ev = threading.Event(), threading.Event()
        thread = threading.Thread(
            target=_loop, args=(every, stop_ev, wake_ev),
            name="systemu-render-snapshots", daemon=True)
        _refresher = {"thread": thread, "stop": stop_ev, "wake": wake_ev}
        thread.start()
        return True


def request_refresh() -> bool:
    """Ask the refresher for a fresh round. NEVER blocks, never produces.

    False means no refresher is running, so nobody was nudged -- the caller has
    not been told a refresh is coming when it is not.
    """
    with _lock:
        thread = _refresher.get("thread")
        wake_ev = _refresher.get("wake")
    if thread is None or wake_ev is None or not thread.is_alive():
        return False
    wake_ev.set()
    return True


def stop_refresher(timeout: float = 5.0) -> None:
    """Stop the refresher thread and wait for it (tests, shutdown)."""
    global _refresher
    with _lock:
        thread = _refresher.get("thread")
        stop_ev = _refresher.get("stop")
        wake_ev = _refresher.get("wake")
        _refresher = {"thread": None, "stop": None, "wake": None}
    if stop_ev is not None:
        stop_ev.set()
    if wake_ev is not None:
        wake_ev.set()
    if thread is not None and thread.is_alive():
        thread.join(timeout)
