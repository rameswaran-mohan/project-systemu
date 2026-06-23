"""Modeled operator approval + resume for the eval harness.

WHAT THIS IS (and the honesty boundary).  When the agent pull-provisions a
HIGH-risk capability (e.g. forging new tool code), the Governor correctly
ESCALATES and the runtime SUSPENDS, awaiting a human. That escalation is a real,
recorded event (the harness ledger / decision card) and is what RQ4 measures --
this module does NOT bypass or hide it. To measure end-to-end *recovery* (RQ2) in
an unattended batch, we model the operator's APPROVE: materialise the grant
(forge), stamp the resume snapshot, and drive the runtime's existing
resume-after-grant path. We model a *yes*; we never auto-grant inside the
Governor and never suppress the escalation. The paper discloses this as modeled
operator approval, and reports the escalation (RQ4) separately from recovery
(RQ2).

Mechanically this mirrors the daemon's ``reconcile_resolved_harness_grants`` +
``Supervisor.resume_after_grant`` (jobs.py / supervisor.py), minus the worker
pool: we re-invoke ``ShadowRuntime.execute(..., resume_from_execution_id=...)``
directly, which reads the snapshot, peels the ``__HARNESS_GRANT__`` note, and
applies the grant via the same helper the autonomous GRANT path uses.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional


def _pending_harness_escalations(vault) -> List[Any]:
    """Return decision objects that are escalated, undispatched harness gates."""
    try:
        headers = vault.load_index("decisions") or []
    except Exception:
        return []
    out: List[Any] = []
    for h in headers:
        did = h.get("id") if isinstance(h, dict) else None
        if not did:
            continue
        try:
            dec = vault.get_decision(did)
        except Exception:
            continue
        ctx = getattr(dec, "context", None) or {}
        if ctx.get("gate_type") != "harness":
            continue
        if ctx.get("harness_grant_dispatched"):
            continue
        if ctx.get("verdict") != "escalate":
            continue
        out.append(dec)
    return out


def approve_and_resume_once(runtime, shadow, activity, vault, config) -> Optional[Dict[str, Any]]:
    """Model the operator APPROVING every pending harness escalation, materialise
    the grants, stamp the resume snapshot, and resume the execution ONCE.

    Returns the resumed ``execute()`` result dict, or ``None`` when nothing was
    pending (no escalation to approve).
    """
    from systemu.core.models import (
        HarnessRequest, HarnessVerdict, HarnessKind, HarnessDecision, RiskBand,
    )
    from systemu.runtime.governor import Governor
    from systemu.runtime.execution_snapshot import read_snapshot, write_snapshot
    from systemu.scheduler.jobs import _map_grant_payload

    pending = _pending_harness_escalations(vault)
    if not pending:
        return None

    exec_id: Optional[str] = None
    for dec in pending:
        ctx = getattr(dec, "context", None) or {}
        exec_id = ctx.get("execution_id")
        if not exec_id:
            continue
        kind = (ctx.get("harness_kind") or "").lower()

        if kind == "input":
            # ASK_OPERATOR — model a neutral acknowledgement.
            grant_payload: Dict[str, Any] = {
                "kind": "input", "granted": True,
                "operator_answer": ctx.get("operator_answer") or "Proceed.",
            }
        else:
            req = HarnessRequest(
                request_id=ctx.get("request_id", "") or "",
                kind=HarnessKind(kind),
                spec=ctx.get("spec") or {},
                rationale=ctx.get("rationale", "") or "",
                fallback=ctx.get("fallback", "") or "",
            )
            rb = ctx.get("risk_band", "high")
            rb = rb if rb in ("low", "medium", "high") else "high"
            verdict = HarnessVerdict(
                request_id=req.request_id,
                decision=HarnessDecision.GRANT,
                risk_band=RiskBand(rb),
                rationale="operator approved (eval-modeled)",
            )
            materialised = Governor(config).materialise(
                req, verdict, vault=vault, config=config, execution_id=exec_id)
            grant_payload = _map_grant_payload(kind, materialised)

        # Stamp the grant onto the suspend snapshot (resume_after_grant's contract).
        snap = read_snapshot(exec_id)
        if snap is not None:
            key = f"__HARNESS_GRANT__::{exec_id}"
            if not any(str(n).startswith(key) for n in snap.sticky_notes):
                snap.sticky_notes.append(
                    f"{key}::{json.dumps(grant_payload, separators=(',', ':'))}")
                write_snapshot(snap)

        # Mark the card resolved/approved (bookkeeping; mirrors the operator click).
        try:
            dec.context["harness_grant_dispatched"] = True
            dec.status = "resolved"
            dec.choice = "approve"
            vault.save_decision(dec)
        except Exception:
            pass

    if exec_id is None:
        return None
    return asyncio.run(
        runtime.execute(shadow, activity, resume_from_execution_id=exec_id))
