"""S1b — approval-fatigue metrics store (spec DEC-11 / plan PLAN-11).

An append-only, best-effort side-store for the live action-gate's approval
metrics. The writers are the gate/decision chokepoints
(``approval/decision_queue.py`` post/resolve, the inbox's born-resolved
auto-policy row) and the S4 shadow meter; the reader is the ``/health``
"Approvals" section (see THE SURFACE below).

Persistence mirrors ``runtime/table_store.py``: a single JSON file, **atomic
writes** (tempfile + os.replace) so an interrupted write can't corrupt the
store, and **defensive reads** (a missing/corrupt file yields empty state,
never an exception).

Distinct from ``metrics_tracker.py`` (shadow-execution metrics) and
``tool_metrics.py`` (per-tool-call metrics) — this store is scoped to the
gate-approval-fatigue counters only:

  gate_cards_created, gate_cards_resolved, resolution_latency_ms (histogram
  samples), always_allow_grants, denies, bulk_approve_events, asks_created,
  asks_resolved, approvals_timed, approvals_unread

THE LATENCY SERIES IS BOUNDED (PLAN-11 / DEC-11). It used to grow by one float
per resolution, forever, inside a file that is read and rewritten whole on
every gate decision. It now keeps the last :data:`LATENCY_WINDOW` samples (the
window the median / p90 on ``/health`` are computed from) plus a running
``resolution_latency_count`` / ``_sum_ms`` / ``_min_ms`` / ``_max_ms`` updated
on EVERY sample, so the long-run aggregates stay exact after the window
rotates. A store written before the bound (the whole series, no aggregates) is
seeded exactly from its full series the first time it is read, and trimmed on
the next write.

EVERY READ-MODIFY-WRITE HOLDS ``_STATE_LOCK``. The rotation makes an unlocked
RMW worse than a lost +1: two threads that both load, append and rotate write
back two different windows (and on Windows the losing ``os.replace`` raises).
The lock is process-wide -- stores are constructed per call, so a per-instance
lock would serialise nothing -- and covers all three writers of
``metrics.json``. It does not reach another PROCESS (the CLI): only
``os.replace`` atomicity holds there, as for every lock-guarded side-store in
docs/CONC-MAP.md.

THE SURFACE. ``/health`` renders the "Approvals" section from a PUBLISHED
:class:`ApprovalMetricsSnapshot` (``runtime/render_snapshots``), produced off
the event loop by :func:`publish_approval_metrics`; the page never reads this
file itself.
"""
from __future__ import annotations

import json
import math
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

_COUNTER_KEYS = (
    "gate_cards_created",
    "gate_cards_resolved",
    "always_allow_grants",
    "denies",
    "bulk_approve_events",
    "asks_created",
    "asks_resolved",
)

# consecutive resolutions closer together than this (seconds) count as a
# "bulk approve" event (DEC-11 fatigue signal).
_BULK_WINDOW_S = 2.0

#: How many of the most recent latency samples the series keeps. The median and
#: p90 shown on /health are computed over THIS window and say so; the running
#: count/sum/min/max cover every sample ever recorded.
LATENCY_WINDOW = 500

#: An APPROVAL decided faster than this (seconds after the card was created) is
#: counted as approved WITHOUT READING the card. 3 s is a ruling, not a
#: measurement: about the time to read a card's title and one line of its
#: detail, so an approval under it was given on recognition alone.
FAST_APPROVAL_S = 3.0

# The choices that APPROVE a gate card, normalised (strip + lower). An
# ALLOWLIST, so an unrecognised label (a bespoke card's own option, a typed
# answer, "Reclassify effect...") is never counted as an approval: the fatigue
# share can under-count, never invent a rubber stamp. Spelled from the option
# sets in interface/command/gate.py and runtime/first_gate_review.py.
_APPROVAL_CHOICES = frozenset({
    "approve",
    "approve once",
    "always allow",
    "trust this tool for the session",
    "trust for session",
    "approve & install",
    "always allow the approvable ones",
})

