"""Lightweight execution metrics tracker for the data flywheel.

Writes a per-shadow `metrics.json` file after each execution.
Tracks improvement over time: success rate, avg iterations, memory growth.
No database — pure JSON append-and-aggregate, safe for frequent writes.

Metrics tracked (what makes the flywheel *provably* spin):
  success_rate        — rising means shadow solves tasks more reliably
  avg_iterations      — falling means shadow needs fewer reasoning steps
  memory_entry_count  — rising means accumulated experience is growing
  high_confidence_entries — rising means lessons are being reinforced
  objectives_completed_rate — rising means shadow achieves more goals per run
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime

from systemu.core.utils import utcnow
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

METRICS_FILENAME   = "metrics.json"
MAX_EXEC_HISTORY   = 100   # keep last N execution records (space-bounded)


# ─────────────────────────────────────────────────────────────────────────────

def record_execution(
    shadow_id:          str,
    shadow_name:        str,
    shadow_dir:         Path,
    execution_id:       str,
    status:             str,        # "success" | "failure" | "partial"
    iteration_count:    int,
    tool_calls_made:    int,
    objectives_completed: int,
    objectives_total:   int,
    duration_seconds:   float,
    memory_md_path:     str = "",
) -> None:
    """Append a single execution record and recompute aggregate metrics.

    This is called after every execution (success or fail) from ShadowRuntime.
    Atomic write ensures no corruption on concurrent runs.
    """
    metrics_path = shadow_dir / METRICS_FILENAME

    # Load existing or bootstrap
    if metrics_path.exists():
        try:
            metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        except Exception:
            metrics = _bootstrap(shadow_id, shadow_name)
    else:
        metrics = _bootstrap(shadow_id, shadow_name)

    # Count memory entries for growth tracking
    mem_count, high_conf_count = _count_memory_entries(memory_md_path)

    # New execution record
    rec: Dict[str, Any] = {
        "execution_id":        execution_id,
        "timestamp":           utcnow().isoformat(),
        "status":              status,
        "iterations":          iteration_count,
        "tool_calls":          tool_calls_made,
        "objectives_completed": objectives_completed,
        "objectives_total":    objectives_total,
        "duration_seconds":    round(duration_seconds, 2),
        "memory_entry_count":  mem_count,
    }

    executions: list = metrics.get("executions", [])
    executions.append(rec)
    # Trim to max history
    executions = executions[-MAX_EXEC_HISTORY:]
    metrics["executions"] = executions

    # Recompute aggregates
    total   = len(executions)
    success = sum(1 for e in executions if e["status"] == "success")
    fail    = sum(1 for e in executions if e["status"] == "failure")
    iters   = [e["iterations"] for e in executions if e["iterations"] > 0]
    obj_rates = [
        e["objectives_completed"] / e["objectives_total"]
        for e in executions
        if e.get("objectives_total", 0) > 0
    ]

    metrics.update({
        "shadow_id":                 shadow_id,
        "shadow_name":               shadow_name,
        "total_executions":          total,
        "success_count":             success,
        "failure_count":             fail,
        "partial_count":             total - success - fail,
        "success_rate":              round(success / total * 100, 1) if total else 0.0,
        "avg_iterations":            round(sum(iters) / len(iters), 1) if iters else 0.0,
        "memory_entry_count":        mem_count,
        "high_confidence_entries":   high_conf_count,
        "objectives_completed_rate": round(sum(obj_rates) / len(obj_rates) * 100, 1) if obj_rates else 0.0,
        "last_execution_at":         rec["timestamp"],
    })

    _atomic_write(metrics_path, metrics)
    logger.debug(
        "[Metrics] Shadow '%s': exec=%d success_rate=%.1f%% avg_iter=%.1f mem=%d",
        shadow_name, total, metrics["success_rate"], metrics["avg_iterations"], mem_count,
    )


def load_metrics(shadow_dir: Path) -> Dict[str, Any]:
    """Load metrics for a shadow. Returns empty dict if none recorded yet."""
    path = shadow_dir / METRICS_FILENAME
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("[Metrics] Could not load %s: %s", path, exc)
        return {}


def load_all_metrics(vault_dir: str) -> list[Dict[str, Any]]:
    """Load metrics for all shadows in the vault. Used by the flywheel dashboard."""
    results = []
    shadow_army = Path(vault_dir) / "shadow_army"
    if not shadow_army.exists():
        return results
    for shadow_dir in shadow_army.iterdir():
        if shadow_dir.is_dir():
            m = load_metrics(shadow_dir)
            if m:
                results.append(m)
    return results


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _bootstrap(shadow_id: str, shadow_name: str) -> Dict[str, Any]:
    return {
        "shadow_id":                 shadow_id,
        "shadow_name":               shadow_name,
        "total_executions":          0,
        "success_count":             0,
        "failure_count":             0,
        "partial_count":             0,
        "success_rate":              0.0,
        "avg_iterations":            0.0,
        "memory_entry_count":        0,
        "high_confidence_entries":   0,
        "objectives_completed_rate": 0.0,
        "last_execution_at":         None,
        "executions":                [],
    }


def _count_memory_entries(memory_md_path: str) -> tuple[int, int]:
    """Parse SHADOW_MEMORY.md and count total entries + high-confidence (conf >= 3) entries."""
    if not memory_md_path or not Path(memory_md_path).exists():
        return 0, 0
    try:
        text = Path(memory_md_path).read_text(encoding="utf-8")
        import re
        entries     = len(re.findall(r"^\s*-\s+\*\*lesson\*\*", text, re.MULTILINE | re.IGNORECASE))
        if entries == 0:
            # Count bullet points as rough approximation
            entries = len(re.findall(r"^\s*-\s+\S", text, re.MULTILINE))
        high_conf   = len(re.findall(r"confidence[:\s]+([3-9]|\d{2,})", text, re.IGNORECASE))
        return entries, high_conf
    except Exception:
        return 0, 0


def _atomic_write(path: Path, data: Dict[str, Any]) -> None:
    """Write JSON atomically using a temp file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp_metrics_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
