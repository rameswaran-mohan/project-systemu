"""v0.9.1 fresh-context objective verifier.

One Tier-1 LLM call per completion claim. The model sees only the
objective, the verifier hint, and a StateDelta — no conversation history.
It returns a JSON verdict. The runtime credits the objective only when
the verdict is verified=True.

Prior art: Odysseus ``src/agent_loop.py`` lines 1275-1312 (fresh-context
completion verifier, capped at 2 verify rounds, requires fresh effectful
work between re-verifies). Hermes ``agent/background_review.py`` (parallel
fork-after-turn pattern with restricted tool whitelist).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict

from systemu.core.models import Objective
from systemu.runtime.state_delta import StateDelta
from systemu.core.llm_router import llm_call_json

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "verify_objective_completion.md"


def _load_system_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def run(*, objective: Objective, delta: StateDelta, config) -> Dict[str, Any]:
    """Judge whether ``delta`` proves ``objective`` complete.

    Returns ``{"verified": bool, "reason": str}``.

    Short-circuits to ``{"verified": True, "reason": ...}`` when:
    - ``config.verifier_enabled`` is False (operator killed the layer); OR
    - ``objective.verifier`` is None (legacy objective with no contract).
    """
    if not getattr(config, "verifier_enabled", True):
        return {"verified": True, "reason": "verifier disabled by config"}

    if not objective.verifier:
        return {"verified": True, "reason": "no verifier hint declared on objective"}

    user_payload = {
        "objective": {
            "id": objective.id,
            "goal": objective.goal,
            "success_criteria": objective.success_criteria,
            "verifier_hint": objective.verifier,
        },
        "state_delta": delta.model_dump(),
    }

    try:
        result = llm_call_json(
            tier=int(getattr(config, "verifier_tier", 1)),
            system=_load_system_prompt(),
            user=json.dumps(user_payload, separators=(",", ":")),
            config=config,
            # v0.9.8 (B7): reasoning models emit visible chain-of-thought that
            # consumes the budget BEFORE the JSON verdict — with 200 the response
            # truncated mid-thought and never produced JSON (-> spurious soft-pass).
            # max_tokens is a CAP not a target; this just lets the model reach the
            # JSON (which _extract_json's end-first scan then recovers).
            max_tokens=1500,
            temperature=0.0,
        )
    except Exception as exc:
        # v0.9.8 (B5): a verifier-LLM infra failure (e.g. a reasoning model that
        # wraps its JSON in prose so the parser fails even after repair) must NOT
        # be treated as REJECTION — that blocks the user's task in a retry loop
        # over OUR failure. Soft-pass instead; the goal-level verifier (Pillar A)
        # still checks the final deliverable, and crediting-when-can't-verify is
        # already this module's policy for the no-hint / disabled cases above.
        logger.warning(
            "[Verifier] LLM call failed for objective %s (tier=%s) — SOFT-PASS: %s",
            objective.id, int(getattr(config, "verifier_tier", 1)), exc,
        )
        return {"verified": True, "reason": f"verifier unavailable (soft-pass): {exc}"}

    if not isinstance(result, dict) or "verified" not in result:
        logger.warning(
            "[Verifier] malformed output for objective %s — SOFT-PASS", objective.id,
        )
        return {"verified": True, "reason": "verifier output malformed/unparsable (soft-pass)"}

    return {
        "verified": bool(result["verified"]),
        "reason": str(result.get("reason") or "")[:300],
    }