_LATENCY_AGG_KEYS = (
    "resolution_latency_count",
    "resolution_latency_sum_ms",
    "resolution_latency_min_ms",
    "resolution_latency_max_ms",
)
_APPROVAL_KEYS = ("approvals_timed", "approvals_unread")

# Process-wide: every MetricsStore is constructed per call, so only a module
# lock serialises two writers of the same file. Re-entrant so a writer can never
# deadlock on a nested call.
_STATE_LOCK = threading.RLock()


def _default_state() -> Dict[str, Any]:
    state: Dict[str, Any] = {k: 0 for k in _COUNTER_KEYS}
    state["resolution_latency_ms"] = []
    state["resolution_latency_count"] = 0
    state["resolution_latency_sum_ms"] = 0.0
    state["resolution_latency_min_ms"] = None
    state["resolution_latency_max_ms"] = None
    for k in _APPROVAL_KEYS:
        state[k] = 0
    state["_last_resolution_ts"] = None
    return state


def _is_sample(x: Any) -> bool:
    """A usable latency sample: a finite int/float. ``type() in`` so a bool (an
    int subclass) is not one -- DEC-36."""
    return type(x) in (int, float) and math.isfinite(x)


def _count(x: Any) -> int:
    """A non-negative int counter, or 0 for anything else."""
    return x if type(x) is int and x >= 0 else 0


def _normalise_latency(state: Dict[str, Any], raw: Dict[str, Any]) -> None:
    """Make ``state``'s latency/approval fields well-typed, bounded and EXACT.

    Aggregates persisted alongside the series are trusted only when every one
    of them is well-typed and consistent with the window; otherwise they are
    re-derived from the series. For a file written before the bound that is
    EXACT -- such a file holds every sample ever taken -- which is why the
    seeding happens here, before anything trims the series.
    """
    series = raw.get("resolution_latency_ms")
    samples = ([float(x) for x in series if _is_sample(x)]
               if type(series) is list else [])
    count = raw.get("resolution_latency_count")
    total = raw.get("resolution_latency_sum_ms")
    lo = raw.get("resolution_latency_min_ms")
    hi = raw.get("resolution_latency_max_ms")
    trusted = (
        type(count) is int and count >= len(samples)
        and _is_sample(total)
        and ((count == 0 and lo is None and hi is None)
             or (count > 0 and _is_sample(lo) and _is_sample(hi)))
    )
    if not trusted:
        count = len(samples)
        total = float(sum(samples))
        lo = min(samples) if samples else None
        hi = max(samples) if samples else None
    state["resolution_latency_ms"] = samples[-LATENCY_WINDOW:]
    state["resolution_latency_count"] = count
    state["resolution_latency_sum_ms"] = float(total)
    state["resolution_latency_min_ms"] = None if lo is None else float(lo)
    state["resolution_latency_max_ms"] = None if hi is None else float(hi)
    for k in _APPROVAL_KEYS:
        state[k] = _count(raw.get(k))


def empty_snapshot() -> Dict[str, Any]:
    """What :meth:`MetricsStore.snapshot` returns for a store with nothing in it."""
    state = _default_state()
    out: Dict[str, Any] = {k: 0 for k in _COUNTER_KEYS}
    out["resolution_latency_ms"] = []
    for k in _LATENCY_AGG_KEYS + _APPROVAL_KEYS:
        out[k] = state[k]
    return out


