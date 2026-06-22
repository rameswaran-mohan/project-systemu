"""v0.9.5 L6 mixture_of_agents — peer-review fork-N-agents for high-stakes verification.

The peer-review fork pattern: spawn N Tier-1 LLM calls with diverse lenses,
aggregate verdicts, return majority. When the verifier needs higher confidence
than a single judgment, this tool delivers it.

Self-doubt-driven: the LLM uses this tool BEFORE claiming completion on
expensive or irreversible decisions — paying the cost of N calls when
the cost of being wrong is higher.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from systemu.core.llm_router import llm_call_json
from systemu.runtime.tool_registry_v2 import registry

logger = logging.getLogger(__name__)

_DEFAULT_LENSES = ("correctness", "completeness", "edge-cases")


def mixture_of_agents(
    *,
    query: str,
    config,
    n_peers: int = 3,
    lenses: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Spawn ``n_peers`` Tier-1 LLM calls in sequence with diverse lenses,
    aggregate yes/no verdicts, return the majority decision.

    Conservative tie-break: ties resolve to "no" (don't credit on doubt).
    LLM exceptions count as "no" (conservative).

    Returns:
        {
          "majority_verdict": "yes" | "no",
          "yes_count": int,
          "no_count": int,
          "per_peer_verdicts": List[{"lens": str, "verdict": str, "reason": str}],
        }
    """
    lenses_list = list(lenses or _DEFAULT_LENSES)
    # Pad or truncate lenses to match n_peers
    while len(lenses_list) < n_peers:
        lenses_list.append(_DEFAULT_LENSES[len(lenses_list) % len(_DEFAULT_LENSES)])
    lenses_list = lenses_list[:n_peers]

    system_prompt = (
        "You are a peer reviewer. You have NO conversation history. "
        "Judge the question through ONE specific lens. "
        "Return strict JSON: {\"verdict\": \"yes\" | \"no\", \"reason\": \"<one sentence>\"}. "
        "Be conservative — when uncertain, prefer \"no\"."
    )

    per_peer: List[Dict[str, Any]] = []
    for lens in lenses_list:
        user_payload = f"Lens: {lens}\n\nQuestion: {query}"
        try:
            result = llm_call_json(
                tier=1,
                system=system_prompt,
                user=user_payload,
                config=config,
                max_tokens=150,
                temperature=0.0,
            )
        except Exception as exc:
            logger.warning("[MoA] peer (lens=%s) failed: %s", lens, exc)
            result = {"verdict": "no", "reason": f"peer failed: {exc}"}
        if not isinstance(result, dict):
            result = {"verdict": "no", "reason": "peer returned non-dict"}
        verdict = str(result.get("verdict", "no")).lower().strip()
        if verdict not in ("yes", "no"):
            verdict = "no"
        per_peer.append({
            "lens": lens,
            "verdict": verdict,
            "reason": str(result.get("reason", ""))[:200],
        })

    yes_count = sum(1 for p in per_peer if p["verdict"] == "yes")
    no_count = sum(1 for p in per_peer if p["verdict"] == "no")
    # Conservative tie-break — never credit on doubt
    majority = "yes" if yes_count > no_count else "no"

    return {
        "majority_verdict": majority,
        "yes_count": yes_count,
        "no_count": no_count,
        "per_peer_verdicts": per_peer,
    }


# ── Tool registration ────────────────────────────────────────────────

_MOA_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "The question or claim to verify."},
        "n_peers": {"type": "integer", "description": "Number of peer reviewers (default 3).", "default": 3},
        "lenses": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Distinct review lenses (e.g. ['correctness','security','completeness']).",
        },
    },
    "required": ["query"],
}


def _moa_handler(**kwargs) -> Dict[str, Any]:
    from sharing_on.config import Config
    cfg = Config.from_env()
    query = kwargs.get("query", "")
    n_peers = int(kwargs.get("n_peers", 3))
    lenses = kwargs.get("lenses")
    try:
        result = mixture_of_agents(
            query=query, config=cfg, n_peers=n_peers, lenses=lenses,
        )
        return {"success": True, **result}
    except Exception as exc:
        return {"success": False, "error": f"{type(exc).__name__}: {exc}"}


registry.register(
    name="mixture_of_agents", toolset="verification",
    schema=_MOA_SCHEMA, handler=_moa_handler,
    description=(
        "Spawn N Tier-1 LLM peer reviewers with diverse lenses, aggregate "
        "yes/no verdicts, return majority. Use BEFORE claiming completion "
        "on expensive or irreversible decisions when self-doubt is appropriate."
    ),
    is_action_tool=False,
    max_result_size_chars=10_000,
)
