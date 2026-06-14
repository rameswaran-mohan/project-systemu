"""Per-iteration decision audit (Plan 0 Build 1 Task 1.3).

Records ONE row per loop iteration of an execution to::

    {vault_root}/executions/{execution_id}/decision_audit.jsonl

Each row captures *why* the loop did what it did on a given iteration —
the chosen action, the model's reasoning, and the live loop-health
counters (consecutive THINKs, loop-guard state, stuck-round count,
research-read / tool-failure streaks).  When the iteration is a
``REQUEST_HARNESS`` (the agent pulling for a capability it lacks), the
row additionally carries the harness request id / kind / confidence /
attempts-before-request so the pull decision can be reconstructed
offline.

This is an **audit/ledger** sidecar — best-effort throughout.  A write
failure never propagates to the caller; it just leaves a gap in the
audit trail.  Reads of a missing file return ``[]``.

Style mirrors ``systemu/runtime/llm_transcript.py`` and
``systemu/runtime/execution_snapshot.py``: module-level functions, a
``_path`` helper, a dataclass row, append-JSONL writes.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def _path(vault_root, execution_id: str) -> Path:
    return Path(vault_root) / "executions" / execution_id / "decision_audit.jsonl"


# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class IterationDecision:
    """One audited loop iteration.

    Field order roughly follows the shadow_runtime loop's per-iteration
    decision flow: identity → action/reasoning → loop-health counters →
    REQUEST_HARNESS pull-decision instrumentation → timestamp.
    """
    execution_id:            str
    iteration:               int
    action:                  str
    reasoning:               str
    consecutive_thinks:      int
    loop_guard_active:       bool
    loop_guard_message:      Optional[str]
    stuck_round_count:       int
    consec_research_reads:   int
    consec_tool_failures:    int
    is_request_harness:      bool
    harness_request_id:      Optional[str] = None
    harness_kind:            Optional[str] = None
    harness_confidence:      Optional[float] = None
    harness_attempts_before: Optional[int] = None
    ts:                      str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Public API

def append_iteration_decision(
    vault_root,
    execution_id: str,
    row: IterationDecision,
) -> None:
    """Append ``row`` as one JSONL line.  Best-effort, never raises.

    Creates the ``executions/<id>/`` parent directory if needed.  Stamps
    ``ts`` with the current UTC time if the caller left it blank.
    """
    p = _path(vault_root, execution_id)
    try:
        if not row.ts:
            row.ts = _now_iso()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(row)) + "\n")
    except Exception:
        # Audit sidecar: a write failure must never break the loop.
        pass


def read_iteration_decisions(
    vault_root,
    execution_id: str,
) -> List[Dict[str, Any]]:
    """Return all recorded rows in append order; ``[]`` if missing/unreadable."""
    p = _path(vault_root, execution_id)
    out: List[Dict[str, Any]] = []
    try:
        if not p.exists():
            return []
        with p.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    # Skip a single malformed line rather than losing the rest.
                    continue
    except Exception:
        return []
    return out
