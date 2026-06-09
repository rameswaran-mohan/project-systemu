"""v0.9.8 Phase 2 — autonomous mid-run steering coach.

When the runtime detects that the agent has STALLED (no objective credit for N
iterations, or a tool failing over and over), it can FIRST try to self-steer
before escalating to a human operator. ``generate_steer`` makes a fresh-context
Tier-1 LLM call that diagnoses the stall and returns ONE concrete corrective
instruction the runtime injects as an operator-style hint to retry with.

Design posture (mirrors harness_judge.py / goal_verifier.py): fail-safe. On ANY
exception, missing client, unparseable output, empty steer, or low confidence,
``generate_steer`` returns "" — an empty string. An empty steer means "no steer";
the caller then falls through to the existing operator-escalation path, exactly as
it behaved before this module existed. ``generate_steer`` NEVER raises.

The LLM-call idiom (system prompt loaded from systemu/prompts/, Tier-1
``llm_call_json``, JSON parse + try/except fallback) is copied from
harness_judge.judge_harness_request so it behaves identically in this codebase.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from systemu.core.llm_router import llm_call_json

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "coach_steer.md"

# Minimum self-reported confidence below which a steer is discarded (we'd rather
# escalate to a human than inject a steer the coach itself isn't sure about).
_MIN_STEER_CONFIDENCE = 0.5


def _load_system_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _objective_view(objective) -> Dict[str, Any]:
    """Compact, JSON-serialisable view of the stalled objective for the LLM."""
    if objective is None:
        return {}
    return {
        "id": getattr(objective, "id", None),
        "goal": (
            getattr(objective, "goal", None)
            or getattr(objective, "description", None)
            or getattr(objective, "intent", None)
            or str(objective)
        ),
        "success_criteria": getattr(objective, "success_criteria", None),
    }


def generate_steer(
    *,
    objective,
    reason: str,
    tools_tried: Optional[List[str]],
    history: Optional[list],
    config,
) -> str:
    """Ask an LLM for ONE concrete corrective instruction to unstick a stalled run.

    Parameters
    ----------
    objective:
        The objective the agent is stuck on (carries id + goal/description).
    reason:
        Why the runtime decided the agent is stalled (the stuck-trigger reason).
    tools_tried:
        The tools currently failing (those with an active failure streak).
    history:
        A short chronological excerpt of recent tool calls / results / thoughts
        (already bounded by ``_build_history_slice``).
    config:
        Runtime config object (used for the LLM client + verifier_tier).

    Returns
    -------
    str
        ONE imperative corrective instruction, or "" when no usable steer is
        available. "" means "escalate to operator as before".

    FAIL-SAFE: any exception, missing LLM client, malformed output, empty steer,
    or confidence < 0.5 → "". Never raises.
    """
    # ── Build the user payload from objective + reason + tools + history ───────
    user_payload: Dict[str, Any] = {
        "objective": _objective_view(objective),
        "reason": str(reason or ""),
        "tools_tried": list(tools_tried or []),
        "history": history or [],
    }

    # ── Fresh-context Tier-1 LLM call (same idiom as harness_judge) ───────────
    tier = int(getattr(config, "verifier_tier", 1))
    try:
        result = llm_call_json(
            tier=tier,
            system=_load_system_prompt(),
            user=json.dumps(user_payload, separators=(",", ":"), default=str),
            config=config,
            # v0.9.8 (B7): reasoning models burn a 200-token budget on visible
            # chain-of-thought and never reach the JSON steer (-> "no steer" every
            # time, leaving the coach dead on hard tasks). Give it room to finish.
            max_tokens=1500,
            temperature=0.0,
        )
    except Exception as exc:
        logger.warning(
            "[Coach] LLM call failed (tier=%s) for objective %s: %s — no steer",
            tier, getattr(objective, "id", "?"), exc,
        )
        return ""

    # ── Validate response shape ───────────────────────────────────────────────
    if not isinstance(result, dict) or "steer" not in result:
        logger.warning(
            "[Coach] Malformed LLM response (missing 'steer'): %r — no steer",
            result,
        )
        return ""

    steer = str(result.get("steer") or "").strip()
    try:
        confidence = float(result.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0

    # ── Apply the conservative gate ───────────────────────────────────────────
    if not steer:
        logger.info("[Coach] empty steer for objective %s — no steer",
                    getattr(objective, "id", "?"))
        return ""
    if confidence < _MIN_STEER_CONFIDENCE:
        logger.info(
            "[Coach] steer confidence %.2f (< %.2f) for objective %s — discarding, "
            "will escalate to operator",
            confidence, _MIN_STEER_CONFIDENCE, getattr(objective, "id", "?"),
        )
        return ""

    return steer[:600]
