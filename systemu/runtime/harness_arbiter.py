"""HarnessArbiter — deterministic, pure risk-scoring and policy layer.

No LLM calls, no I/O, no network. Every decision is reproducible given the
same (request, policy, context) triple. Ambiguous MEDIUM cases set
``needs_llm_judgment=True`` in the returned dict to signal the Governor's
LLM layer to resolve them.

Risk-band table (design spec §8):
┌────────────┬──────────────────────────────────────────┬────────┬────────────────┐
│ Kind       │ Condition                                │ Band   │ Default action │
├────────────┼──────────────────────────────────────────┼────────┼────────────────┤
│ TOOL       │ reuse existing (enabled tool in context) │ LOW    │ GRANT          │
│ TOOL       │ new code / forge                         │ HIGH   │ ESCALATE       │
│ SKILL      │ reuse existing                           │ LOW    │ GRANT          │
│ SKILL      │ new procedural text                      │ MEDIUM │ GRANT*/ESCALATE│
│ ACCESS     │ read, whitelisted resource               │ LOW    │ GRANT          │
│ ACCESS     │ read, non-whitelisted                    │ MEDIUM │ ESCALATE       │
│ ACCESS     │ write / secret / network                 │ HIGH   │ ESCALATE       │
│ COMPUTE    │ +budget within ceiling                   │ LOW    │ GRANT          │
│ COMPUTE    │ over ceiling                             │ HIGH   │ ESCALATE       │
│ SUBAGENT   │ within depth+budget                      │ MEDIUM │ GRANT*/ESCALATE│
│ SUBAGENT   │ beyond depth or budget                   │ HIGH   │ ESCALATE       │
│ INPUT      │ (always operator question)               │ MEDIUM │ ESCALATE       │
└────────────┴──────────────────────────────────────────┴────────┴────────────────┘
* = only when policy auto-grant flag is True and unambiguous

DENY semantics: issued when the request is policy-forbidden AND non-blocking
(blocking=False). Alternatives are always attached. blocking=True requests
that cannot be auto-granted are ESCALATE (suspend the run, await operator).
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from systemu.core.models import (
    HarnessDecision,
    HarnessKind,
    HarnessRequest,
    HarnessVerdict,
    RiskBand,
)
from systemu.runtime.harness_policy import HarnessPolicy


# ─────────────────────────────────────────────────────────────────────────────
#  Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _lease_id() -> str:
    return "lease_" + uuid.uuid4().hex[:10]


def _verdict(
    request: HarnessRequest,
    decision: HarnessDecision,
    risk_band: RiskBand,
    rationale: str,
    alternatives: Optional[List[str]] = None,
    lease: bool = False,
) -> HarnessVerdict:
    return HarnessVerdict(
        request_id=request.request_id,
        decision=decision,
        risk_band=risk_band,
        rationale=rationale,
        lease_id=_lease_id() if (lease and decision == HarnessDecision.GRANT) else None,
        alternatives=alternatives or [],
    )


def _result(
    request: HarnessRequest,
    decision: HarnessDecision,
    risk_band: RiskBand,
    rationale: str,
    needs_llm_judgment: bool = False,
    alternatives: Optional[List[str]] = None,
    lease: bool = False,
) -> Dict[str, Any]:
    """Build the full arbitration result dict."""
    verdict = _verdict(request, decision, risk_band, rationale, alternatives, lease)
    return {
        "verdict": verdict,
        "risk_band": risk_band,
        "needs_llm_judgment": needs_llm_judgment,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Per-kind arbitrators
# ─────────────────────────────────────────────────────────────────────────────

def _arbitrate_tool(
    request: HarnessRequest, policy: HarnessPolicy, ctx: Dict[str, Any]
) -> Dict[str, Any]:
    """TOOL kind — forge new code is always HIGH; reuse existing enabled tool is LOW."""
    spec = request.spec
    tool_name = spec.get("name", "")
    enabled_tools: List[str] = ctx.get("enabled_tools", [])
    is_reuse = bool(tool_name and tool_name in enabled_tools)

    if is_reuse:
        # LOW: reuse an already-enabled tool — auto-grant regardless of policy switch
        return _result(
            request, HarnessDecision.GRANT, RiskBand.LOW,
            f"Tool '{tool_name}' is already enabled — reuse is safe.",
            lease=True,
        )

    # NEW code / forge → always HIGH → always ESCALATE (default-deny)
    alts = request.fallback.split(";") if request.fallback else []
    alts = [a.strip() for a in alts if a.strip()]
    alts = alts or ["use an existing enabled tool", "request operator review"]
    if not request.blocking:
        return _result(
            request, HarnessDecision.DENY, RiskBand.HIGH,
            "Forging new tool code requires operator review (HIGH risk).",
            alternatives=alts,
        )
    return _result(
        request, HarnessDecision.ESCALATE, RiskBand.HIGH,
        "Forging new tool code requires operator review (HIGH risk — new executable code).",
    )


def _arbitrate_skill(
    request: HarnessRequest, policy: HarnessPolicy, ctx: Dict[str, Any]
) -> Dict[str, Any]:
    """SKILL kind — reuse existing is LOW; new procedural text is MEDIUM."""
    spec = request.spec
    skill_name = spec.get("name", "")
    existing_skills: List[str] = ctx.get("existing_skills", [])
    is_reuse = bool(skill_name and skill_name in existing_skills)

    if is_reuse:
        return _result(
            request, HarnessDecision.GRANT, RiskBand.LOW,
            f"Skill '{skill_name}' already exists — reuse is safe.",
            lease=True,
        )

    # New procedural text → MEDIUM
    if policy.auto_grant_skill:
        # Unambiguous grant: policy allows, no special flags needed
        return _result(
            request, HarnessDecision.GRANT, RiskBand.MEDIUM,
            "Policy auto-grants new skill text (MEDIUM risk — procedural knowledge only).",
            lease=True,
        )

    # Policy doesn't allow auto-grant — flag for LLM judgment
    return _result(
        request, HarnessDecision.ESCALATE, RiskBand.MEDIUM,
        "New skill text requires review (auto-grant disabled).",
        needs_llm_judgment=True,
    )


def _arbitrate_access(
    request: HarnessRequest, policy: HarnessPolicy, ctx: Dict[str, Any]
) -> Dict[str, Any]:
    """ACCESS kind — read whitelisted resource is LOW; write/secret/network is HIGH."""
    spec = request.spec
    resource = spec.get("resource", "")
    access_type = spec.get("access_type", "read").lower()  # "read" | "write" | "secret" | "network"

    # Write, secret access, or network egress → always HIGH → always ESCALATE
    high_ops = {"write", "secret", "network", "fs_write", "egress"}
    if access_type in high_ops or any(h in resource.lower() for h in ("secret", "credential", "passwd", "token")):
        return _result(
            request, HarnessDecision.ESCALATE, RiskBand.HIGH,
            f"ACCESS type '{access_type}' on '{resource}' is HIGH risk — operator review required.",
        )

    # Read operations
    is_whitelisted = resource in policy.allowed_resources
    if is_whitelisted:
        # LOW — auto-grant
        return _result(
            request, HarnessDecision.GRANT, RiskBand.LOW,
            f"Read access to '{resource}' is whitelisted — safe to grant.",
            lease=True,
        )

    # Non-whitelisted read → MEDIUM → ESCALATE (default-deny; policy.auto_grant_access
    # covers only pre-approved; non-whitelist reads still need judgment)
    return _result(
        request, HarnessDecision.ESCALATE, RiskBand.MEDIUM,
        f"Read access to non-whitelisted resource '{resource}' requires review.",
        needs_llm_judgment=True,
    )


def _arbitrate_compute(
    request: HarnessRequest, policy: HarnessPolicy, ctx: Dict[str, Any]
) -> Dict[str, Any]:
    """COMPUTE kind — within ceiling is LOW; over ceiling is HIGH."""
    spec = request.spec
    requested_fraction = float(spec.get("budget_fraction", 0.0))
    # Also accept absolute tokens and convert loosely
    requested_tokens = float(spec.get("tokens", 0))
    baseline_tokens = float(ctx.get("baseline_tokens", 100_000))

    # Normalise to a fraction if tokens were given instead
    if requested_tokens > 0 and baseline_tokens > 0:
        requested_fraction = max(requested_fraction, requested_tokens / baseline_tokens)

    within_ceiling = requested_fraction <= policy.max_compute_ceiling

    if within_ceiling and policy.auto_grant_compute:
        return _result(
            request, HarnessDecision.GRANT, RiskBand.LOW,
            f"Compute request ({requested_fraction:.0%} of baseline) is within ceiling "
            f"({policy.max_compute_ceiling:.0%}) — auto-granted.",
            lease=True,
        )

    if not within_ceiling:
        alts = [
            f"cap the request at {policy.max_compute_ceiling:.0%} of baseline budget",
            "request operator review for extended compute",
        ]
        if not request.blocking:
            return _result(
                request, HarnessDecision.DENY, RiskBand.HIGH,
                f"Compute request ({requested_fraction:.0%}) exceeds ceiling "
                f"({policy.max_compute_ceiling:.0%}).",
                alternatives=alts,
            )
        return _result(
            request, HarnessDecision.ESCALATE, RiskBand.HIGH,
            f"Compute request ({requested_fraction:.0%}) exceeds ceiling "
            f"({policy.max_compute_ceiling:.0%}) — operator review required.",
        )

    # within_ceiling but auto_grant_compute=False → MEDIUM → ESCALATE
    return _result(
        request, HarnessDecision.ESCALATE, RiskBand.MEDIUM,
        "Compute increase within ceiling, but auto-grant is disabled — escalating.",
        needs_llm_judgment=True,
    )


def _arbitrate_subagent(
    request: HarnessRequest, policy: HarnessPolicy, ctx: Dict[str, Any]
) -> Dict[str, Any]:
    """SUBAGENT kind — within depth+budget is MEDIUM; beyond is HIGH."""
    spec = request.spec
    # v0.9.33 Bug 3: the depth guard must reflect ACTUAL nesting, not just the
    # model-claimed spec.depth (a child could claim depth=1 forever and cascade).
    # Take the MAX of (a) the requester's real nesting + 1 (ctx["subagent_depth"]
    # is the requester's current depth; parent=0) and (b) the model-claimed
    # spec.depth — so a child cannot undercut its true depth by lying low, AND an
    # over-claimed large depth still escalates (preserving the existing contract).
    actual_next_depth = int(ctx.get("subagent_depth", 0)) + 1
    requested_depth = max(int(spec.get("depth", 1)), actual_next_depth)
    requested_budget_fraction = float(spec.get("budget_fraction", 0.0))

    beyond_depth = requested_depth > policy.max_subagent_depth
    beyond_budget = requested_budget_fraction > policy.max_subagent_budget_fraction

    if beyond_depth or beyond_budget:
        reasons = []
        if beyond_depth:
            reasons.append(
                f"requested depth {requested_depth} > max {policy.max_subagent_depth}"
            )
        if beyond_budget:
            reasons.append(
                f"requested budget {requested_budget_fraction:.0%} > "
                f"max {policy.max_subagent_budget_fraction:.0%}"
            )
        alts = [
            "reduce sub-Shadow nesting depth",
            "reduce sub-Shadow budget allocation",
            "request operator approval for extended subagent run",
        ]
        # Merge fallback string into alternatives
        if request.fallback:
            fallback_items = [a.strip() for a in request.fallback.split(";") if a.strip()]
            alts = list(dict.fromkeys(alts + fallback_items))
        if not request.blocking:
            return _result(
                request, HarnessDecision.DENY, RiskBand.HIGH,
                "SUBAGENT exceeds limits: " + "; ".join(reasons),
                alternatives=alts,
            )
        return _result(
            request, HarnessDecision.ESCALATE, RiskBand.HIGH,
            "SUBAGENT exceeds depth/budget limits — operator review required: "
            + "; ".join(reasons),
        )

    # Within limits → MEDIUM
    if policy.auto_grant_subagent:
        return _result(
            request, HarnessDecision.GRANT, RiskBand.MEDIUM,
            "Sub-Shadow is within depth and budget limits; policy auto-grants.",
            lease=True,
        )

    return _result(
        request, HarnessDecision.ESCALATE, RiskBand.MEDIUM,
        "Sub-Shadow spawn within limits but auto-grant is disabled.",
        needs_llm_judgment=True,
    )


def _arbitrate_input(
    request: HarnessRequest, policy: HarnessPolicy, ctx: Dict[str, Any]
) -> Dict[str, Any]:
    """INPUT kind — always ESCALATE (requires operator human answer)."""
    return _result(
        request, HarnessDecision.ESCALATE, RiskBand.MEDIUM,
        "INPUT requests always route to the operator for a human answer.",
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Public entry point
# ─────────────────────────────────────────────────────────────────────────────

_KIND_ARBITRATORS = {
    HarnessKind.TOOL:     _arbitrate_tool,
    HarnessKind.SKILL:    _arbitrate_skill,
    HarnessKind.ACCESS:   _arbitrate_access,
    HarnessKind.COMPUTE:  _arbitrate_compute,
    HarnessKind.SUBAGENT: _arbitrate_subagent,
    HarnessKind.INPUT:    _arbitrate_input,
}


def arbitrate(
    request: HarnessRequest,
    policy: HarnessPolicy,
    context: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Deterministic arbitration of a HarnessRequest.

    Parameters
    ----------
    request:
        The agent's capability request.
    policy:
        Operator-configured limits and allowlists.
    context:
        Optional runtime state used to resolve low-risk cases without LLM.
        Recognised keys:
          ``enabled_tools``          list[str]  — names of already-enabled tools
          ``existing_skills``        list[str]  — names of known skills
          ``requests_this_run``      int        — count of requests so far
          ``baseline_tokens``        int        — token baseline for COMPUTE band
          ``subagent_depth``         int        — current nesting depth (for SUBAGENT)

    Returns
    -------
    dict with keys:
      ``verdict``            HarnessVerdict
      ``risk_band``          RiskBand
      ``needs_llm_judgment`` bool  — True when the Governor's LLM should resolve

    Notes
    -----
    Default-deny: any kind without a registered arbitrator is ESCALATE HIGH.
    Per-run request cap: when ``requests_this_run >= policy.max_requests_per_run``
    the request is DENY (non-blocking) or ESCALATE (blocking) before
    kind-specific logic runs.
    """
    ctx = context or {}

    # ── 1. Per-run request cap ─────────────────────────────────────────────
    requests_this_run: int = int(ctx.get("requests_this_run", 0))
    if requests_this_run >= policy.max_requests_per_run:
        alts = ["wait for the next run", "batch capabilities before starting"]
        if not request.blocking:
            return _result(
                request, HarnessDecision.DENY, RiskBand.HIGH,
                f"Per-run request cap ({policy.max_requests_per_run}) exceeded.",
                alternatives=alts,
            )
        return _result(
            request, HarnessDecision.ESCALATE, RiskBand.HIGH,
            f"Per-run request cap ({policy.max_requests_per_run}) exceeded — "
            "operator must allow more requests or restart the run.",
        )

    # ── 2. Kind-specific arbitration ──────────────────────────────────────
    arbitrator = _KIND_ARBITRATORS.get(request.kind)
    if arbitrator is None:
        # Unknown kind → default-deny
        return _result(
            request, HarnessDecision.ESCALATE, RiskBand.HIGH,
            f"Unknown harness kind '{request.kind}' — default-deny.",
        )

    result = arbitrator(request, policy, ctx)

    # ── 3. Post-process: non-blocking policy-denied requests → DENY ────────
    # If the arbitrator returned ESCALATE but blocking=False, downgrade to
    # DENY so the run can continue without this capability.
    # Exception: INPUT always escalates (requires a human answer; cannot
    # be silently skipped — the run was already told to pause for input).
    verdict: HarnessVerdict = result["verdict"]
    if (
        not request.blocking
        and verdict.decision == HarnessDecision.ESCALATE
        and result["risk_band"] in (RiskBand.HIGH, RiskBand.MEDIUM)
        and request.kind != HarnessKind.INPUT
    ):
        # Only downgrade MEDIUM/HIGH escalations when not ambiguous
        alts = verdict.alternatives or []
        if request.fallback:
            fallback_items = [a.strip() for a in request.fallback.split(";") if a.strip()]
            alts = list(dict.fromkeys(alts + fallback_items))  # deduplicate, order-preserving
        if not alts:
            alts = ["continue without this capability", "request during next operator session"]
        new_verdict = HarnessVerdict(
            request_id=verdict.request_id,
            decision=HarnessDecision.DENY,
            risk_band=verdict.risk_band,
            rationale=verdict.rationale + " (non-blocking → DENY so run continues)",
            lease_id=None,
            alternatives=alts,
        )
        result = {
            "verdict": new_verdict,
            "risk_band": result["risk_band"],
            "needs_llm_judgment": result.get("needs_llm_judgment", False),
        }

    return result
