"""RQ1 (PRIMARY): pull-decision quality, parsed from the SHIPPED instrumentation.

Reads, per run:
  {vault}/executions/<exec_id>/decision_audit.jsonl
      one row per loop iteration (systemu.runtime.decision_audit.IterationDecision):
      blockage signals (loop_guard_active, stuck_round_count, consec_research_reads,
      consec_tool_failures) + the REQUEST_HARNESS pull instrumentation
      (is_request_harness, harness_kind, harness_confidence, harness_attempts_before).
  {vault}/harness_ledger/<exec_id>.jsonl
      append-only governor ledger (systemu.runtime.governor).  Two relevant row
      shapes:
        * arbitration rows: {"request": {... "attempts_before", "confidence"},
                             "verdict": {"decision", "decided_by", ...},
                             "outcome": {...}}
        * request-outcome events: {"event_type": "request-outcome",
                             "outcome": one of granted_used/granted_unused/
                             denied_fallback_ok/denied_fallback_failed/
                             escalate_unresolved,
                             "pull_failure_category": premature_request/
                             wasted_request/unused_grant/unknown}

Metrics (paper §5.1/§5.4): precision/recall of blocked->pulled, premature/wasted/
unused-request rates, used-vs-unused grants, and the deterministic-vs-LLM
``decided_by`` split (RQ3 input).  Observational only — it never grades the goal
(that is the external oracle in cgb_eval.oracle).

The shipped ledger differs from the original plan sketch: ``decided_by`` /
``decision`` live nested under the arbitration row's ``verdict`` (not flat on a
verdict row), and the terminal usage outcome is carried on a separate
``request-outcome`` event (``outcome`` + ``pull_failure_category``), not on the
verdict.  This module reads the real shapes; it also tolerates the flat shape so
synthetic test fixtures and any future flattening still parse.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List

# A request is "premature" if it fired before trying >= N available tools.  Mirrors
# systemu.runtime.failure_classifier.classify_pull_failure (attempts_before < 1).
MIN_ATTEMPTS_BEFORE_REQUEST = 1

# Stuck/blocked thresholds mirror the loop-health signals in decision_audit.
_STUCK_ROUND_THRESHOLD = 2
_TOOL_FAIL_THRESHOLD = 2

_OUTCOME_GRANTED_USED = "granted_used"
_OUTCOME_GRANTED_UNUSED = "granted_unused"
_OUTCOME_DENIED_FALLBACK_OK = "denied_fallback_ok"
_OUTCOME_DENIED_FALLBACK_FAILED = "denied_fallback_failed"
_OUTCOME_ESCALATE_UNRESOLVED = "escalate_unresolved"
_OUTCOME_DENIED_CAP = "denied_cap"          # v0.9.41: request denied by the per-run cap
_OUTCOME_GRANTED_NA = "granted"             # v0.9.41: granted, usage indeterminate (access advisory / subagent fleet)

_CAT_PREMATURE = "premature_request"
_CAT_WASTED = "wasted_request"
_CAT_UNUSED = "unused_grant"
_CAT_CAP = "cap_exceeded"                    # v0.9.41: over-delegation — denied at the run-tree cap


def _read_jsonl(p: Path) -> List[dict]:
    if not p.is_file():
        return []
    rows: List[dict] = []
    for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            rows.append(json.loads(ln))
        except Exception:
            continue
    return rows


def _is_blocked(row: dict) -> bool:
    """Genuine blockage signal active at decision time (from decision_audit)."""
    return bool(row.get("loop_guard_active")) \
        or int(row.get("stuck_round_count", 0) or 0) >= _STUCK_ROUND_THRESHOLD \
        or int(row.get("consec_tool_failures", 0) or 0) >= _TOOL_FAIL_THRESHOLD


def _verdict_decided_by(row: dict) -> str:
    """Read decided_by from a ledger arbitration row (nested or flat)."""
    verd = row.get("verdict")
    if isinstance(verd, dict) and verd.get("decided_by"):
        return str(verd["decided_by"])
    return str(row.get("decided_by") or "")


def _request_attempts_before(row: dict) -> int:
    """Read attempts_before from a ledger arbitration row (nested or flat)."""
    req = row.get("request")
    if isinstance(req, dict) and "attempts_before" in req:
        try:
            return int(req.get("attempts_before") or 0)
        except (TypeError, ValueError):
            return 0
    try:
        return int(row.get("attempts_before", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _is_arbitration_row(row: dict) -> bool:
    """An arbitration row carries a request+verdict and is NOT an event row."""
    if row.get("event_type"):
        return False
    return isinstance(row.get("verdict"), dict) or bool(row.get("decided_by"))


def _row_outcome(row: dict) -> str:
    """The terminal request-outcome string from a request-outcome event row.

    Also accepts a flat ``request_outcome`` key for synthetic/flattened fixtures.
    """
    if row.get("event_type") == "request-outcome":
        return str(row.get("outcome") or "")
    # Flattened fixture shape: a verdict row carrying request_outcome directly.
    return str(row.get("request_outcome") or "")


def extract_pull_decision(vault_dir, family: str) -> Dict[str, int]:
    """Aggregate one run's pull-decision instrumentation into RQ1/RQ3 counts.

    Returns zeroed counts if the instrumentation is absent (a push run, or Build 1
    not landed) so the runner can store a uniform record shape.
    """
    vault_dir = Path(vault_dir)
    exec_root = vault_dir / "executions"
    ledger_dir = vault_dir / "harness_ledger"

    audit: List[dict] = []
    if exec_root.is_dir():
        for ex in exec_root.iterdir():
            if ex.is_dir():
                audit += _read_jsonl(ex / "decision_audit.jsonl")

    ledger_rows: List[dict] = []
    if ledger_dir.is_dir():
        for f in ledger_dir.glob("*.jsonl"):
            ledger_rows += _read_jsonl(f)

    # ── Blockage + request signals from decision_audit ─────────────────────────
    blocked_iters = sum(1 for r in audit if _is_blocked(r))
    requests = [r for r in audit if r.get("is_request_harness")]

    # premature: request emitted without enough prior tool attempts
    premature = sum(
        1 for r in requests
        if int(r.get("harness_attempts_before", 0) or 0) < MIN_ATTEMPTS_BEFORE_REQUEST
    )
    # blocked->pulled true positive: a request that followed (at/after) a blocked iter
    blocked_then_pulled = sum(
        1 for r in requests
        if any(_is_blocked(a) for a in audit
               if int(a.get("iteration", 0) or 0) <= int(r.get("iteration", 0) or 0))
    )

    # ── Verdict provenance (decided_by) from the ledger arbitration rows ────────
    arb_rows = [r for r in ledger_rows if _is_arbitration_row(r)]
    decided_by_det = sum(1 for r in arb_rows if _verdict_decided_by(r) == "deterministic")
    decided_by_llm = sum(1 for r in arb_rows if _verdict_decided_by(r) == "llm")

    # ── Terminal outcomes (granted_used/unused, fallback, escalate) ────────────
    outcomes = [_row_outcome(r) for r in ledger_rows]
    outcomes = [o for o in outcomes if o]
    granted_used = outcomes.count(_OUTCOME_GRANTED_USED)
    granted_unused = outcomes.count(_OUTCOME_GRANTED_UNUSED)
    # wasted: a DENY where a viable fallback existed (Build 1 taxonomy)
    wasted = outcomes.count(_OUTCOME_DENIED_FALLBACK_OK)
    escalate_unresolved = outcomes.count(_OUTCOME_ESCALATE_UNRESOLVED)
    denied_fallback_failed = outcomes.count(_OUTCOME_DENIED_FALLBACK_FAILED)
    cap_exceeded = outcomes.count(_OUTCOME_DENIED_CAP)  # v0.9.41 over-delegation denials
    granted_na = outcomes.count(_OUTCOME_GRANTED_NA)    # v0.9.41 granted, usage N/A by kind
    unused_grant = granted_unused

    # ── Pull-failure taxonomy from the request-outcome events (authoritative) ──
    categories = [
        str(r.get("pull_failure_category") or "")
        for r in ledger_rows if r.get("event_type") == "request-outcome"
    ]
    # Prefer the ledger's own classification when present; fall back to the
    # decision_audit-derived premature count when no event rows exist (push/old).
    cat_premature = categories.count(_CAT_PREMATURE)
    cat_wasted = categories.count(_CAT_WASTED)
    cat_unused = categories.count(_CAT_UNUSED)
    cat_cap = categories.count(_CAT_CAP)
    if categories:
        premature = cat_premature
        wasted = cat_wasted
        unused_grant = cat_unused
        cap_exceeded = cat_cap

    return {
        "blocked_iters": blocked_iters,
        "requests": len(requests),
        "blocked_then_pulled": blocked_then_pulled,
        "premature": premature,
        "wasted": wasted,
        "unused_grant": unused_grant,
        "cap_exceeded": cap_exceeded,
        "granted_na": granted_na,
        "granted_used": granted_used,
        "granted_unused": granted_unused,
        "denied_fallback_failed": denied_fallback_failed,
        "escalate_unresolved": escalate_unresolved,
        "decided_by_det": decided_by_det,
        "decided_by_llm": decided_by_llm,
    }
