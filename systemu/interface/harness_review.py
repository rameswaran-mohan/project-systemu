"""Operator-facing surface for escalated Reverse-Harness requests (Phase 2.4).

This module bridges the Governor's per-execution harness ledger and the
operator decision mechanism so that ESCALATED HarnessRequests reach an
operator card on the dashboard (Pending Actions tab / /insights page).

Public API
----------
surface_harness_request(request, verdict, *, execution_id, vault) -> str
    Surface an escalated HarnessRequest as an OperatorDecision card with
    options Approve / Edit spec / Deny.  Returns the decision_id.
    Dedup key format: ``harness:<execution_id>:<request_id>``.

load_harness_ledger(execution_id, vault) -> list[dict]
    Read all JSONL lines from the Governor's ledger for execution_id.
    Returns an empty list for missing / empty ledgers (never raises).

summarize_harness(execution_id, vault) -> dict
    Aggregate ledger lines into a summary dict:
    ``{counts_by_kind, counts_by_verdict, total, leases}``.
    Returns an empty summary dict when the ledger is absent (never raises).

Dashboard note
--------------
A dedicated dashboard page is NOT added in this phase.  The existing
/insights Pending Actions tab already renders every OperatorDecision from
the queue — harness review cards surface there automatically once
surface_harness_request() posts the decision.  The read model helpers
(load_harness_ledger / summarize_harness) are CLI-friendly and can also
be consumed by a future dedicated /harness-review page.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Import log_event at module level so tests can patch it as
# "systemu.interface.harness_review.log_event".
# The import is guarded so that harness_review can be imported even
# in lean test environments that don't have the full notifications stack.
try:
    from systemu.interface.notifications import log_event  # noqa: F401
except Exception:  # pragma: no cover
    def log_event(*args, **kwargs):  # type: ignore[misc]
        pass

# ─────────────────────────────────────────────────────────────────────────────
#  Constants
# ─────────────────────────────────────────────────────────────────────────────

_HARNESS_OPTIONS: List[str] = ["Deny", "Approve", "Edit spec"]
"""Decision options presented to the operator.

