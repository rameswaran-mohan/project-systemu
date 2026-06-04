"""v0.8.22.1 (R5): resume a chat task when its stuck-loop decision is resolved.

A daemon-registered EventBus subscriber. On `operator_decision_resolved` for a
stuck `structured_question` decision that carries a `chat_submission_id`, it
stashes the operator's answer into the parked run's resume snapshot and
re-submits the activity with `resume_from_execution_id` so the runtime applies
the answer at resume-start (shadow_runtime `_apply_stuck_answer`).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Idempotency: decisions we've already re-dispatched (EventBus may replay).
_handled: set = set()


def handle_decision_resolved(event: Dict[str, Any], *, vault, supervisor,
                             data_dir: Optional[Path] = None) -> None:
    """Process one operator_decision_resolved event. Best-effort; never raises."""
    try:
        if event.get("category") != "operator_decision_resolved":
            return
        ctx = event.get("context") or {}
        decision_id = ctx.get("decision_id")
        if not decision_id or decision_id in _handled:
            return
        try:
            dec = vault.get_decision(decision_id)
        except Exception:
            return
        dctx = dec.context or {}
        if dctx.get("kind") != "structured_question":
            return
        if not dctx.get("chat_submission_id"):
            return
        execution_id = dctx.get("execution_id")
        activity_id = dctx.get("activity_id")
        shadow_id = dctx.get("shadow_id")
        objective_id = dctx.get("objective_id")
        if not (execution_id and activity_id and shadow_id):
            logger.info("[ResumeOnDecision] decision %s missing resume coords — skipping", decision_id)
            return

        # Stash the operator's answer into the snapshot so resume is deterministic.
        try:
            from systemu.runtime.execution_snapshot import read_snapshot, write_snapshot
            snap = read_snapshot(execution_id, data_dir=data_dir)
            if snap is not None:
                snap.sticky_notes.append(
                    f"__STUCK_ANSWER__::obj_{objective_id}::{dec.choice}"
                )
                write_snapshot(snap, data_dir=data_dir)
        except Exception:
            logger.debug("[ResumeOnDecision] could not stash answer in snapshot", exc_info=True)

        _handled.add(decision_id)
        supervisor.submit(
            activity_id, shadow_id,
            priority=1, reason="chat", origin="chat",
            resume_from_execution_id=execution_id,
            chat_submission_id=dctx.get("chat_submission_id"),
            consult_affinity_log=False,
        )
        logger.info("[ResumeOnDecision] re-dispatched activity %s (resume %s) after decision %s",
                    activity_id, execution_id, decision_id)
    except Exception:
        logger.debug("[ResumeOnDecision] handler error", exc_info=True)


def register(vault, supervisor, data_dir: Optional[Path] = None):
    """Subscribe the handler to the EventBus. Returns the unsubscribe callable."""
    from systemu.interface.event_bus import EventBus

    def _cb(ev):
        handle_decision_resolved(ev, vault=vault, supervisor=supervisor, data_dir=data_dir)

    unsub = EventBus.get().subscribe(_cb, replay=False)
    logger.info("[ResumeOnDecision] registered EventBus subscriber for stuck-decision resume")
    return unsub
