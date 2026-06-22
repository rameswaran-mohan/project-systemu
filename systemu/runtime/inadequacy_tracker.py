"""Cross-shadow inadequacy tracker (v0.5.1-d).

When ≥N distinct shadows independently flag the same tool as
structurally inadequate within a rolling window, that's a cluster
signal — the flaw is universal, not shadow-specific.  The diagnosis
LLM should bias toward `bump_version` for cluster signals, since the
problem affects everyone using the tool.

Storage: JSON file at ``data/inadequacy_tracker.json`` with atomic
tmp-rename writes — same pattern as the other v0.4/v0.5 stores.

Schema per ``tool_id`` entry:

    {
      "flags": [
        {"shadow_id": "sh-A", "execution_id": "exec-1",
         "ts": "2026-05-15T...", "rationale": "..."},
        ...
      ]
    }

Old flags are auto-pruned by ``recent_flags(window_hours=...)`` queries;
we don't compact the file proactively (operator-scale data).
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


_DEFAULT_PATH = Path("data") / "inadequacy_tracker.json"
_DEFAULT_WINDOW_HOURS = 24
# Distinct-shadow threshold for promoting a single-shadow signal into a
# "cluster" signal that biases diagnosis toward bump_version.
_CLUSTER_THRESHOLD = 3


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _now_iso() -> str:
    return _now().isoformat(timespec="seconds")


# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class InadequacyFlag:
    shadow_id:    str
    execution_id: str
    ts_iso:       str
    rationale:    Optional[str] = None


@dataclass(frozen=True)
class ClusterSignal:
    """Summary of cross-shadow inadequacy data for a tool."""
    tool_id:           str
    distinct_shadows:  int
    total_flags:       int
    is_cluster:        bool       # True when distinct_shadows ≥ _CLUSTER_THRESHOLD
    shadows:           List[str] = field(default_factory=list)
    sample_rationales: List[str] = field(default_factory=list)


class InadequacyTracker:
    """Process-safe tracker keyed by tool_id."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path or _DEFAULT_PATH)
        self._lock = threading.Lock()

    # ── Public API ────────────────────────────────────────────────────────

    def flag(
        self,
        *,
        tool_id: str,
        shadow_id: str,
        execution_id: str,
        rationale: Optional[str] = None,
    ) -> None:
        """Record one inadequacy observation.  Idempotent per
        (tool_id × shadow_id × execution_id) — duplicate observations from
        the same execution are dropped."""
        if not tool_id or not shadow_id:
            return
        try:
            with self._lock:
                data = self._load()
                rows: Dict[str, Any] = data.setdefault("rows", {})
                entry = rows.setdefault(tool_id, {"flags": []})
                # Dedup
                for existing in entry["flags"]:
                    if (existing.get("shadow_id") == shadow_id
                        and existing.get("execution_id") == execution_id):
                        return
                entry["flags"].append({
                    "shadow_id":    shadow_id,
                    "execution_id": execution_id,
                    "ts":           _now_iso(),
                    "rationale":    (rationale or "")[:300],
                })
                self._save(data)
        except Exception:
            logger.debug("[InadequacyTracker] flag skipped", exc_info=True)

    def cluster_signal_for(
        self,
        tool_id: str,
        *,
        window_hours: int = _DEFAULT_WINDOW_HOURS,
        threshold: int = _CLUSTER_THRESHOLD,
    ) -> ClusterSignal:
        """Compute the cluster signal for ``tool_id`` over the recent window."""
        cutoff = (_now() - timedelta(hours=window_hours)).isoformat(timespec="seconds")
        try:
            with self._lock:
                data = self._load()
                entry = data.get("rows", {}).get(tool_id) or {"flags": []}
        except Exception:
            return ClusterSignal(tool_id=tool_id, distinct_shadows=0,
                                  total_flags=0, is_cluster=False)
        recent = [f for f in entry.get("flags", []) if (f.get("ts") or "") >= cutoff]
        shadows_seen = sorted({f.get("shadow_id") for f in recent if f.get("shadow_id")})
        rationales = [f.get("rationale") for f in recent if f.get("rationale")][:3]
        return ClusterSignal(
            tool_id=tool_id,
            distinct_shadows=len(shadows_seen),
            total_flags=len(recent),
            is_cluster=len(shadows_seen) >= threshold,
            shadows=shadows_seen,
            sample_rationales=rationales,
        )

    def clear(self) -> int:
        """Wipe the tracker.  Returns the number of tools that had flags."""
        with self._lock:
            data = self._load()
            n = len(data.get("rows", {}))
            self._save({"rows": {}})
        return n

    # ── Internals ─────────────────────────────────────────────────────────

    def _load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {"rows": {}}
        try:
            raw = self.path.read_text(encoding="utf-8")
            if not raw.strip():
                return {"rows": {}}
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("inadequacy_tracker is not a JSON object")
            data.setdefault("rows", {})
            return data
        except Exception:
            logger.exception(
                "[InadequacyTracker] could not parse %s — starting empty",
                self.path,
            )
            return {"rows": {}}

    def _save(self, data: Dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self.path)
        except Exception:
            logger.exception("[InadequacyTracker] could not persist %s", self.path)


# Module-level singleton
_singleton: Optional[InadequacyTracker] = None
_singleton_lock = threading.Lock()


def get_inadequacy_tracker(force_path: Optional[Path] = None) -> InadequacyTracker:
    global _singleton
    with _singleton_lock:
        if force_path is not None:
            return InadequacyTracker(force_path)
        if _singleton is None:
            _singleton = InadequacyTracker()
        return _singleton


def reset_singleton_for_tests() -> None:
    global _singleton
    with _singleton_lock:
        _singleton = None