Ordering contract (mirrors notify_user's actions[0]-is-safe rule):
  - index 0 → safe-default: Deny (refuse the capability request)
  - index 1 → Approve (grant the capability at the operator's explicit choice)
  - index 2 → Edit spec (approve but signal the spec needs revision)
"""


# ─────────────────────────────────────────────────────────────────────────────
#  surface_harness_request
# ─────────────────────────────────────────────────────────────────────────────

def surface_harness_request(
    request,
    verdict,
    *,
    execution_id: str,
    activity_id: str = "",
    shadow_id: str = "",
    vault,
) -> str:
    """Surface an escalated HarnessRequest as an operator decision card.

    Posts an OperatorDecision to the vault-backed OperatorDecisionQueue via
    the same mechanism used by ``notify_user`` in queue-mode and by
    ``_ask_stuck_or_degrade`` in shadow_runtime.  The operator sees the card
    on the /insights Pending Actions tab and can Approve, Edit spec, or Deny.

    Parameters
    ----------
    request:
        The ``HarnessRequest`` that received an ESCALATE verdict.
    verdict:
        The ``HarnessVerdict`` from Governor.arbitrate().
    execution_id:
        The execution context that triggered the request.
    vault:
        A Vault instance supporting ``save_decision`` / ``load_index`` /
        ``get_decision`` (all three vault backends satisfy this contract).

    Returns
    -------
    The decision_id (``dec_<hex>``) of the newly posted (or already-pending)
    OperatorDecision.

    Notes
    -----
    * The dedup key ``harness:<execution_id>:<request_id>`` ensures repeated
      calls for the same escalation return the existing pending decision
      rather than creating duplicates.
    * This function does **not** raise PendingOperatorDecision — it is a
      "fire and surface" call; the caller is responsible for deciding whether
      to suspend or continue on ESCALATE.
    """
    request_id: str = getattr(request, "request_id", "") or ""
    kind_val: str = getattr(request.kind, "value", str(request.kind))
    spec: Dict[str, Any] = getattr(request, "spec", {}) or {}
    rationale: str = getattr(request, "rationale", "") or ""
    urgency: str = getattr(request, "urgency", "normal") or "normal"
    blocking: bool = getattr(request, "blocking", True)

    verdict_decision: str = getattr(verdict.decision, "value", str(verdict.decision))
    verdict_risk: str = getattr(verdict.risk_band, "value", str(verdict.risk_band))
    verdict_rationale: str = getattr(verdict, "rationale", "") or ""

    dedup_key = f"harness:{execution_id}:{request_id}"

    # ── Build human-readable body ─────────────────────────────────────────────
    spec_preview = json.dumps(spec, default=str)
    if len(spec_preview) > 400:
        spec_preview = spec_preview[:397] + "..."

    body_lines = [
        f"Kind: {kind_val}  |  Risk: {verdict_risk}  |  Urgency: {urgency}  |  Blocking: {blocking}",
        "",
        f"Agent rationale: {rationale or '(none)'}",
        f"Verdict rationale: {verdict_rationale or '(none)'}",
        "",
        f"Spec: {spec_preview}",
    ]
    body = "\n".join(body_lines)

    # ── Harness-specific context preserved across the re-tag ──────────────────
    # The row is re-tagged to a kind="gate" / gate_type="harness" descriptor so
    # InboxQueue.list_descriptors() surfaces it (it filters ctx["kind"]=="gate").
    # These extras ride alongside the descriptor's serialized context via
    # enqueue(context_extras=...) so the FUTURE grant-resume executor (and the
    # current renderers) still have execution_id / request_id / harness_kind /
    # spec / verdict / rationale to work with. (`kind`/`gate_type` are NOT placed
    # here — to_decision_context owns them, and enqueue makes them win regardless.)
    context_extras: Dict[str, Any] = {
        "execution_id":      execution_id,
        # Resume coords — the daemon harness-grant reconciler reads these to call
        # Supervisor.resume_after_grant(execution_id=, activity_id=, shadow_id=).
        "activity_id":       activity_id,
        "shadow_id":         shadow_id,
        "request_id":        request_id,
        "harness_kind":      kind_val,
        "risk_band":         verdict_risk,
        "urgency":           urgency,
        "blocking":          blocking,
        "verdict":           verdict_decision,
        "spec":              spec,
        "rationale":         rationale,
        "verdict_rationale": verdict_rationale,
    }

    # Merge chat_submission_id from contextvar so the chat UI can link the card
    try:
        from systemu.runtime.chat_submission_ctx import current_chat_submission_id
        cid = current_chat_submission_id()
        if cid:
            context_extras["chat_submission_id"] = cid
    except Exception:
        pass  # chat-submission linkage is best-effort enrichment — never block surfacing

    # ── Surface as a gate via the Inbox facade ────────────────────────────────
    # GateDescriptor.from_harness mirrors this surface's options / safe-default
    # ("Deny") / dedup-key format verbatim (gate.py:94); enqueue posts through the
    # SAME OperatorDecisionQueue and stamps kind="gate"/gate_type="harness", which
    # is what list_descriptors() needs to show the harness review card.
    from systemu.interface.command.gate import GateDescriptor
    from systemu.interface.command.inbox import InboxQueue

    descriptor = GateDescriptor.from_harness(
        request, verdict, execution_id=execution_id
    )
    decision_id = InboxQueue(vault).enqueue(
        descriptor,
        gate_type="harness",
        body=body,
        context_extras=context_extras,
    )

    logger.info(
        "[HarnessReview] surfaced decision_id=%s dedup_key=%r execution_id=%s request_id=%s",
        decision_id,
        dedup_key,
        execution_id,
        request_id,
    )

    # ── Emit event log entry so the event feed shows the escalation ───────────
    try:
        log_event(
            level="WARNING",
            category="harness",
            message=f"Harness request ESCALATED: {kind_val} {request_id} — awaiting operator",
            context={
                "execution_id": execution_id,
                "request_id":   request_id,
                "decision_id":  decision_id,
                "dedup_key":    dedup_key,
                "kind":         kind_val,
                "risk_band":    verdict_risk,
            },
        )
    except Exception:
        pass  # log_event is best-effort; never block the surfacing

    return decision_id


# ─────────────────────────────────────────────────────────────────────────────
#  load_harness_ledger
# ─────────────────────────────────────────────────────────────────────────────

def _ledger_path(execution_id: str, vault) -> Path:
    """Resolve the JSONL ledger path for execution_id using vault root."""
    if vault is not None:
        root_attr = getattr(vault, "root", None)
        if root_attr:
            return Path(root_attr) / "harness_ledger" / f"{execution_id}.jsonl"
    return Path("data") / "systemu" / "vault" / "harness_ledger" / f"{execution_id}.jsonl"


def load_harness_ledger(execution_id: str, vault) -> List[Dict[str, Any]]:
    """Read all JSONL lines from the Governor's harness ledger for execution_id.

    Parameters
    ----------
    execution_id:
        The execution whose ledger to load.
    vault:
        A Vault instance (used to resolve the ledger path).

    Returns
    -------
    List of dicts, one per ledger line.  Empty list when the ledger file is
    absent, empty, or every line fails to parse.  Never raises.
    """
    path = _ledger_path(execution_id, vault)
    if not path.exists():
        return []
    entries: List[Dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for lineno, raw in enumerate(fh, 1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entries.append(json.loads(raw))
                except json.JSONDecodeError as exc:
                    logger.warning(
                        "[HarnessReview] ledger parse error (execution_id=%s line=%d): %s",
                        execution_id,
                        lineno,
                        exc,
                    )
    except Exception as exc:
        logger.error(
            "[HarnessReview] could not read ledger for execution_id=%s: %s",
            execution_id,
            exc,
        )
    return entries


# ─────────────────────────────────────────────────────────────────────────────
#  summarize_harness
# ─────────────────────────────────────────────────────────────────────────────

def summarize_harness(execution_id: str, vault) -> Dict[str, Any]:
    """Aggregate ledger entries into a summary dict.

    Parameters
    ----------
    execution_id:
        The execution to summarise.
    vault:
        A Vault instance (forwarded to load_harness_ledger).

    Returns
    -------
    Dict with keys:
        total          — int total ledger lines
        counts_by_kind — {kind_str: count}
        counts_by_verdict — {decision_str: count}
        leases         — list of lease_id strings from materialised grants
        execution_id   — echoed back for convenience

    All keys are present even when the ledger is absent or empty.  Never raises.
    """
    empty: Dict[str, Any] = {
        "execution_id":      execution_id,
        "total":             0,
        "counts_by_kind":    {},
        "counts_by_verdict": {},
        "leases":            [],
    }

    entries = load_harness_ledger(execution_id, vault)
    if not entries:
        return empty

    counts_by_kind: Dict[str, int] = {}
    counts_by_verdict: Dict[str, int] = {}
    leases: List[str] = []

    for entry in entries:
        # Extract kind
        kind = (entry.get("request") or {}).get("kind") or "unknown"
        counts_by_kind[kind] = counts_by_kind.get(kind, 0) + 1

        # Extract verdict decision
        decision = (entry.get("verdict") or {}).get("decision") or "unknown"
        counts_by_verdict[decision] = counts_by_verdict.get(decision, 0) + 1

        # Collect lease_ids from materialised outcomes
        outcome = entry.get("outcome") or {}
        lease_id = outcome.get("lease_id")
        if lease_id and lease_id not in leases:
            leases.append(lease_id)

    return {
        "execution_id":      execution_id,
        "total":             len(entries),
        "counts_by_kind":    counts_by_kind,
        "counts_by_verdict": counts_by_verdict,
        "leases":            leases,
    }
