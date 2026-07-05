"""S1b — approval-fatigue metrics store (spec DEC-11 / plan PLAN-11).

An append-only, best-effort side-store for the live action-gate's approval
metrics. This module is the **store layer only** — a later task wires the
counters into the gate/decision chokepoints. No UI ships in this release.

Persistence mirrors ``runtime/table_store.py``: a single JSON file, **atomic
writes** (tempfile + os.replace) so an interrupted write can't corrupt the
store, and **defensive reads** (a missing/corrupt file yields empty state,
never an exception).

Distinct from ``metrics_tracker.py`` (shadow-execution metrics) and
``tool_metrics.py`` (per-tool-call metrics) — this store is scoped to the
gate-approval-fatigue counters only:

  gate_cards_created, gate_cards_resolved, resolution_latency_ms (histogram
  samples), always_allow_grants, denies, bulk_approve_events, asks_created,
  asks_resolved
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Union

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


def _default_state() -> Dict[str, Any]:
    state: Dict[str, Any] = {k: 0 for k in _COUNTER_KEYS}
    state["resolution_latency_ms"] = []
    state["_last_resolution_ts"] = None
    return state


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
            return state
        except Exception:
            return _default_state()

    # -- public API ----------------------------------------------------

    def incr(self, key: str) -> None:
        """Increment a simple integer counter by 1 (persisted)."""
        state = self._load()
        state[key] = int(state.get(key, 0) or 0) + 1
        self._write_atomic(state)

    def record_resolution(self, latency_ms: float, ts: float, choice: str = "") -> None:
        """Record a gate-card resolution: latency sample, resolved count,
        choice-specific counter, and bulk-approve detection against the
        persisted last-resolution timestamp."""
        state = self._load()

        samples: List[float] = list(state.get("resolution_latency_ms") or [])
        samples.append(latency_ms)
        state["resolution_latency_ms"] = samples

        state["gate_cards_resolved"] = int(state.get("gate_cards_resolved", 0) or 0) + 1

        if choice == "Always allow":
            state["always_allow_grants"] = int(state.get("always_allow_grants", 0) or 0) + 1
        elif choice == "Deny":
            state["denies"] = int(state.get("denies", 0) or 0) + 1

        last_ts = state.get("_last_resolution_ts")
        if last_ts is not None and (ts - last_ts) < _BULK_WINDOW_S:
            state["bulk_approve_events"] = int(state.get("bulk_approve_events", 0) or 0) + 1
        state["_last_resolution_ts"] = ts

        self._write_atomic(state)

    def snapshot(self) -> Dict[str, Any]:
        """Current counters, defensive: missing keys default to 0 / []."""
        state = self._load()
        out = {k: state.get(k, 0) for k in _COUNTER_KEYS}
        out["resolution_latency_ms"] = list(state.get("resolution_latency_ms") or [])
        return out
