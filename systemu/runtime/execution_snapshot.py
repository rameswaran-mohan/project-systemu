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

from systemu.core.models import Objective
from systemu.runtime.snapshot_migrations import (
    CURRENT_SCHEMA_VERSION, SnapshotRefused, migrate_snapshot_dict,
)

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
    # v0.9.33 Bug 2/3: harness-request bookkeeping survives suspend/resume so a
    # parked-then-resumed run does not silently reset the per-run cap or its
    # nesting depth.
    requests_this_run:        int = 0
    subagent_depth:           int = 0
    # v0.9.39 Bug 15: the run-tree id shared across the suspend→resume chain (and
    # sub-agent children). A resume inherits it so the per-run request cap +
    # outcome reconciliation stay scoped to the whole tree, not one execution.
    root_execution_id:        Optional[str] = None
    snapshotted_at:           str = ""
    # G1 (R-A2): the mutable+durable objective graph + its id allocator floor,
    # plus a schema version for the SnapshotMigrator (DEC-9, later task). Defaults
    # reproduce a legacy (pre-G1) snapshot: empty graph, floor allocator, unversioned=1.
    objective_graph:          List["Objective"] = field(default_factory=list)
    next_objective_id:        int = 1
    schema_version:           int = 1
    # R-A9 (Situational Inventory §5.1): survey_situation caches its SituationReport
    # here as a plain dict (SituationReport.model_dump()) so the snapshot stays
    # store-agnostic + cycle-free (we deliberately do NOT import SituationReport),
    # plus the freshness stamps used for invalidation-aware re-survey.
    situation_report:         Optional[dict] = None
    situation_stamps:         dict = field(default_factory=dict)
    # R-A10 (step B6): the binder's RequirementReport cached here as a plain dict
    # (RequirementReport.model_dump()) so a resumed run does not re-ask the
    # operator. Store-agnostic + cycle-free — we deliberately do NOT import
    # RequirementReport (mirrors situation_report).
    requirement_report:       Optional[dict] = None
    # S4 (fail-closed external-effect credit): the external-evidence store, a plain
    # dict {str(objective_id): ExternalEvidence.model_dump()} so a resumed run keeps
    # its fail-closed evidence and does NOT silently re-credit an unverified external
    # effect. Store-agnostic + cycle-free — we deliberately do NOT import
    # ExternalEvidence (mirrors requirement_report). Default {} = no evidence = no
    # external credit (fail-closed).
    external_evidence:        dict = field(default_factory=dict)
    # R-A12a (durable external-event retry timers): the pending-waits list, a plain
    # list of dicts so a resumed run keeps its durable retry timers and re-arms them
    # instead of dropping (or double-firing) an in-flight wait. Store-agnostic +
    # cycle-free (mirrors external_evidence). Default [] = no pending waits.
    pending_waits:            List[dict] = field(default_factory=list)
    # R-P3a (per-run cost visibility): the run's LLM usage rows, a plain list of
    # {model, tokens_in, tokens_out} dicts drained from costing._LEDGER at capture
    # so the run's cost survives a suspend→resume and can be priced off the record
    # itself (costing.cost_of(record)). Store-agnostic + cycle-free (mirrors
    # pending_waits). Default [] = no recorded usage. NOT a caps mechanism.
    cost:                     List[dict] = field(default_factory=list)


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
    snapshot.schema_version = CURRENT_SCHEMA_VERSION
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
        data = migrate_snapshot_dict(data, path=target)   # DEC-9: may raise SnapshotRefused
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
            requests_this_run=int(data.get("requests_this_run", 0)),
            subagent_depth=int(data.get("subagent_depth", 0)),
            root_execution_id=data.get("root_execution_id"),
            snapshotted_at=data.get("snapshotted_at", ""),
            schema_version=int(data.get("schema_version", 1)),
            next_objective_id=int(data.get("next_objective_id", 1) or 1),  # 1-based allocator floor; 0/absent -> 1 (matches capture)
            # Fix 1 (read-path poison guard): reuse _coerce_objectives so read and
            # capture handle a corrupt/hand-edited entry the SAME way (drop-with-warning),
            # not oppositely. A bare `[Objective(**o) for ...]` comp raised on ONE bad
            # entry → the broad except below returned None → the resume caller read that
            # as "no snapshot, start fresh" (DEC-9 re-execute-effects hazard). Now a
            # single malformed entry degrades to "resume with the good objectives". The
            # top-level isinstance(list) guard mirrors the situation/requirement_report
            # guards on the lines below.
            objective_graph=_coerce_objectives(
                data.get("objective_graph", []) if isinstance(data.get("objective_graph"), list) else []
            ),
            # R-A9: guard a poisoned/garbage cache — a non-dict report degrades to
            # None (=> a re-survey, never a crash); a non-dict stamps degrades to {}.
            situation_report=(lambda _v: _v if isinstance(_v, dict) else None)(data.get("situation_report")),
            situation_stamps=dict(data.get("situation_stamps") or {}) if isinstance(data.get("situation_stamps"), dict) else {},
            # R-A10: guard a poisoned/garbage cache — a non-dict report degrades to
            # None (=> re-ask the operator, never a crash).
            requirement_report=(lambda _v: _v if isinstance(_v, dict) else None)(data.get("requirement_report")),
            # S4: guard a poisoned/garbage store — a non-dict external_evidence
            # degrades to {} (=> no external credit, fail-closed; never a crash).
            external_evidence=dict(data.get("external_evidence") or {}) if isinstance(data.get("external_evidence"), dict) else {},
            # R-A12a: guard a poisoned/garbage store — a non-list pending_waits
            # degrades to [] (=> no phantom timers; never a crash).
            pending_waits=list(data.get("pending_waits") or []) if isinstance(data.get("pending_waits"), list) else [],
            # R-P3a: guard a poisoned/garbage store — a non-list cost degrades to
            # [] (=> the run shows zero usage; never a crash).
            cost=list(data.get("cost") or []) if isinstance(data.get("cost"), list) else [],
        )
    except SnapshotRefused:
        # DEC-9: a newer-than-supported snapshot must refuse LOUDLY — never
        # degrade to None (which the resume caller reads as "start fresh",
        # potentially re-executing effectful actions).
        raise
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
        "requests_this_run":       snapshot.requests_this_run,
        "subagent_depth":          snapshot.subagent_depth,
        "root_execution_id":       snapshot.root_execution_id,
        "snapshotted_at":          snapshot.snapshotted_at,
        "schema_version":          snapshot.schema_version,
        "next_objective_id":       snapshot.next_objective_id,
        "objective_graph":         [o.model_dump(mode="json") for o in snapshot.objective_graph],
        "situation_report":        snapshot.situation_report,
        "situation_stamps":        snapshot.situation_stamps,
        "requirement_report":      snapshot.requirement_report,
        "external_evidence":       snapshot.external_evidence,
        "pending_waits":           snapshot.pending_waits,
        "cost":                    snapshot.cost,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Helpers used by shadow_runtime to capture / restore

