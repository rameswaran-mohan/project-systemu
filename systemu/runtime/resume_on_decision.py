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
    kind = dctx.get("kind")
    # v0.9.52: a parked COMMAND gate (kind="gate", gate_type="command") is now
    # resumable too — previously only structured_question questions resumed, so a
    # chat task that parked on a run_command approval hung forever on resolution.
    # S1b (Task 4): a gate_type="tool" action gate resumes the SAME way (its
    # activity/shadow also derive from the snapshot; its Deny/Approve branches are
    # gate_type-discriminated below so the command path stays byte-for-byte).
    gate_type = dctx.get("gate_type")
    is_cmd_gate = (kind == "gate" and gate_type == "command")
    is_tool_gate = (kind == "gate" and gate_type == "tool")
    is_gate = is_cmd_gate or is_tool_gate
    # R-A13 Stage-3a: an operator-attest card (gate_type="operator") resumes too. Its
    # `kind` is overwritten to "gate" by gate.to_decision_context, so key off the
    # sibling kind_marker (NOT `kind`). It stashes an __OPERATOR_ATTEST__ sticky the
    # resume-start applier peels; activity/shadow derive from the snapshot like a gate.
    is_attest = (dctx.get("kind_marker") == "operator_attest")
    if kind != "structured_question" and not is_gate and not is_attest:
        return False
    if not dctx.get("chat_submission_id"):
        return False
    if dctx.get("resume_dispatched"):
        return False
    execution_id = dctx.get("execution_id")
    if not execution_id:
        logger.info("[ResumeOnDecision] decision %s has no execution_id — skipping", decision.id)
        return False
    choice = (decision.choice or "").strip().lower()

    # v0.10.21: a tool/command gate that parked on the run's FIRST tool call now
    # STAMPS activity_id/shadow_id into the context (chat_submission_ctx carriers), so
    # prefer those. A structured_question carries them directly. Only fall back to the
    # snapshot for an OLDER gate decision (or a mid-run park) that lacks context coords.
    activity_id = dctx.get("activity_id")
    shadow_id = dctx.get("shadow_id")
    snap = None
    snapshot_refused = False
    try:
        from systemu.runtime.execution_snapshot import read_snapshot, write_snapshot
        from systemu.runtime.snapshot_migrations import SnapshotRefused
        try:
            snap = read_snapshot(execution_id, data_dir=data_dir)
        except SnapshotRefused as _refused:
            # DEC-9: schema newer than this build supports. Don't proceed as if
            # there were simply no snapshot — log loudly. Any resume this path
            # dispatches re-reads the snapshot in shadow_runtime.execute (the single
            # fresh-vs-resume chokepoint), which refuses it there, so the run fails
            # honestly rather than starting fresh.
            logger.error("[ResumeOnDecision] snapshot refused for %s: %s",
                         execution_id, _refused)
            snap = None
            snapshot_refused = True
    except Exception:
        snap = None
    if snap is not None:
        activity_id = activity_id or getattr(snap, "activity_id", None)
        shadow_id = shadow_id or getattr(snap, "shadow_id", None)
    if not (activity_id and shadow_id):
        # v0.10.21: a GATE with no resume coords (an iteration-1 park whose decision
        # predates the context-coord stamp, or any coords-less gate) can't be RESUMED,
        # but we can still record the operator's approval so a manual re-run succeeds —
        # and stamp resume_dispatched so the reconciler stops re-logging this every
        # poll. A structured_question / attest genuinely needs the snapshot coords, so
        # those keep skipping.
        #
        # DEC-9 GUARD: a REFUSED snapshot (newer schema — not merely absent) means the
        # parked run may have done effectful work this build can't read. Do NOT record
        # the approval or stamp dispatched in that case — fall through to the honest
        # skip so nothing masks the refusal (mirrors the pre-v0.10.21 behaviour).
        if is_gate and not snapshot_refused:
            # Record ONLY a STANDING allow here ("Always allow" on a tool gate) — the
            # choice meant to carry forward, idempotent and keyed to persist across runs.
            # Deliberately DO NOT persist a SINGLE-USE bridge ("Approve once", or any
            # command gate) in the coords-less path: with no run to resume, the one-shot
            # would sit unconsumed and could later be spent by an UNRELATED call to the
            # same (params-independent) tool signature. A manual re-run simply re-asks —
            # the correct "approve once" semantics for a parked instance that no longer
            # exists. (The normal coords-present path re-submits immediately, consuming
            # the bridge within seconds, so it has no such window.)
            # A DENY-band gate records NOTHING here. Routing it into the recorder would
            # fall through to the single-use bridge — creating exactly the dangling,
            # params-independent one-shot this branch refuses to create, and creating it
            # only for the most dangerous band. A DENY simply re-asks on re-run.
            _v = str(dctx.get("verdict") or "").strip().lower()
            if is_tool_gate and choice == "always allow" and _v != "deny":
                _record_gate_approval(dctx, is_tool_gate=True, choice=choice)
            _stamp_dispatched(decision, vault)
            _handled.add(decision.id)
            logger.info(
                "[ResumeOnDecision] %s gate resolved (%s) for %s but no resume coords "
                "— %s; task must be re-run (not auto-resumed)",
                gate_type, choice or "deny", decision.id,
                ("recorded standing allow" if (is_tool_gate and choice == "always allow")
                 else "no standing approval recorded"),
            )
            return True
        logger.info(
            "[ResumeOnDecision] decision %s missing resume coords — skipping",
            decision.id,
        )
        return False

    if is_gate:
        if choice in ("deny", ""):
            # Operator denied a REQUIRED command/tool → the task can't proceed; mark
            # it FAILED rather than re-submitting (which would re-ask the same action
            # → loop). Idempotent: only flips a non-terminal activity. gate_type-
            # agnostic — a tool gate finalizes identically to a command gate.
            try:
                from systemu.core.models import ActivityStatus
                act = vault.get_activity(activity_id)
                if getattr(act, "status", None) not in (
                        ActivityStatus.COMPLETED, ActivityStatus.FAILED):
                    act.status = ActivityStatus.FAILED
                    vault.save_activity(act)
            except Exception:
                logger.debug("[ResumeOnDecision] gate-deny finalize failed", exc_info=True)
            _stamp_dispatched(decision, vault)
            logger.info("[ResumeOnDecision] %s gate DENIED for activity %s — finalized",
                        gate_type, activity_id)
            return True
        # S1b (Task 4) / v0.9.52: record the operator's approval to the
        # CommandApprovalStore. Tool gate: "Always allow" → STANDING allow-list,
        # anything else non-deny → SINGLE-USE resume bridge. Command gate: always a
        # SINGLE-USE bridge (byte-for-byte the v0.9.52 behaviour). Extracted into
        # _record_gate_approval so the missing-coords rescue above records the SAME
        # approval — the ONLY place a tool-gate "Always allow" becomes a standing entry.
        _record_gate_approval(dctx, is_tool_gate=is_tool_gate, choice=choice)
    elif is_attest:
        # R-A13 Stage-3a: stash the operator's attest CHOICE + the enqueue-time effect
        # classification (a JSON payload the resume-start applier peels) so the applier
        # can run verify(operator_attest) with the KNOWN non-money effect_class — a
        # requires_external objective with no known effect tag is money-move via the
        # fail-closed fallback and could otherwise never credit. Never a money-move at
        # enqueue (the enqueue gate excludes it); the applier + verify re-gate anyway.
        import json as _json
        objective_id = dctx.get("objective_id")
        try:
            if snap is not None:
                _payload = _json.dumps({
                    "choice": decision.choice,
                    "effect_class": dctx.get("effect_class"),
                    "is_money_move": bool(dctx.get("is_money_move")),
                })
                snap.sticky_notes.append(
                    f"__OPERATOR_ATTEST__::obj_{objective_id}::{_payload}"
                )
                write_snapshot(snap, data_dir=data_dir)
        except Exception:
            logger.debug("[ResumeOnDecision] could not stash attest answer in snapshot",
                         exc_info=True)
    else:
        # structured_question: stash the operator's answer into the snapshot so the
        # runtime applies it deterministically on resume.
        objective_id = dctx.get("objective_id")
        try:
            if snap is not None:
                snap.sticky_notes.append(
                    f"__STUCK_ANSWER__::obj_{objective_id}::{decision.choice}"
                )
                write_snapshot(snap, data_dir=data_dir)
        except Exception:
            logger.debug("[ResumeOnDecision] could not stash answer in snapshot", exc_info=True)

    _stamp_dispatched(decision, vault)
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


