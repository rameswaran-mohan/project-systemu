"""v0.9.7 Phase 1.4a — goal-level verifier (keystone unblocker).

Judges whether the durable evidence produced by a run satisfies the RAW user
goal, re-deriving acceptance criteria at verify-time instead of judging against
the refiner's pre-baked per-objective success_criteria.

Problem the old L4 verifier had:
  The per-objective verifier (objective_verifier.py) judges each frozen
  success_criteria string that was baked BEFORE the agent reasoned.  For a
  goal like "find my city from IP and write it to a file", objective 1
  ("determine the city") is an in-memory reasoning step with no durable
  artifact.  The old verifier rejected it even though the real deliverable
  (the written file) was achievable and was produced.

Fix:
  verify_goal() judges the GOAL — the raw user request — not the sub-objective.
  It makes a fresh-context Tier-1 LLM call that re-derives acceptance criteria
  from the goal at verify-time and checks them against StateDelta evidence.

Config gate:
  SYSTEMU_GOAL_VERIFIER_ENABLED (default True).  When disabled, the old
  per-objective verifier path still runs as the sole guard.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from systemu.runtime.state_delta import StateDelta
from systemu.core.llm_router import llm_call_json

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "verify_goal.md"


def _load_system_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _goal_verifier_enabled(config) -> bool:
    """Return True unless the operator has explicitly disabled the goal verifier.

    Resolution order (highest wins):
      1. config.goal_verifier_enabled attribute (set by tests / runtime).
      2. SYSTEMU_GOAL_VERIFIER_ENABLED env var.
      3. Default: True.
    """
    if hasattr(config, "goal_verifier_enabled"):
        return bool(config.goal_verifier_enabled)
    return os.getenv("SYSTEMU_GOAL_VERIFIER_ENABLED", "true").lower() != "false"


def _compact_delta(delta: StateDelta) -> Dict[str, Any]:
    """Build a compact, JSON-serialisable view of the StateDelta for the LLM.

    We don't need to send raw file content — the file list + sizes + previews
    (already bounded by state_delta_file_preview_chars) are enough for the
    verifier to judge durable artifact presence.
    """
    return {
        "files_added": delta.files_added,
        "files_modified": delta.files_modified,
        "audit_entries_added": delta.audit_entries_added,
        "vault_records_added": delta.vault_records_added,
        "chat_result_set": delta.chat_result_set,
    }


def verify_goal(
    *,
    goal: str,
    delta: StateDelta,
    config,
    chat_result: Optional[str] = None,
    prior_criteria: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Judge whether ``delta`` + ``chat_result`` prove the raw ``goal`` is met.

    Parameters
    ----------
    goal:
        The raw, verbatim user request string.  This is the authoritative bar;
        the refiner's restatement and ``prior_criteria`` are hints only.
    delta:
        A StateDelta capturing all durable state changes produced during the run.
    config:
        Runtime config object (used for verifier_tier, goal_verifier_enabled).
    chat_result:
        The agent's final chat reply (may be None).  For purely-informational
        goals this can satisfy the goal without any file artifact.
    prior_criteria:
        The refiner's pre-baked per-objective success criteria (HINTS ONLY).
        Passed to the LLM for context but never used as the acceptance bar.

    Returns
    -------
    dict with keys:
        verified        bool   — True iff the goal is provably met.
        reason          str    — One-sentence explanation.
        derived_criteria list  — Acceptance criteria the LLM derived from the goal.

    Never raises — on any error returns verified=False (fail-safe, never a
    false pass).
    """
    # ── Config gate ──────────────────────────────────────────────────────────
    if not _goal_verifier_enabled(config):
        return {
            "verified": True,
            "reason": "goal verifier disabled by config",
            "derived_criteria": [],
        }

    # ── Build user payload — goal is the authoritative bar ───────────────────
    user_payload: Dict[str, Any] = {
        "goal": goal,
        "state_delta": _compact_delta(delta),
        "chat_result": chat_result,
        "prior_criteria": prior_criteria or [],
    }

    # ── Fresh-context Tier-1 LLM call ─────────────────────────────────────────
    tier = int(getattr(config, "verifier_tier", 1))
    try:
        result = llm_call_json(
            tier=tier,
            system=_load_system_prompt(),
            user=json.dumps(user_payload, separators=(",", ":")),
            config=config,
            # v0.9.8 (B7): reasoning models consume a 300-token budget on visible
            # chain-of-thought before emitting the JSON verdict — with 300 the
            # response truncated mid-thought and goal verification always failed.
            max_tokens=1500,
            temperature=0.0,
        )
    except Exception as exc:
        logger.warning(
            "[GoalVerifier] LLM call failed (tier=%s): %s",
            tier, exc,
        )
        return {
            "verified": False,
            "reason": f"verifier error: {exc}",
            "derived_criteria": [],
        }

    # ── Validate response shape ───────────────────────────────────────────────
    if not isinstance(result, dict) or "verified" not in result:
        logger.warning(
            "[GoalVerifier] Malformed LLM response (missing 'verified'): %r",
            result,
        )
        return {
            "verified": False,
            "reason": "goal verifier output malformed/unparsable",
            "derived_criteria": [],
        }

    derived = result.get("derived_criteria")
    if not isinstance(derived, list):
        derived = []

    return {
        "verified": bool(result["verified"]),
        "reason": str(result.get("reason") or "")[:400],
        "derived_criteria": [str(c) for c in derived],
    }
