"""Execution snapshot persistence for resume-after-recalibration (v0.5.1-e).

When the supervisor's RECALIBRATE_TOOL fires, the in-flight execution
is snapshotted to disk so the resume pathway can rebuild context
instead of restarting from scratch.

Persists to ``data/audit/exec_<execution_id>/resume_snapshot.json``.

Snapshot fields:

* ``execution_id``           — original execution id
* ``shadow_id``              — owning shadow
* ``scroll_id``              — scroll under execution
* ``activity_id``            — activity id (if known)
* ``iteration``              — last completed iteration
* ``current_action_block``   — pointer for ActionBlock-mode scrolls
* ``completed_objective_ids``— intent-driven mode
* ``recent_history_slice``   — last N events (tool_call/observation/thought)
* ``sticky_notes``           — survives rollback (and now also resume)
* ``original_tool_id``       — what was being recalibrated
* ``recalibration_dedup_key``— links back to the operator approval card

Loaded by ``shadow_runtime.execute()`` when its activity payload carries
``resume_from_snapshot=True``.  The execution context's pre-loop state
is rebuilt from the snapshot, then the loop continues normally.

Best-effort throughout — snapshot failures don't break the recalibration
flow; they just downgrade resume to v0.5.0's fresh-restart-with-sticky.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def _snapshot_path(data_dir: Path, execution_id: str) -> Path:
    return data_dir / "audit" / f"exec_{execution_id}" / "resume_snapshot.json"


# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ExecutionSnapshot:
    """Restorable view of an in-flight execution.

    Field order roughly follows the shadow_runtime loop's state setup.
    """
    execution_id:             str
    shadow_id:                str
    scroll_id:                str
    activity_id:              Optional[str] = None
    iteration:                int = 0
    current_action_block:     int = 1
    completed_objective_ids:  List[Any] = field(default_factory=list)
    recent_history_slice:     List[Dict[str, Any]] = field(default_factory=list)
    sticky_notes:             List[str] = field(default_factory=list)
    original_tool_id:         Optional[str] = None
    recalibration_dedup_key:  Optional[str] = None
    snapshotted_at:           str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Public API

_lock = threading.Lock()


def write_snapshot(
    snapshot: ExecutionSnapshot,
    *,
    data_dir: Optional[Path] = None,
) -> Optional[Path]:
    """Persist ``snapshot`` to disk; returns the file path on success."""
    target = _snapshot_path(Path(data_dir or "data"), snapshot.execution_id)
    snapshot.snapshotted_at = snapshot.snapshotted_at or _now_iso()
    try:
        with _lock:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_text(
                json.dumps(_to_dict(snapshot), indent=2, sort_keys=True),
                encoding="utf-8",
            )
            os.replace(tmp, target)
        logger.info(
            "[ExecSnapshot] wrote %s (iter=%d completed_objs=%d sticky=%d)",
            target, snapshot.iteration,
            len(snapshot.completed_objective_ids), len(snapshot.sticky_notes),
        )
        return target
    except Exception:
        logger.exception("[ExecSnapshot] write failed for %s", snapshot.execution_id)
        return None


def read_snapshot(
    execution_id: str,
    *,
    data_dir: Optional[Path] = None,
) -> Optional[ExecutionSnapshot]:
    """Load a snapshot for ``execution_id``.  Returns None on missing /
    unparseable file."""
    target = _snapshot_path(Path(data_dir or "data"), execution_id)
    if not target.exists():
        return None
    try:
        with _lock:
            data = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        return ExecutionSnapshot(
            execution_id=data.get("execution_id", execution_id),
            shadow_id=data.get("shadow_id", ""),
            scroll_id=data.get("scroll_id", ""),
            activity_id=data.get("activity_id"),
            iteration=int(data.get("iteration", 0)),
            current_action_block=int(data.get("current_action_block", 1)),
            completed_objective_ids=list(data.get("completed_objective_ids", [])),
            recent_history_slice=list(data.get("recent_history_slice", [])),
            sticky_notes=list(data.get("sticky_notes", [])),
            original_tool_id=data.get("original_tool_id"),
            recalibration_dedup_key=data.get("recalibration_dedup_key"),
            snapshotted_at=data.get("snapshotted_at", ""),
        )
    except Exception:
        logger.exception("[ExecSnapshot] read failed for %s", execution_id)
        return None


def delete_snapshot(
    execution_id: str,
    *,
    data_dir: Optional[Path] = None,
) -> bool:
    """Best-effort delete after the resume has consumed it.  Returns True
    if a file was removed."""
    target = _snapshot_path(Path(data_dir or "data"), execution_id)
    if not target.exists():
        return False
    try:
        with _lock:
            target.unlink()
        logger.info("[ExecSnapshot] deleted %s after resume", target)
        return True
    except Exception:
        logger.debug("[ExecSnapshot] delete failed for %s", execution_id, exc_info=True)
        return False


def _to_dict(snapshot: ExecutionSnapshot) -> Dict[str, Any]:
    return {
        "execution_id":            snapshot.execution_id,
        "shadow_id":               snapshot.shadow_id,
        "scroll_id":               snapshot.scroll_id,
        "activity_id":             snapshot.activity_id,
        "iteration":               snapshot.iteration,
        "current_action_block":    snapshot.current_action_block,
        "completed_objective_ids": snapshot.completed_objective_ids,
        "recent_history_slice":    snapshot.recent_history_slice,
        "sticky_notes":            snapshot.sticky_notes,
        "original_tool_id":        snapshot.original_tool_id,
        "recalibration_dedup_key": snapshot.recalibration_dedup_key,
        "snapshotted_at":          snapshot.snapshotted_at,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Helpers used by shadow_runtime to capture / restore

def capture_from_context(
    *,
    execution_id: str,
    shadow_id: str,
    scroll_id: str,
    iteration: int,
    current_action_block: int,
    completed_objectives: Optional[Set[Any]],
    context,
    activity_id: Optional[str] = None,
    original_tool_id: Optional[str] = None,
    recalibration_dedup_key: Optional[str] = None,
    history_max_events: int = 12,
) -> ExecutionSnapshot:
    """Build an ExecutionSnapshot from a live ExecutionContext.

    The history slice is bounded — we don't dump the entire conversation;
    a recent slice is enough to give the resumed shadow continuity.
    """
    sticky = []
    try:
        sticky = context.get_sticky_notes() or []
    except Exception:
        pass

    history_slice: List[Dict[str, Any]] = []
    try:
        # Reuse the same compaction the LLM-prompt builder uses.
        from systemu.runtime.shadow_runtime import _build_history_slice
        history_slice = _build_history_slice(context, max_events=history_max_events) or []
    except Exception:
        pass

    return ExecutionSnapshot(
        execution_id=execution_id,
        shadow_id=shadow_id,
        scroll_id=scroll_id,
        activity_id=activity_id,
        iteration=iteration,
        current_action_block=current_action_block,
        completed_objective_ids=sorted(list(completed_objectives or [])),
        recent_history_slice=history_slice,
        sticky_notes=sticky,
        original_tool_id=original_tool_id,
        recalibration_dedup_key=recalibration_dedup_key,
    )


def apply_to_context(snapshot: ExecutionSnapshot, *, context) -> None:
    """Push the snapshot's recoverable state back into a fresh
    ExecutionContext: sticky notes + a one-shot reflection block that
    summarises the resumed state.

    Note: we do NOT directly reinject `recent_history_slice` into
    `_history` — that would conflict with the runtime's normal event-
    capture loop.  Instead we surface it via the reflection block as
    operator-readable context for the LLM.  The actual completed-
    objectives advancement is handled by the runtime's own bookkeeping
    when it sees the resume hint.
    """
    try:
        for note in snapshot.sticky_notes:
            context.add_sticky_note(note)
    except Exception:
        pass

    # Summarise the recent history into the one-shot reflection block.
    try:
        history_summary_parts = []
        for ev in snapshot.recent_history_slice[-6:]:
            role = ev.get("role", "?")
            if role == "tool_call":
                history_summary_parts.append(
                    f"  - tool_call: {ev.get('tool', '?')} "
                    f"(params keys: {list((ev.get('params') or {}).keys())})"
                )
            elif role == "tool_result":
                r = ev.get("result", {})
                if isinstance(r, dict):
                    summary = "success" if r.get("success") else f"failure ({str(r.get('error', ''))[:60]})"
                else:
                    summary = str(r)[:60]
                history_summary_parts.append(f"  - tool_result: {summary}")
            elif role == "thought":
                history_summary_parts.append(f"  - thought: {ev.get('thought', '')[:80]}")

        completed_count = len(snapshot.completed_objective_ids)
        block = (
            "## Resumed from recalibration snapshot\n\n"
            f"This execution previously failed at iteration {snapshot.iteration} "
            f"because a tool was structurally inadequate.  The supervisor "
            f"recalibrated it and the operator approved the new version.\n\n"
            f"**Already-completed objectives** (do NOT redo): "
            f"{snapshot.completed_objective_ids if completed_count else '(none)'}\n\n"
            f"**Recent execution context** (last 6 events from prior run):\n"
            + ("\n".join(history_summary_parts) if history_summary_parts else "  (none captured)")
            + "\n\nContinue from where the prior run left off using the "
              "newly-enabled tool.  Do not retry the failed tool calls verbatim — "
              "use the new version's interface."
        )
        context.queue_reflection_block(block)
    except Exception:
        logger.debug("[ExecSnapshot] apply reflection failed", exc_info=True)
