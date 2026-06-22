"""Shadow-level success metrics keyed by (shadow_id, intent_hash).

v0.4.3-a — the data source for affinity-routing alternative selection.
``Supervisor._resolve_shadow_with_affinity`` (v0.4.2-a) picks alternatives
by skill overlap; this module adds a second-tier ranking: shadows with a
higher historical success rate on the *same kind of work* are preferred
over shadows that have historically struggled.

Key design choices:

* **Per-(shadow × intent_hash), not per-shadow alone.**  A shadow that
  excels at browser work but flounders on data-pipeline tasks shouldn't
  be uniformly upranked.  The intent_hash from
  ``affinity_log.compute_intent_hash`` is the right granularity — same
  hash means "the same kind of work" without coupling to specific
  scroll ids.
* **Persisted to ``data/shadow_metrics.json``** with atomic tmp-rename
  writes.  Same pattern as DepApprovalStore / AffinityLog / RejectionStore.
* **Read-on-every-check** when queried by the supervisor — small file,
  no cache to go stale across processes.
* **Neutral default** for shadows with no history on this intent_hash
  (success_rate = ``0.5``).  Prevents new shadows from being penalised
  as if they'd failed everything they hadn't been asked yet.
* **Never raises into the caller.**  Telemetry write failures are
  swallowed; query failures return neutral defaults.

Used by:

* ``shadow_runtime._record_terminal_telemetry`` — records each terminal
  state.
* ``Supervisor._resolve_shadow_with_affinity`` — consults during
  alternative-shadow selection.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DEFAULT_PATH = Path("data") / "shadow_metrics.json"


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class MetricEntry:
    """Per (shadow_id, intent_hash) counts."""
    executions: int = 0
    successes: int = 0
    partials:  int = 0
    failures:  int = 0
    last_seen: str = ""
    # Paper-readiness (Plan 0 / Build 1): a *harness* slice of the above.
    # ``harness_runs`` counts executions that went through the reverse
    # harness; ``harness_successes`` those that also succeeded.  These are
    # additive — a harness run is still counted in executions/successes too.
    harness_runs:      int = 0
    harness_successes: int = 0

    @property
    def success_rate(self) -> float:
        """Fraction of executions that returned success.

        Returns 0.5 (neutral) when no executions have been recorded —
        prevents new shadows from being unfairly penalised by the
        supervisor's alternative-selection scoring.
        """
        if self.executions <= 0:
            return 0.5
        return self.successes / self.executions

    @property
    def has_history(self) -> bool:
        return self.executions > 0

    @property
    def harness_success_rate(self) -> float:
        """Fraction of harness runs that succeeded.

        Returns 0.5 (neutral) when no harness runs have been recorded —
        same neutral-default convention as ``success_rate``.
        """
        if self.harness_runs <= 0:
            return 0.5
        return self.harness_successes / self.harness_runs

    @property
    def without_harness_success_rate(self) -> float:
        """Fraction of *non-harness* executions that succeeded.

        Derived from the additive harness slice: non-harness executions
        are ``executions - harness_runs`` and non-harness successes are
        ``successes - harness_successes``.  Returns 0.5 (neutral) when no
        non-harness executions have been recorded.
        """
        runs = self.executions - self.harness_runs
        if runs <= 0:
            return 0.5
        succ = self.successes - self.harness_successes
        if succ < 0:
            succ = 0
        return succ / runs


class ShadowMetrics:
    """JSON-file-backed per-(shadow_id, intent_hash) counters.

    Thread-safe via a single module lock + atomic file writes.  At
    operator scale the file is tiny (one row per shadow × intent_hash
    that's been observed), so we re-read on every query rather than
    maintaining an in-memory cache — keeps cross-process behaviour
    correct without a reload signal.
    """

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path or _DEFAULT_PATH)
        self._lock = threading.Lock()

    # ── Public API ────────────────────────────────────────────────────────

    def record(
        self,
        *,
        shadow_id: str,
        intent_hash: str,
        status: str,
    ) -> None:
        """Record a terminal-state outcome.

        ``status`` must be one of: ``success``, ``failure``, ``partial``,
        ``cancelled``.  ``cancelled`` updates ``executions`` + ``last_seen``
        but does NOT count toward successes/failures — cancellations
        usually mean the work was preempted (watchdog), not that the
        shadow failed it.
        """
        if not shadow_id or not intent_hash:
            return
        try:
            with self._lock:
                data = self._load()
                key = self._key(shadow_id, intent_hash)
                rows: Dict[str, Any] = data.setdefault("rows", {})
                row = rows.get(key) or self._blank_row(shadow_id, intent_hash)
                row["executions"] = int(row.get("executions", 0)) + 1
                if status == "success":
                    row["successes"] = int(row.get("successes", 0)) + 1
                elif status == "partial":
                    row["partials"] = int(row.get("partials", 0)) + 1
                elif status == "failure":
                    row["failures"] = int(row.get("failures", 0)) + 1
                # cancelled: executions only, no success/fail attribution
                row["last_seen"] = _now_iso()
                rows[key] = row
                self._save(data)
        except Exception:
            logger.debug("[ShadowMetrics] record skipped", exc_info=True)

    def record_harness_run(
        self,
        *,
        shadow_id: str,
        intent_hash: str,
        used_harness: bool,
        success: bool,
    ) -> None:
        """Record an execution, tracking whether it used the reverse harness.

        A harness run is a *real* execution, so it always updates the
        ordinary ``executions`` / ``successes`` / ``failures`` counters
        (just like :meth:`record`).  Additionally, when ``used_harness``
        is true, ``harness_runs`` is incremented, and ``harness_successes``
        is incremented when the run also succeeded.  When ``used_harness``
        is false the harness counters are left untouched, so the
        with-harness vs without-harness slices stay disjoint.

        Never raises — telemetry failures are swallowed, matching the rest
        of this module.
        """
        if not shadow_id or not intent_hash:
            return
        try:
            with self._lock:
                data = self._load()
                key = self._key(shadow_id, intent_hash)
                rows: Dict[str, Any] = data.setdefault("rows", {})
                row = rows.get(key) or self._blank_row(shadow_id, intent_hash)
                row["executions"] = int(row.get("executions", 0)) + 1
                if success:
                    row["successes"] = int(row.get("successes", 0)) + 1
                else:
                    row["failures"] = int(row.get("failures", 0)) + 1
                if used_harness:
                    row["harness_runs"] = int(row.get("harness_runs", 0)) + 1
                    if success:
                        row["harness_successes"] = int(row.get("harness_successes", 0)) + 1
                row["last_seen"] = _now_iso()
                rows[key] = row
                self._save(data)
        except Exception:
            logger.debug("[ShadowMetrics] record_harness_run skipped", exc_info=True)

    def note_harness_usage(
        self, *, shadow_id: str, intent_hash: str, used_harness: bool, success: bool,
    ) -> None:
        """Additively record the harness slice WITHOUT touching the base
        executions/successes/failures counters.

        Unlike :meth:`record_harness_run` (the *sole* per-run recorder, which bumps
        base + harness together), this is called at terminal *finalize* alongside the
        ordinary base recorder, so it must NOT double-count executions.  Only
        ``harness_runs`` / ``harness_successes`` move, and only when ``used_harness``
        is true.  ``without_harness_success_rate`` already returns the neutral default
        when the (now-decoupled) subtraction is non-positive, so the base slice stays
        safe.  Never raises.
        """
        if not shadow_id or not intent_hash or not used_harness:
            return
        try:
            with self._lock:
                data = self._load()
                key = self._key(shadow_id, intent_hash)
                rows: Dict[str, Any] = data.setdefault("rows", {})
                row = rows.get(key) or self._blank_row(shadow_id, intent_hash)
                row["harness_runs"] = int(row.get("harness_runs", 0)) + 1
                if success:
                    row["harness_successes"] = int(row.get("harness_successes", 0)) + 1
                row["last_seen"] = _now_iso()
                rows[key] = row
                self._save(data)
        except Exception:
            logger.debug("[ShadowMetrics] note_harness_usage skipped", exc_info=True)

    def get(self, *, shadow_id: str, intent_hash: str) -> MetricEntry:
        """Return the metric entry for (shadow_id, intent_hash).

        Missing entries return a neutral default (success_rate=0.5,
        executions=0).  Caller can check ``.has_history`` if it needs to
        distinguish "no data" from "perfect track record".
        """
        try:
            with self._lock:
                data = self._load()
                row = data.get("rows", {}).get(self._key(shadow_id, intent_hash))
            if not row:
                return MetricEntry()
            return self._row_to_entry(row)
        except Exception:
            logger.debug("[ShadowMetrics] get skipped", exc_info=True)
            return MetricEntry()

    def harness_success_rates(
        self, *, shadow_id: str, intent_hash: str,
    ) -> Dict[str, float]:
        """Return with-harness vs without-harness success rates for a pair.

        Keys: ``with_harness`` and ``without_harness``.  Each uses the
        neutral-default convention (0.5 when there's no data for that
        slice), so a brand-new pair reports ``{0.5, 0.5}`` rather than
        biasing toward either path.
        """
        entry = self.get(shadow_id=shadow_id, intent_hash=intent_hash)
        return {
            "with_harness":    entry.harness_success_rate,
            "without_harness": entry.without_harness_success_rate,
        }

    def list_for_intent(self, intent_hash: str) -> List[Dict[str, Any]]:
        """Return all metric rows for the given intent_hash, sorted by
        success_rate descending then by executions descending.  Used by
        operator dashboards + the affinity-router's debug surface.
        """
        try:
            with self._lock:
                data = self._load()
            out: List[Dict[str, Any]] = []
            for row in data.get("rows", {}).values():
                if row.get("intent_hash") != intent_hash:
                    continue
                entry = self._row_to_entry(row)
                out.append({
                    "shadow_id":    row.get("shadow_id"),
                    "intent_hash":  row.get("intent_hash"),
                    **asdict(entry),
                    "success_rate": entry.success_rate,
                    "harness_success_rate": entry.harness_success_rate,
                })
            out.sort(key=lambda r: (-r["success_rate"], -r["executions"]))
            return out
        except Exception:
            return []

    def clear(self) -> int:
        """Wipe the metrics file.  Returns the number of rows removed."""
        try:
            with self._lock:
                data = self._load()
                n = len(data.get("rows", {}))
                self._save({"rows": {}})
            return n
        except Exception:
            return 0

    # ── Internals ─────────────────────────────────────────────────────────

    @staticmethod
    def _key(shadow_id: str, intent_hash: str) -> str:
        return f"{shadow_id}|{intent_hash}"

    @staticmethod
    def _blank_row(shadow_id: str, intent_hash: str) -> Dict[str, Any]:
        return {
            "shadow_id":   shadow_id,
            "intent_hash": intent_hash,
            "executions":  0,
            "successes":   0,
            "partials":    0,
            "failures":    0,
            "last_seen":   "",
            "harness_runs":      0,
            "harness_successes": 0,
        }

    @staticmethod
    def _row_to_entry(row: Dict[str, Any]) -> MetricEntry:
        return MetricEntry(
            executions=int(row.get("executions", 0)),
            successes=int(row.get("successes", 0)),
            partials=int(row.get("partials", 0)),
            failures=int(row.get("failures", 0)),
            last_seen=row.get("last_seen", ""),
            harness_runs=int(row.get("harness_runs", 0)),
            harness_successes=int(row.get("harness_successes", 0)),
        )

    def _load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {"rows": {}}
        try:
            raw = self.path.read_text(encoding="utf-8")
            if not raw.strip():
                return {"rows": {}}
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("shadow_metrics is not a JSON object")
            data.setdefault("rows", {})
            return data
        except Exception:
            logger.exception(
                "[ShadowMetrics] could not parse %s — starting empty",
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
            logger.exception("[ShadowMetrics] could not persist %s", self.path)


# ─────────────────────────────────────────────────────────────────────────────
# Module-level singleton

_singleton: Optional[ShadowMetrics] = None
_singleton_lock = threading.Lock()


def get_shadow_metrics(force_path: Optional[Path] = None) -> ShadowMetrics:
    global _singleton
    with _singleton_lock:
        if force_path is not None:
            return ShadowMetrics(force_path)
        if _singleton is None:
            _singleton = ShadowMetrics()
        return _singleton


def reset_singleton_for_tests() -> None:
    global _singleton
    with _singleton_lock:
        _singleton = None
