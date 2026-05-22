"""Pre-execution scroll validator (v0.4.0-c).

Catches impossible scrolls BEFORE a Shadow burns iterations on them.
Driven by a Tier-1 LLM call against the catalog of currently-available
tools and skills.  Output is a structured :class:`ValidationResult` the
caller can use to:

* Pass the scroll through to activity extraction (``satisfiable=True``)
* Surface an approval card to the operator via the v0.3.6 supervisor
  flash path (``satisfiable=False``) — operator chooses refine or abort.

The validator is **advisory** at this phase.  Callers may choose to
proceed-anyway via an explicit override; the validation result is logged
either way so we can measure precision/recall over time.

This module is opt-in: enabled when ``config.intelligent_supervisor_enabled``
is True OR when ``SYSTEMU_SCROLL_VALIDATOR=1`` is set in the env.  Off by
default during the v0.4.0 rollout, matching the rest of the supervisor
infrastructure.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from sharing_on.config import Config
    from systemu.core.models import Scroll
    from systemu.vault.vault import Vault

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Blocker:
    objective_id:  Optional[int]
    # expanded category set.
    # Legacy: no_tool | tool_not_deployed | unmeasurable | contradiction | missing_resource | other
    # New (intent-aware): intent_mismatch | data_flow_break | output_type_mismatch | outcome_mismatch
    category:      str
    explanation:   str
    suggested_fix: str


@dataclass
class ProposedRevision:
    """candidate revised objectives the validator LLM emits when it
    blocks a scroll.  Powers the side-by-side operator card on /scrolls."""
    objectives: List[Dict[str, Any]] = field(default_factory=list)
    rationale:  str = ""


@dataclass
class ValidationResult:
    satisfiable: bool
    confidence:  str                 # high | medium | low
    blockers:    List[Blocker] = field(default_factory=list)
    summary:     str = ""
    error:       Optional[str] = None    # set when the validator itself errored
    # when satisfiable=False, the LLM may emit a candidate revision
    # the operator can one-click accept.  None when satisfiable or when the
    # LLM didn't produce one.
    proposed_revision: Optional[ProposedRevision] = None


# ─────────────────────────────────────────────────────────────────────────────
# Opt-in resolution

def is_enabled(config) -> bool:
    """Validator runs when SYSTEMU_SCROLL_VALIDATOR env var says so, OR when
    config.scroll_validator is True (defaults True), OR when
    config.intelligent_supervisor_enabled is True (legacy supervisor wiring).
    """
    env = (os.environ.get("SYSTEMU_SCROLL_VALIDATOR") or "").strip().lower()
    if env in ("1", "true", "yes"):
        return True
    if env in ("0", "false", "no"):
        return False
    # respect the new dedicated config field (defaults True).
    if bool(getattr(config, "scroll_validator", False)):
        return True
    return bool(getattr(config, "intelligent_supervisor_enabled", False))


# ─────────────────────────────────────────────────────────────────────────────
# Public API

def validate_scroll(
    scroll: "Scroll",
    *,
    config: "Config",
    vault: "Vault",
    catalog_overrides: Optional[Dict[str, Any]] = None,
) -> ValidationResult:
    """Run the pre-flight validator against ``scroll``.

    Args:
        scroll:            The Scroll under inspection.
        config:            Carries API keys + Tier-1 model name.
        vault:             Used to gather the tool/skill catalog.
        catalog_overrides: For tests — direct {"tools": [...], "skills": [...]}
                           dict overrides catalog discovery.

    Returns ``ValidationResult.satisfiable=True`` when the validator is
    disabled (caller is free to proceed), so callers can always rely on
    the result regardless of feature-flag state.
    """
    if not is_enabled(config):
        return ValidationResult(
            satisfiable=True,
            confidence="low",
            summary="validator disabled — caller proceeds without pre-flight",
        )

    try:
        catalog = catalog_overrides or _build_catalog(vault)
    except Exception as exc:
        logger.exception("[ScrollValidator] catalog build failed")
        return ValidationResult(
            satisfiable=True, confidence="low",
            summary="validator could not build catalog — proceeding (fail-open)",
            error=str(exc),
        )

    # payload now includes per-objective output_type (when present
    # on the model) and expected_outcome (new Scroll field added in Stage 2).
    # These let the LLM do explicit data-flow reasoning, not just keyword
    # capability matching.
    payload = {
        "scroll": {
            "name":             getattr(scroll, "name", ""),
            "intent":           getattr(scroll, "intent", ""),
            "expected_outcome": getattr(scroll, "expected_outcome", ""),
            "objectives": [
                {
                    "id": getattr(obj, "id", None),
                    "goal": getattr(obj, "goal", ""),
                    "success_criteria": getattr(obj, "success_criteria", ""),
                    "output_type": getattr(obj, "output_type", ""),
                }
                for obj in (getattr(scroll, "objectives", []) or [])
            ],
            "constraints": getattr(scroll, "constraints", {}) or {},
        },
        "tools_available":  catalog.get("tools", []),
        "skills_available": catalog.get("skills", []),
    }

    # Empty-objectives early exit — saves an LLM call and matches the prompt rule.
    if not payload["scroll"]["objectives"]:
        return ValidationResult(
            satisfiable=False, confidence="high",
            blockers=[Blocker(
                objective_id=None, category="other",
                explanation="scroll has no objectives",
                suggested_fix="re-refine the scroll to extract concrete objectives",
            )],
            summary="Scroll has no objectives — cannot execute.",
        )

    try:
        from systemu.core.llm_router import llm_call_json
        from systemu.core.utils import load_prompt
        raw = llm_call_json(
            tier=1,
            system=load_prompt("validate_scroll.md"),
            user=json.dumps(payload),
            config=config,
            temperature=0.1,
            # bumped from 1024 to fit the new proposed_revision
            # block.  Empirically a ~5-objective revision + blockers list
            # fits comfortably within 2048.
            max_tokens=2048,
        )
    except Exception as exc:
        logger.warning("[ScrollValidator] LLM call failed: %s", exc)
        return ValidationResult(
            satisfiable=True, confidence="low",
            summary="validator LLM call failed — proceeding (fail-open)",
            error=str(exc),
        )

    return _parse_validator_output(raw)


# ─────────────────────────────────────────────────────────────────────────────
# Internals

def _build_catalog(vault) -> Dict[str, List[Dict[str, Any]]]:
    """Build the catalog payload for the validator LLM.

    reads schema summaries directly from the index header
    (``parameters_schema_summary`` / ``return_schema_summary``) — no per-tool
    ``vault.get_tool()`` fetch.  Headers older than v0.6.1 (no schema
    summaries) fall back to empty {} dicts; those tools will gain the
    summaries automatically on next save.
    """
    tools  = vault.load_index("tools") or []
    skills = vault.load_index("skills") or []
    return {
        "tools": [
            {
                "name":        t.get("name", ""),
                "description": (t.get("description") or "")[:200],
                "status":      t.get("status", ""),
                "parameters_schema": t.get("parameters_schema_summary") or {},
                "return_schema":     t.get("return_schema_summary") or {},
            }
            for t in tools
        ],
        "skills": [
            {
                "name":        s.get("name", ""),
                "description": (s.get("description") or "")[:200],
                # v0.6.0-d.5 fields may not exist on starter-pack skills.
                "target_outcomes": s.get("target_outcomes") or [],
                "produces":        s.get("produces") or [],
            }
            for s in skills
        ],
    }


def _parse_validator_output(raw: Any) -> ValidationResult:
    if not isinstance(raw, dict):
        return ValidationResult(
            satisfiable=True, confidence="low",
            summary="validator returned non-object — proceeding (fail-open)",
            error=f"expected dict, got {type(raw).__name__}",
        )
    satisfiable = bool(raw.get("satisfiable", True))
    blockers: List[Blocker] = []
    for b in raw.get("blockers", []) or []:
        try:
            blockers.append(Blocker(
                objective_id=b.get("objective_id"),
                category=str(b.get("category", "other")),
                explanation=str(b.get("explanation", ""))[:300],
                suggested_fix=str(b.get("suggested_fix", ""))[:300],
            ))
        except Exception:
            logger.debug("[ScrollValidator] malformed blocker entry; skipping")

    # extract proposed_revision when present (only on failures).
    proposed: Optional[ProposedRevision] = None
    if not satisfiable:
        pr = raw.get("proposed_revision")
        if isinstance(pr, dict):
            try:
                objs = pr.get("objectives") or []
                if isinstance(objs, list):
                    proposed = ProposedRevision(
                        objectives=[
                            {
                                "id": o.get("id"),
                                "goal": str(o.get("goal", ""))[:400],
                                "success_criteria": str(o.get("success_criteria", ""))[:400],
                                "output_type": str(o.get("output_type", ""))[:60],
                            }
                            for o in objs if isinstance(o, dict)
                        ][:20],
                        rationale=str(pr.get("rationale", ""))[:500],
                    )
            except Exception:
                logger.debug("[ScrollValidator] malformed proposed_revision; skipping")

    return ValidationResult(
        satisfiable=satisfiable,
        confidence=str(raw.get("confidence", "medium")),
        blockers=blockers,
        summary=str(raw.get("summary", ""))[:1000],
        proposed_revision=proposed,
    )
