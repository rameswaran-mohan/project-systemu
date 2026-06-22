"""v0.9.7 Phase 4.1 — LLM judge for ambiguous MEDIUM-risk harness requests.

The deterministic ``harness_arbiter`` resolves every LOW/HIGH case and the
unambiguous MEDIUM cases on its own. The genuinely-ambiguous MEDIUM cases
(new-skill text with auto-grant off, non-whitelisted reads, within-budget
sub-shadows / compute with auto-grant off) are flagged ``needs_llm_judgment``
and default to ESCALATE. This module gives the Governor a way to ask an LLM to
resolve those — without ever silently granting something risky.

Design posture (mirrors goal_verifier.py): fail-safe. On ANY exception, missing
client, unparseable output, or low confidence, we return ESCALATE / lease=False.
The judge can only ever *downgrade* the default ESCALATE to GRANT/DENY when it is
confident; it never opens a hole when something goes wrong.

The LLM-call idiom (system prompt loaded from systemu/prompts/, Tier-1
``llm_call_json``, JSON parse + try/except fallback) is copied from
goal_verifier.verify_goal so it behaves identically in this codebase.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict

from systemu.core.models import HarnessDecision
from systemu.core.llm_router import llm_call_json

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "harness_judge.md"

# Minimum confidence below which a GRANT is downgraded to ESCALATE (never trust a
# hesitant grant — escalate to a human instead).
_MIN_GRANT_CONFIDENCE = 0.6


def _load_system_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _escalate(rationale: str, confidence: float = 0.0) -> Dict[str, Any]:
    """Build the conservative fall-safe verdict: ESCALATE, no lease."""
    return {
        "decision": HarnessDecision.ESCALATE,
        "rationale": rationale,
        "confidence": float(confidence),
        "lease": False,
    }


def _policy_limits(policy) -> Dict[str, Any]:
    """Extract the operator-relevant policy limits to show the judge."""
    return {
        "auto_grant_skill": getattr(policy, "auto_grant_skill", None),
        "auto_grant_access": getattr(policy, "auto_grant_access", None),
        "auto_grant_compute": getattr(policy, "auto_grant_compute", None),
        "auto_grant_subagent": getattr(policy, "auto_grant_subagent", None),
        "max_compute_ceiling": getattr(policy, "max_compute_ceiling", None),
        "max_subagent_depth": getattr(policy, "max_subagent_depth", None),
        "max_subagent_budget_fraction": getattr(policy, "max_subagent_budget_fraction", None),
        "allowed_resources": sorted(getattr(policy, "allowed_resources", set()) or []),
    }


def judge_harness_request(
    *,
    request,
    arb_result: Dict[str, Any],
    policy,
    context: Dict[str, Any] | None,
    config,
) -> Dict[str, Any]:
    """Ask an LLM to resolve an ambiguous MEDIUM-risk harness request.

    Parameters
    ----------
    request:
        The ``HarnessRequest`` the agent emitted.
    arb_result:
        The deterministic arbiter's result dict (``verdict``, ``risk_band``,
        ``needs_llm_judgment``). Its verdict rationale is forwarded as the
        ``arbiter_rationale`` so the judge knows *why* this was flagged.
    policy:
        The active ``HarnessPolicy`` — relevant limits are surfaced to the judge.
    context:
        Optional runtime state (enabled_tools, existing_skills, budgets, …).
    config:
        Runtime config object (used for the LLM client + verifier_tier).

    Returns
    -------
    dict with keys:
        decision    HarnessDecision  — GRANT / DENY / ESCALATE
        rationale   str              — the judge's one-sentence reason
        confidence  float            — 0.0-1.0 self-reported certainty
        lease       bool             — True iff a capability lease should be minted
                                       (only ever True for a confident GRANT)

    CONSERVATIVE FALLBACK: any exception, missing LLM client, malformed output,
    or a GRANT with confidence < 0.6 → ESCALATE, lease=False. Never silently
    GRANTs. Never raises.
    """
    ctx = context or {}

    # ── Build the verdict rationale the arbiter attached (the "why flagged") ──
    arbiter_rationale = ""
    try:
        arb_verdict = arb_result.get("verdict") if isinstance(arb_result, dict) else None
        if arb_verdict is not None:
            arbiter_rationale = getattr(arb_verdict, "rationale", "") or ""
    except Exception:
        arbiter_rationale = ""

    # ── Build the user payload from request + arbiter + policy + context ──────
    user_payload: Dict[str, Any] = {
        "kind": getattr(request.kind, "value", str(request.kind)),
        "spec": request.spec,
        "rationale": request.rationale,
        "fallback": request.fallback,
        "blocking": request.blocking,
        "arbiter_rationale": arbiter_rationale,
        "policy": _policy_limits(policy),
        "context": {
            "enabled_tools": ctx.get("enabled_tools", []),
            "existing_skills": ctx.get("existing_skills", []),
            "baseline_tokens": ctx.get("baseline_tokens"),
            "subagent_depth": ctx.get("subagent_depth"),
        },
    }

    # ── Fresh-context Tier-1 LLM call (same idiom as goal_verifier) ───────────
    tier = int(getattr(config, "verifier_tier", 1))
    try:
        result = llm_call_json(
            tier=tier,
            system=_load_system_prompt(),
            user=json.dumps(user_payload, separators=(",", ":"), default=str),
            config=config,
            max_tokens=300,
            temperature=0.0,
        )
    except Exception as exc:
        logger.warning(
            "[HarnessJudge] LLM call failed (tier=%s) for request %s: %s — escalating",
            tier, getattr(request, "request_id", "?"), exc,
        )
        return _escalate(f"judge error: {exc}")

    # ── Validate response shape ───────────────────────────────────────────────
    if not isinstance(result, dict) or "decision" not in result:
        logger.warning(
            "[HarnessJudge] Malformed LLM response (missing 'decision'): %r — escalating",
            result,
        )
        return _escalate("judge output malformed/unparsable")

    raw_decision = str(result.get("decision", "")).strip().upper()
    try:
        confidence = float(result.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    rationale = str(result.get("rationale") or "")[:400]

    # ── Map + apply the conservative rules ────────────────────────────────────
    if raw_decision == "GRANT":
        if confidence < _MIN_GRANT_CONFIDENCE:
            logger.info(
                "[HarnessJudge] GRANT with low confidence %.2f (< %.2f) for request %s "
                "— downgrading to ESCALATE",
                confidence, _MIN_GRANT_CONFIDENCE, getattr(request, "request_id", "?"),
            )
            return _escalate(
                rationale or "judge granted but confidence below threshold",
                confidence,
            )
        return {
            "decision": HarnessDecision.GRANT,
            "rationale": rationale or "judge granted: clearly safe and within policy",
            "confidence": confidence,
            "lease": True,
        }

    if raw_decision == "DENY":
        return {
            "decision": HarnessDecision.DENY,
            "rationale": rationale or "judge denied: outside policy, agent can continue without it",
            "confidence": confidence,
            "lease": False,
        }

    # ESCALATE (explicit) or any unrecognised decision → conservative escalate.
    return _escalate(rationale or "judge escalated: uncertain — operator review required", confidence)