def _coerce_objectives(graph) -> List["Objective"]:
    """Normalise a context._objective_graph into a list of Objective instances.

    R-A10 (RISK-2): the runtime may stash the objective graph on the context as
    either Objective instances OR plain JSON dicts (model_dump). ExecutionSnapshot._to_dict
    requires Objective instances (it calls o.model_dump). Coerce here so both
    representations round-trip and a malformed entry is skipped (never crashes the
    snapshot). Objective already imported at module top.
    """
    out: List["Objective"] = []
    for o in graph or []:
        try:
            out.append(o if isinstance(o, Objective) else Objective(**o))
        except Exception:
            logger.debug("[ExecSnapshot] dropped malformed objective_graph entry", exc_info=True)
    return out


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
    requests_this_run: int = 0,
    subagent_depth: int = 0,
    root_execution_id: Optional[str] = None,
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
        requests_this_run=int(requests_this_run),
        subagent_depth=int(subagent_depth),
        root_execution_id=root_execution_id,
        objective_graph=_coerce_objectives(getattr(context, "_objective_graph", []) or []),
        next_objective_id=int(getattr(context, "_next_objective_id", 1) or 1),
        situation_report=getattr(context, "_situation_report", None),
        situation_stamps=dict(getattr(context, "_situation_stamps", {}) or {}),
        requirement_report=getattr(context, "_requirement_report", None),
        external_evidence=dict(getattr(context, "_external_evidence", {}) or {}),
        pending_waits=list(getattr(context, "_pending_waits", []) or []),
        cost=_usage_rows_for(execution_id, root_execution_id),
    )


