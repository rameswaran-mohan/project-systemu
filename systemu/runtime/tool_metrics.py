"""Tool-level success-rate tracking (v0.4.4-a).

Tracks every tool invocation across the army.  Used for:

* **Operator visibility** — which tools are flaky over time, where to
  focus re-forging effort.  CLI: ``sharing_on debug tool-metrics``.
* **Evolution input** — tools with chronically low success rates are
  candidates for the v0.3+ Evolution pipeline to re-forge.  Read-only
  consumer; the auto-Evolution pathway itself lands in v0.4.5+.

**Explicitly NOT** used for SWAP_SHADOW decisions.  Tool reliability
is global; SWAP_SHADOW is about specialist fit (which shadow handles
this kind of work best).  ShadowMetrics (v0.4.3-a, keyed by shadow_id ×
intent_hash) is the right data for routing.  This module is a different
lever.

Storage: JSON file at ``data/tool_metrics.json``, atomic tmp-rename
writes — same pattern as DepApprovalStore / AffinityLog /
RejectionStore / ShadowMetrics.

Schema per (tool_id) entry:

* ``calls``               — total invocations
* ``successes``           — result.success == True
* ``failures``            — result.success == False AND not a dep block
* ``dependency_blocked``  — failures whose error_type was a
                            ``missing_dependency`` / ``dependency_install_*``;
                            don't count against the tool itself.
* ``timeouts``            — failures with timed_out=True
* ``last_failure_at``     — ISO ts of last failure (for staleness UI)
* ``last_seen``           — ISO ts of any invocation

``success_rate`` excludes ``dependency_blocked`` from the denominator
because those failures reflect the install environment, not the tool.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


_DEFAULT_PATH = Path("data") / "tool_metrics.json"


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ToolMetricEntry:
    calls:              int = 0
    successes:          int = 0
    failures:           int = 0
    dependency_blocked: int = 0
    timeouts:           int = 0
    last_failure_at:    str = ""
    last_seen:          str = ""

    @property
    def attributable_calls(self) -> int:
        """Calls that count toward the tool's own track record (excludes
        dependency-blocked failures, which are install-environment issues)."""
        return max(0, self.calls - self.dependency_blocked)

    @property
    def success_rate(self) -> float:
        """Successes / attributable_calls.  Returns ``0.5`` (neutral) when
        the tool has no attributable history — same convention as
        ShadowMetrics so the two scoring paths stay consistent.
        """
        attr = self.attributable_calls
        if attr <= 0:
            return 0.5
        return self.successes / attr

    @property
    def has_history(self) -> bool:
        return self.calls > 0


_DEP_ERROR_TYPES = frozenset({
    "missing_dependency",
    "dependency_install_blocked",
    "dependency_install_pending_approval",
    "dependency_install_failed",
})


class ToolMetrics:
    """Per-(tool_id) lifetime metrics.  Process-safe via tmp-rename writes."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path or _DEFAULT_PATH)
        self._lock = threading.Lock()

    # ── Public API ────────────────────────────────────────────────────────

    def record(
        self,
        *,
        tool_id:     str,
        success:     bool,
        error_type:  Optional[str] = None,
        timed_out:   bool = False,
    ) -> None:
        """Record one invocation outcome.

        ``error_type`` should be the ``parsed.error_type`` field from the
        ToolResult when set (classifier output or installer
        diagnostic).  When it's one of the dependency-failure values,
        we bump ``dependency_blocked`` instead of ``failures`` so the
        tool's success_rate isn't penalised for missing packages.
        """
        if not tool_id:
            return
        is_dep_block = bool(error_type and error_type in _DEP_ERROR_TYPES)
        try:
            with self._lock:
                data = self._load()
                rows: Dict[str, Any] = data.setdefault("rows", {})
                row = rows.get(tool_id) or {
                    "tool_id": tool_id, "calls": 0, "successes": 0,
                    "failures": 0, "dependency_blocked": 0, "timeouts": 0,
                    "last_failure_at": "", "last_seen": "",
                }
                row["calls"]    = int(row.get("calls", 0)) + 1
                row["last_seen"] = _now_iso()
                if success:
                    row["successes"] = int(row.get("successes", 0)) + 1
                else:
                    if is_dep_block:
                        row["dependency_blocked"] = int(row.get("dependency_blocked", 0)) + 1
                    else:
                        row["failures"] = int(row.get("failures", 0)) + 1
                    row["last_failure_at"] = _now_iso()
                    if timed_out:
                        row["timeouts"] = int(row.get("timeouts", 0)) + 1
                rows[tool_id] = row
                self._save(data)
        except Exception:
            logger.debug("[ToolMetrics] record skipped", exc_info=True)

    def get(self, tool_id: str) -> ToolMetricEntry:
        """Return the entry for ``tool_id``.  Missing → neutral default."""
        try:
            with self._lock:
                data = self._load()
                row = data.get("rows", {}).get(tool_id)
            if not row:
                return ToolMetricEntry()
            return ToolMetricEntry(
                calls=int(row.get("calls", 0)),
                successes=int(row.get("successes", 0)),
                failures=int(row.get("failures", 0)),
                dependency_blocked=int(row.get("dependency_blocked", 0)),
                timeouts=int(row.get("timeouts", 0)),
                last_failure_at=row.get("last_failure_at", ""),
                last_seen=row.get("last_seen", ""),
            )
        except Exception:
            logger.debug("[ToolMetrics] get skipped", exc_info=True)
            return ToolMetricEntry()

    def list_all(self) -> List[Dict[str, Any]]:
        """Return every tool's metrics, sorted by lowest success_rate then
        highest call volume (so operators see flakiest, most-used tools
        first).  Tools with no attributable history sort last."""
        try:
            with self._lock:
                data = self._load()
            out: List[Dict[str, Any]] = []
            for row in data.get("rows", {}).values():
                entry = ToolMetricEntry(
                    calls=int(row.get("calls", 0)),
                    successes=int(row.get("successes", 0)),
                    failures=int(row.get("failures", 0)),
                    dependency_blocked=int(row.get("dependency_blocked", 0)),
                    timeouts=int(row.get("timeouts", 0)),
                    last_failure_at=row.get("last_failure_at", ""),
                    last_seen=row.get("last_seen", ""),
                )
                out.append({
                    "tool_id":           row.get("tool_id"),
                    **asdict(entry),
                    "success_rate":      entry.success_rate,
                    "attributable_calls": entry.attributable_calls,
                    "has_history":       entry.has_history,
                })
            # Sort: tools with attributable history first (lowest success rate
            # first → flagging flaky tools), then by calls desc.  Cold-start
            # tools (no history) go to the bottom.
            def _sort_key(r: Dict[str, Any]):
                if r["attributable_calls"] <= 0:
                    return (2, -r["calls"])           # cold-start bucket
                return (0, r["success_rate"], -r["calls"])
            out.sort(key=_sort_key)
            return out
        except Exception:
            return []

    def low_success_tools(
        self,
        *,
        threshold: float = 0.5,
        min_calls: int = 5,
    ) -> List[Dict[str, Any]]:
        """Return tools whose attributable success_rate is below threshold
        AND have at least ``min_calls`` attributable calls.  Candidates
        for re-forging via the Evolution pipeline.
        """
        return [
            r for r in self.list_all()
            if r["has_history"]
            and r["attributable_calls"] >= min_calls
            and r["success_rate"] < threshold
        ]

    def clear(self) -> int:
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
                raise ValueError("tool_metrics is not a JSON object")
            data.setdefault("rows", {})
            return data
        except Exception:
            logger.exception(
                "[ToolMetrics] could not parse %s — starting empty", self.path,
            )
            return {"rows": {}}

    def _save(self, data: Dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self.path)
        except Exception:
            logger.exception("[ToolMetrics] could not persist %s", self.path)


# ─────────────────────────────────────────────────────────────────────────────
# Singleton

_singleton: Optional[ToolMetrics] = None
_singleton_lock = threading.Lock()


def get_tool_metrics(force_path: Optional[Path] = None) -> ToolMetrics:
    global _singleton
    with _singleton_lock:
        if force_path is not None:
            return ToolMetrics(force_path)
        if _singleton is None:
            _singleton = ToolMetrics()
        return _singleton


def reset_singleton_for_tests() -> None:
    global _singleton
    with _singleton_lock:
        _singleton = None
