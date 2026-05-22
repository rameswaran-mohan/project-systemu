"""Skill validator (v0.6.0-d.5).

Tier-1 LLM call that validates a newly-authored or re-authored Skill
against its declared intent contract (``target_outcomes`` + ``produces``)
and its citing-scroll evidence.  Catches skills that institutionalise GUI
workflows before they get cached in the catalog and reproduce the wrong
approach for every future scroll that matches them.

Concrete failure mode this stage prevents: the starter-pack skill
``weather_report_creation`` whose ``instructions_md`` codifies
"web_screenshot → temp file → create_word_doc" — every future scroll
that name-matches it reproduces the same wrong approach.

Best-effort throughout — when the LLM is unavailable, returns a low-confidence
"valid" result so callers don't block on validator outages.  The validator
is **advisory** in this phase; callers may proceed-anyway via explicit
override, and the result is logged either way.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from sharing_on.config import Config
    from systemu.core.models import Skill
    from systemu.vault.vault import Vault

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SkillBlocker:
    category:      str   # gui_codification | outcome_mismatch | produces_mismatch | evidence_mismatch | over_or_under_specialized | missing_contract | other
    explanation:   str
    suggested_fix: str


@dataclass
class SkillValidationResult:
    valid:      bool
    confidence: str                          # high | medium | low
    blockers:   List[SkillBlocker] = field(default_factory=list)
    summary:    str = ""
    error:      Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# Opt-in resolution

def is_enabled(config) -> bool:
    """Skill validator runs when the supervisor master switch is on OR
    explicit ``SYSTEMU_SKILL_VALIDATOR`` env var."""
    env = (os.environ.get("SYSTEMU_SKILL_VALIDATOR") or "").strip().lower()
    if env in ("1", "true", "yes"):
        return True
    if env in ("0", "false", "no"):
        return False
    # Default: piggyback on the scroll validator flag so they're enabled
    # together (both are intent-aware pre-flight checks).
    sv_env = (os.environ.get("SYSTEMU_SCROLL_VALIDATOR") or "").strip().lower()
    if sv_env in ("1", "true", "yes"):
        return True
    return bool(getattr(config, "intelligent_supervisor_enabled", False))


# ─────────────────────────────────────────────────────────────────────────────
# Public API

def validate_skill(
    skill: "Skill",
    *,
    config: "Config",
    vault: "Vault",
) -> SkillValidationResult:
    """Run the validator against ``skill``.

    Returns ``valid=True`` with low confidence when the validator is
    disabled, so callers can always rely on the result regardless of
    feature-flag state.

    Fail-open on any internal error — the validator is advisory, not a
    hard gate.  When the result is ``valid=False`` the caller decides
    whether to surface an operator card or just log.
    """
    if not is_enabled(config):
        return SkillValidationResult(
            valid=True, confidence="low",
            summary="validator disabled — caller proceeds without check",
        )

    # Hard contract check: empty target_outcomes or produces is always invalid.
    # This catches Stage 3 outputs that didn't populate the new fields
    # (older prompt versions, or LLM compliance failures).
    if not (getattr(skill, "target_outcomes", None) or []):
        return SkillValidationResult(
            valid=False, confidence="high",
            blockers=[SkillBlocker(
                category="missing_contract",
                explanation="skill.target_outcomes is empty",
                suggested_fix="set target_outcomes to 1-3 intent components",
            )],
            summary="Skill missing intent contract: target_outcomes is empty.",
        )
    if not (getattr(skill, "produces", None) or []):
        return SkillValidationResult(
            valid=False, confidence="high",
            blockers=[SkillBlocker(
                category="missing_contract",
                explanation="skill.produces is empty",
                suggested_fix="set produces to 1-3 output type values",
            )],
            summary="Skill missing intent contract: produces is empty.",
        )

    # Gather citing-scroll intents (best-effort) so the LLM can cross-check.
    evidence_intents: List[str] = []
    try:
        for sid in (getattr(skill, "evidence_scroll_ids", None) or [])[:5]:
            try:
                scroll = vault.get_scroll(sid)
                if getattr(scroll, "intent", ""):
                    evidence_intents.append(scroll.intent[:200])
            except Exception:
                continue
    except Exception:
        pass

    payload = {
        "skill": {
            "name":                getattr(skill, "name", ""),
            "description":         getattr(skill, "description", ""),
            "category":            getattr(skill, "category", ""),
            "instructions_md":     (getattr(skill, "instructions_md", "") or "")[:2000],
            "target_outcomes":     list(getattr(skill, "target_outcomes", None) or []),
            "produces":            list(getattr(skill, "produces", None) or []),
            "required_tool_names": list(getattr(skill, "required_tool_names", None) or []),
        },
        "evidence_scroll_intents": evidence_intents,
    }

    try:
        from systemu.core.llm_router import llm_call_json
        from systemu.core.utils import load_prompt
        raw = llm_call_json(
            tier=1,
            system=load_prompt("validate_skill.md"),
            user=json.dumps(payload),
            config=config,
            temperature=0.1,
            max_tokens=1024,
        )
    except Exception as exc:
        logger.warning("[SkillValidator] LLM call failed: %s", exc)
        return SkillValidationResult(
            valid=True, confidence="low",
            summary="validator LLM call failed — proceeding (fail-open)",
            error=str(exc),
        )

    return _parse_output(raw)


# ─────────────────────────────────────────────────────────────────────────────
# Internals

def _parse_output(raw: Any) -> SkillValidationResult:
    if not isinstance(raw, dict):
        return SkillValidationResult(
            valid=True, confidence="low",
            summary="validator returned non-object — proceeding (fail-open)",
            error=f"expected dict, got {type(raw).__name__}",
        )
    valid = bool(raw.get("valid", True))
    blockers: List[SkillBlocker] = []
    for b in raw.get("blockers", []) or []:
        try:
            blockers.append(SkillBlocker(
                category=str(b.get("category", "other")),
                explanation=str(b.get("explanation", ""))[:300],
                suggested_fix=str(b.get("suggested_fix", ""))[:300],
            ))
        except Exception:
            logger.debug("[SkillValidator] malformed blocker entry; skipping")
    return SkillValidationResult(
        valid=valid,
        confidence=str(raw.get("confidence", "medium")),
        blockers=blockers,
        summary=str(raw.get("summary", ""))[:1000],
    )