def _usage_rows_for(execution_id: str, root_execution_id: Optional[str]) -> List[dict]:
    """R-P3a: the run's OWN LLM usage rows, drained from the live cost ledger, so
    the snapshot carries this run's cost across a suspend→resume. Best-effort — a
    costing import failure never breaks the snapshot.

    Deliberately does NOT union the root-tree id's rows: a sub-agent snapshot must
    carry ONLY the child's own cost. Copying the root run's rows into the child's
    durable field would DOUBLE-COUNT the root at query time (its rows already live
    in the root's own record; a tree total sums each record's own rows). Roll-up is
    a query-time concern over ``root_execution_id``, never a duplication into the
    durable field."""
    try:
        from systemu.runtime import costing
        return list(costing.usage_rows(execution_id))
    except Exception:
        return []


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

    # R-P3a: re-seed the cost ledger so a resumed run's post-resume LLM calls
    # ACCUMULATE on top of its pre-suspend cost. execute() mints a FRESH
    # execution_id on resume, so the fresh-eid ledger is empty; without this, the
    # next capture overwrites snapshot.cost with post-resume-only rows and the
    # pre-suspend cost is LOST (and cost_of/daily_total undercount). Seed the fresh
    # eid from the durable snapshot.cost, then DROP the stale original eid's ledger
    # (snapshot.execution_id) so the daily-total ledger-scan holds exactly ONE
    # entry per logical run — no double-count across the resume. Best-effort +
    # idempotent; a costing hiccup never breaks the resume.
    try:
        from systemu.runtime import costing
        from systemu.runtime.chat_submission_ctx import current_execution_id
        cur_eid = current_execution_id()
        old_eid = getattr(snapshot, "execution_id", None)
        costing.seed_usage(cur_eid, getattr(snapshot, "cost", None) or [])
        if old_eid and old_eid != cur_eid:
            costing.drop_usage(old_eid)
    except Exception:
        pass

    # R-A10 (RISK-2): re-seed the durable objective graph + the B6 requirement
    # report onto the context so a resumed MUTATED run keeps re-persisting them on
    # its NEXT capture (capture_from_context reads context._objective_graph /
    # ._requirement_report). CONDITIONAL on a non-empty graph — an empty graph
    # leaves _objective_graph UNSET so a never-mutated resume still captures []
    # (AC6 byte-identical, snapshot bytes unchanged). requirement_report re-seeds
    # verbatim (None → None is harmless). The shadow_runtime resume block also
    # re-seeds these before delete_snapshot; this makes the helper self-contained
    # for any future caller (idempotent).
    try:
        if snapshot.objective_graph:
            context._objective_graph = [
                o.model_dump(mode="json") if hasattr(o, "model_dump") else dict(o)
                for o in snapshot.objective_graph
            ]
        # Fix 3: also re-seed the id-allocator floor so the helper honors its
        # "self-contained for any future caller (idempotent)" contract. Without this
        # an apply→capture cycle collapsed next_objective_id from N to 1 for a caller
        # relying SOLELY on the helper. SAFE for the live shadow_runtime path: it
        # peels _resume_next_objective_id separately and OVERWRITES
        # context._next_objective_id with max(_resume_next_objective_id, _floor)
        # AFTER apply runs (:3105 « :3737), so AC6 snapshot bytes are unchanged.
        _nid = getattr(snapshot, "next_objective_id", None)
        if _nid is not None:
            context._next_objective_id = int(_nid)
        if getattr(snapshot, "requirement_report", None) is not None:
            context._requirement_report = snapshot.requirement_report
        # S4: re-seed the fail-closed external-evidence store so a resumed run keeps
        # its persisted evidence (a confirmed external effect survives resume; an
        # unconfirmed one stays unconfirmed → no silent re-credit). CONDITIONAL on a
        # non-empty store so a never-touched resume leaves _external_evidence UNSET
        # (byte-identical snapshot on the next capture).
        _ee = getattr(snapshot, "external_evidence", None)
        if _ee:
            context._external_evidence = dict(_ee)
        # R-A12a: re-seed the durable pending-waits so a resumed run re-arms its
        # in-flight external-event retry timers instead of dropping them. CONDITIONAL
        # on a non-empty list so a never-armed resume leaves _pending_waits UNSET
        # (byte-identical snapshot on the next capture; mirrors external_evidence).
        _pw = getattr(snapshot, "pending_waits", None)
        if _pw:
            context._pending_waits = list(_pw)
    except Exception:
        logger.debug("[ExecSnapshot] apply graph/req-report/evidence re-seed failed", exc_info=True)

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