def _record_gate_approval(dctx, *, is_tool_gate: bool, choice: str) -> None:
    """Persist a resolved tool/command gate approval to the CommandApprovalStore.

    Tool gate: ``"always allow"`` → STANDING allow-list (``store.approve``); any other
    non-deny choice → SINGLE-USE resume bridge (``store.mark_resume_approved``). Command
    gate: always a SINGLE-USE bridge (byte-for-byte the v0.9.52 behaviour). The tool
    signature is READ BACK from the context (stamped by the gate at park time), never
    recomputed here (no tool object is in scope).

    Decoupled from the resume DISPATCH so the approval is recorded even when the parked
    run has no resume coords — without it a re-run would re-hit the same gate. The
    caller guarantees ``choice`` is non-deny. Best-effort; never raises."""
    try:
        from pathlib import Path as _P
        from systemu.runtime.command_approvals import init_default_store
        store = init_default_store(_P("data"))
        if store is None:
            return
        if is_tool_gate:
            sig = dctx.get("tool_signature")
            if not sig:
                return
            # IMPL-1: an "Always allow" can NEVER cover the DENY band. The gate stamps the
            # verdict at park time; without this check a standing approval for a
            # DENY-band tool would be consulted by ``tool_sandbox`` BEFORE any band check
            # on the next call and run it UNGATED — a persistent bypass of the
            # unknown-∩-high-severity floor. A DENY is downgraded to single-use, so the
            # gate re-fires every time and the floor keeps being enforced.
            # Normalise via ``.value`` first: ``Verdict`` is a str-Enum, and ``str()`` on
            # the member yields "Verdict.DENY", which would NOT match and would silently
            # re-open the hole for any caller that stamps the enum rather than its value.
            raw = dctx.get("verdict")
            verdict = str(getattr(raw, "value", raw) or "").strip().lower()
            # ABSENCE of the verdict fails CLOSED (single-use), matching
            # ``decision_bridge.classify_resolution``, which floors on a missing verdict
            # for the same key. A gate parked before the verdict was carried is rare; the
            # cost of closing it is one extra ask on a legacy card, and the cost of
            # leaving it open is a standing allow on an unknown band.
            if choice == "always allow" and verdict and verdict != "deny":
                store.approve(sig)
            else:
                store.mark_resume_approved(sig)
        else:
            from systemu.runtime.command_approvals import command_signature
            sig = command_signature(dctx.get("command") or "", cwd=dctx.get("cwd") or "")
            store.mark_resume_approved(sig)
    except Exception:
        logger.debug("[ResumeOnDecision] could not record gate approval", exc_info=True)


def _stamp_dispatched(decision, vault) -> None:
    """Stamp the persisted ``resume_dispatched`` marker so a concurrent poll/event
    can't double-dispatch (across restarts, across both trigger paths)."""
    try:
        decision.context["resume_dispatched"] = True
        vault.save_decision(decision)
    except Exception:
        logger.debug(
            "[ResumeOnDecision] could not stamp resume_dispatched on %s",
            decision.id, exc_info=True,
        )


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