class MetricsStore:
    """A directory-scoped append-only metrics side-store.

    ``base_dir`` is the directory the store lives in (e.g. a vault root, or —
    in tests — a ``tmp_path``); the store file is ``<base_dir>/metrics.json``.
    """

    def __init__(self, base_dir: Union[str, Path]):
        self._dir = Path(base_dir)
        self._path = self._dir / "metrics.json"

    # -- persistence -------------------------------------------------

    def _write_atomic(self, state: Dict[str, Any]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        text = json.dumps(state, indent=2)
        fd, tmp = tempfile.mkstemp(
            dir=str(self._dir), prefix=self._path.name + ".", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, str(self._path))
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _load(self) -> Dict[str, Any]:
        """Defensive: a broken/absent file ⇒ default (zeroed) state."""
        try:
            if not self._path.exists():
                return _default_state()
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return _default_state()
            state = _default_state()
            state.update(raw)
            _normalise_latency(state, raw)
            return state
        except Exception:
            return _default_state()

    # -- public API ----------------------------------------------------

    def incr(self, key: str) -> None:
        """Increment a simple integer counter by 1 (persisted)."""
        with _STATE_LOCK:
            state = self._load()
            state[key] = int(state.get(key, 0) or 0) + 1
            self._write_atomic(state)

    def incr_s4_shadow_meter(self, effect_class: Any, *, would_credit: bool) -> None:
        """R-A13b-1 — the SHADOW park-surface meter's cross-run counter.

        The SINGLE pinnable writer-site for the S4 credit-seam shadow meter (see
        docs/CONC-MAP.md + tests/test_conc_map_writer_ownership.py). Records, per
        effect-class, ``{would_stamp, would_credit, would_park}`` under the
        ``s4_shadow`` bucket: every call bumps ``would_stamp`` and exactly one of
        ``would_credit`` / ``would_park``. A None/blank effect-class buckets as
        ``"unknown"`` (mirrors the DEC-24 UNKNOWN-until-classified convention).
        RECORD-ONLY — this store never gates a credit. Runs on the shadow exec
        thread (same single writer as ``incr``); atomic + defensive, and holds
        ``_STATE_LOCK`` like the other two writers of this file."""
        with _STATE_LOCK:
            state = self._load()
            bucket = state.get("s4_shadow")
            if not isinstance(bucket, dict):
                bucket = {}
            ec = str(effect_class).strip().lower() if effect_class else "unknown"
            cell = bucket.get(ec)
            if not isinstance(cell, dict):
                cell = {"would_stamp": 0, "would_credit": 0, "would_park": 0}
            cell["would_stamp"] = int(cell.get("would_stamp", 0) or 0) + 1
            _outcome = "would_credit" if would_credit else "would_park"
            cell[_outcome] = int(cell.get(_outcome, 0) or 0) + 1
            bucket[ec] = cell
            state["s4_shadow"] = bucket
            self._write_atomic(state)

    def shadow_meter_snapshot(self) -> Dict[str, Any]:
        """The R-A13b-1 park-surface report: ``{effect_class: {would_stamp,
        would_credit, would_park}}``. Defensive — a missing/corrupt bucket ⇒ {}."""
        bucket = self._load().get("s4_shadow")
        return dict(bucket) if isinstance(bucket, dict) else {}

    def record_resolution(self, latency_ms: float, ts: float, choice: str = "") -> None:
        """Record a gate-card resolution: latency sample, resolved count,
        choice-specific counter, and bulk-approve detection against the
        persisted last-resolution timestamp.

        The latency sample joins the bounded window (samples beyond
        :data:`LATENCY_WINDOW` are dropped oldest-first) AND the running
        count/sum/min/max, so the long-run aggregates never depend on what the
        window still holds. A non-finite or non-numeric latency is not a sample:
        the card is still counted as resolved, but no latency figure moves.
        """
        with _STATE_LOCK:
            state = self._load()

            sample = float(latency_ms) if _is_sample(latency_ms) else None
            if sample is not None:
                samples: List[float] = list(state.get("resolution_latency_ms") or [])
                samples.append(sample)
                state["resolution_latency_ms"] = samples[-LATENCY_WINDOW:]
                state["resolution_latency_count"] = state["resolution_latency_count"] + 1
                state["resolution_latency_sum_ms"] = (
                    state["resolution_latency_sum_ms"] + sample)
                lo = state["resolution_latency_min_ms"]
                hi = state["resolution_latency_max_ms"]
                state["resolution_latency_min_ms"] = sample if lo is None else min(lo, sample)
                state["resolution_latency_max_ms"] = sample if hi is None else max(hi, sample)

            state["gate_cards_resolved"] = int(state.get("gate_cards_resolved", 0) or 0) + 1

            if choice == "Always allow":
                state["always_allow_grants"] = int(state.get("always_allow_grants", 0) or 0) + 1
            elif choice == "Deny":
                state["denies"] = int(state.get("denies", 0) or 0) + 1

            # The fatigue share: of the approvals we could TIME, how many were
            # given faster than a card can be read. Its denominator is only the
            # approvals recorded since this counter existed -- a legacy series
            # carries no choice, so it is never folded in after the fact.
            if (sample is not None and type(choice) is str
                    and choice.strip().lower() in _APPROVAL_CHOICES):
                state["approvals_timed"] = state["approvals_timed"] + 1
                if sample < FAST_APPROVAL_S * 1000.0:
                    state["approvals_unread"] = state["approvals_unread"] + 1

            last_ts = state.get("_last_resolution_ts")
            if last_ts is not None and (ts - last_ts) < _BULK_WINDOW_S:
                state["bulk_approve_events"] = int(state.get("bulk_approve_events", 0) or 0) + 1
            state["_last_resolution_ts"] = ts

            self._write_atomic(state)

    def snapshot(self) -> Dict[str, Any]:
        """Current counters, defensive: missing keys default to 0 / [] / None.

        ``resolution_latency_ms`` is the bounded window (at most
        :data:`LATENCY_WINDOW` most-recent samples); the ``resolution_latency_*``
        aggregates cover every sample ever recorded."""
        state = self._load()
        out = {k: state.get(k, 0) for k in _COUNTER_KEYS}
        out["resolution_latency_ms"] = list(state.get("resolution_latency_ms") or [])
        for k in _LATENCY_AGG_KEYS + _APPROVAL_KEYS:
            out[k] = state[k]
        return out


# -- the render snapshot /health reads (D-3: never read on the event loop) ----

#: The ``render_snapshots`` slot the approval metrics are published under.
APPROVAL_METRICS_SNAPSHOT = "approval_metrics"


@dataclass(frozen=True)
class ApprovalMetricsSnapshot:
    """ONE observed read of a vault's approval-metrics store.

    ``store_dir`` names WHICH store was read: the snapshot slot is process-wide,
    so a reader checks it against its own vault before trusting the numbers.
    ``metrics`` is :meth:`MetricsStore.snapshot` exactly as it was read."""

    taken_at: float = 0.0
    interval_s: float = 30.0
    store_dir: str = ""
    metrics: Dict[str, Any] = field(default_factory=dict)

    def age_s(self, now: Optional[float] = None) -> float:
        """Seconds since the observation. Never negative."""
        current = time.monotonic() if now is None else now
        return max(0.0, float(current) - float(self.taken_at))

    @property
    def stale(self) -> bool:
        """Older than the interval it was supposed to be refreshed within."""
        return self.age_s() > float(self.interval_s)


def publish_approval_metrics(base_dir: Union[str, Path]) -> ApprovalMetricsSnapshot:
    """THE PRODUCER: read the store and PUBLISH it. NEVER call from a render.

    Registered with ``render_snapshots`` by the /health page and run on the
    shared refresher thread, so the file read happens there and the page only
    ever reads the published value."""
    from systemu.runtime import render_snapshots as _rs

    store_dir = str(Path(base_dir))
    snap = ApprovalMetricsSnapshot(
        taken_at=time.monotonic(), interval_s=_rs.refresh_interval_s(),
        store_dir=store_dir, metrics=MetricsStore(store_dir).snapshot())
    _rs.publish(APPROVAL_METRICS_SNAPSHOT, snap)
    return snap
