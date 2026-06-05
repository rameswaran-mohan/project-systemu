"""v0.8.22.1 (R5): resume a chat task when its stuck-loop decision is resolved.

Two trigger paths feed the same dispatch:

  (1) EventBus subscriber (fast path) — registered in the daemon via
      :func:`register`. Fires synchronously on the in-process
      ``operator_decision_resolved`` event. Used when the resolution
      happens inside the daemon (dashboard click, etc.).

  (2) Daemon-side reconciler poll (cross-process safety net) — see
      :func:`systemu.scheduler.jobs.reconcile_resolved_stuck_decisions`.
      EventBus is process-local (``systemu/interface/event_bus.py``),
      so the CLI command ``sharing_on decisions resolve`` lives in a
      separate process and its publish never reaches the daemon
      subscriber. The reconciler walks the persisted decisions index
      and re-dispatches any resolved structured_question decision that
      hasn't been dispatched yet.

Both paths funnel into :func:`_dispatch_resume`, which:
  * stashes the operator's answer into the parked run's execution
    snapshot (``__STUCK_ANSWER__::obj_<id>::<choice>`` sticky note,
    consumed by ``shadow_runtime._apply_stuck_answer`` on resume),
  * re-submits the activity with ``resume_from_execution_id`` so the
    runtime applies the answer at resume-start,
  * and stamps ``decision.context["resume_dispatched"] = True`` on
    the persisted decision so we never double-dispatch (across
    restarts, across both paths).

The in-memory ``_handled`` set is the EventBus fast-path dedup; the
persisted flag is the cross-restart / cross-path source of truth.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Idempotency: decisions we've already re-dispatched in THIS process
# (EventBus may replay; in-memory only).  The persisted flag
# decision.context["resume_dispatched"] is the cross-process /
# cross-restart truth — checked in _dispatch_resume.
_handled: set = set()


def _dispatch_resume(decision, *, vault, supervisor,
                     data_dir: Optional[Path] = None) -> bool:
    """Given a resolved stuck `structured_question` decision, stash the
    operator's answer into the parked run's snapshot and re-submit the
    activity with `resume_from_execution_id`.

    Idempotent: skips if ``decision.context["resume_dispatched"]`` is
    already True, AND stamps it True on success so subsequent calls
    (from the poll, from an EventBus replay, from a daemon restart)
    are no-ops.

    Returns True if a dispatch was performed, False if skipped (already
    dispatched, missing coords, wrong kind, etc.).  Best-effort: any
    exception while stashing the snapshot is logged but does not block
    the re-submit.  Failures of the re-submit itself propagate to the
    caller (the EventBus adapter swallows them; the reconciler logs
    and continues).
    """
    dctx = decision.context or {}
    if dctx.get("kind") != "structured_question":
        return False
    if not dctx.get("chat_submission_id"):
        return False
    if dctx.get("resume_dispatched"):
        return False
    execution_id = dctx.get("execution_id")
    activity_id = dctx.get("activity_id")
    shadow_id = dctx.get("shadow_id")
    objective_id = dctx.get("objective_id")
    if not (execution_id and activity_id and shadow_id):
        logger.info(
            "[ResumeOnDecision] decision %s missing resume coords — skipping",
            decision.id,
        )
        return False

    # Stash the operator's answer into the snapshot so resume is deterministic.
    try:
        from systemu.runtime.execution_snapshot import read_snapshot, write_snapshot
        snap = read_snapshot(execution_id, data_dir=data_dir)
        if snap is not None:
            snap.sticky_notes.append(
                f"__STUCK_ANSWER__::obj_{objective_id}::{decision.choice}"
            )
            write_snapshot(snap, data_dir=data_dir)
    except Exception:
        logger.debug(
            "[ResumeOnDecision] could not stash answer in snapshot",
            exc_info=True,
        )

    # Stamp the persisted marker BEFORE submitting so a concurrent
    # poll/event can't race in and double-dispatch.  If the save fails
    # we still proceed (the in-memory _handled set covers same-process
    # replay) — better one extra dispatch than a lost resume.
    try:
        decision.context["resume_dispatched"] = True
        vault.save_decision(decision)
    except Exception:
        logger.debug(
            "[ResumeOnDecision] could not stamp resume_dispatched on %s",
            decision.id, exc_info=True,
        )

    _handled.add(decision.id)
    supervisor.submit(
        activity_id, shadow_id,
        priority=1, reason="chat", origin="chat",
        resume_from_execution_id=execution_id,
        chat_submission_id=dctx.get("chat_submission_id"),
        consult_affinity_log=False,
    )
    logger.info(
        "[ResumeOnDecision] re-dispatched activity %s (resume %s) after decision %s",
        activity_id, execution_id, decision.id,
    )
    return True


def handle_decision_resolved(event: Dict[str, Any], *, vault, supervisor,
                             data_dir: Optional[Path] = None) -> None:
    """EventBus adapter: process one ``operator_decision_resolved`` event.

    Fetches the decision and delegates to :func:`_dispatch_resume`.
    Best-effort; never raises (EventBus subscribers must not crash the
    publisher).
    """
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
        _dispatch_resume(dec, vault=vault, supervisor=supervisor, data_dir=data_dir)
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
