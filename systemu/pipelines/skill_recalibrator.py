"""Skill recalibrator (v0.6.0-d.5).

Re-authors the ``instructions_md`` body of a Skill that was implicated in
a failed or partial Shadow execution.  Mirrors v0.5.0-d's
``tool_recalibrator`` pattern but for the procedural-knowledge layer
instead of the code layer.

Trigger: ``effectiveness_score < 0.5`` AND the skill was in the shadow's
loaded resources during the failed execution.  The supervisor's
RECALIBRATE_SKILL action dispatches here.

Modes:
  * ``bump_skill``   — re-author in place (replaces ``instructions_md``
                        for everyone using this skill).  Operator approval
                        unless ``SYSTEMU_AUTO_APPROVE_LOW_RISK_SKILL_RECAL=true``
                        AND the recalibration meets all low-risk criteria.
  * ``fork_new_skill`` — create a new skill with the failing shadow's
                          ``available_skill_ids`` swapped to it.  Other
                          shadows unaffected.  Auto-approve eligible
                          when env knob set.

Always best-effort — failures fall back to operator-card flow rather than
silently dropping the recalibration.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from sharing_on.config import Config
    from systemu.core.models import Skill
    from systemu.vault.vault import Vault

logger = logging.getLogger(__name__)

# Decay constants — applied by shadow_runtime when a skill was loaded during
# a failed/partial execution.  Below RECAL_THRESHOLD, RECALIBRATE_SKILL fires.
EFFECTIVENESS_DECAY_FAILURE = 0.2
EFFECTIVENESS_DECAY_PARTIAL = 0.5
RECAL_THRESHOLD = 0.5

AUDIT_FILENAME = "skill_recalibrations.jsonl"

# Names that warrant always-operator review even when auto-approve is on.
_DESTRUCTIVE_NAME_HINTS = (
    "delete", "remove", "drop", "wipe", "purge", "destroy",
    "send_email", "publish", "post_",
)


# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SkillRecalibrationResult:
    success:              bool
    skill_id:             str
    mode:                 str   # "bump_skill" | "fork_new_skill"
    new_skill_id:         Optional[str] = None
    new_instructions_md:  str = ""
    confidence:           str = "low"
    destructive_risk:     str = "none"
    side_effects:         List[str] = field(default_factory=list)
    new_required_tools:   List[str] = field(default_factory=list)
    rationale:            str = ""
    error:                Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# Public API

def recalibrate_skill(
    skill: "Skill",
    *,
    failure_context: Dict[str, Any],
    config: "Config",
    vault: "Vault",
    mode: str = "bump_skill",
) -> SkillRecalibrationResult:
    """Run the Tier-2 re-author pass on a skill that failed.

    Returns the structured result; the caller (supervisor) decides
    whether to auto-apply or surface an operator card based on
    ``is_low_risk_skill_recalibration()``.

    Never raises — failures return ``success=False`` with error.
    """
    if mode not in ("bump_skill", "fork_new_skill"):
        return SkillRecalibrationResult(
            success=False, skill_id=getattr(skill, "id", ""),
            mode=mode, error=f"invalid mode: {mode}",
        )

    catalog = _build_tool_catalog(vault)

    payload = {
        "skill": {
            "name":                getattr(skill, "name", ""),
            "description":         getattr(skill, "description", ""),
            "target_outcomes":     list(getattr(skill, "target_outcomes", None) or []),
            "produces":            list(getattr(skill, "produces", None) or []),
            "required_tool_names": list(getattr(skill, "required_tool_names", None) or []),
            "instructions_md":     (getattr(skill, "instructions_md", "") or "")[:2000],
            "skill_version":       getattr(skill, "skill_version", 1),
            "evolution_history":   (getattr(skill, "evolution_history", None) or [])[-3:],
        },
        "current_tool_catalog": catalog,
        "failure_context":      failure_context,
    }

    try:
        from systemu.core.llm_router import llm_call_json
        from systemu.core.utils import load_prompt
        raw = llm_call_json(
            tier=2,
            system=load_prompt("recalibrate_skill.md"),
            user=json.dumps(payload, default=str),
            config=config,
            temperature=0.2,
            max_tokens=2048,
        )
    except Exception as exc:
        logger.warning("[SkillRecal] LLM call failed: %s", exc)
        return SkillRecalibrationResult(
            success=False, skill_id=getattr(skill, "id", ""),
            mode=mode, error=str(exc),
        )

    if not isinstance(raw, dict):
        return SkillRecalibrationResult(
            success=False, skill_id=getattr(skill, "id", ""),
            mode=mode, error=f"expected dict, got {type(raw).__name__}",
        )

    new_body = str(raw.get("new_instructions_md", "")).strip()
    if not new_body:
        return SkillRecalibrationResult(
            success=False, skill_id=getattr(skill, "id", ""),
            mode=mode, error="LLM returned empty new_instructions_md",
        )

    return SkillRecalibrationResult(
        success=True,
        skill_id=getattr(skill, "id", ""),
        mode=mode,
        new_instructions_md=new_body,
        confidence=str(raw.get("confidence", "low")).lower(),
        destructive_risk=str(raw.get("destructive_risk", "none")).lower(),
        side_effects=list(raw.get("side_effects_introduced") or []),
        new_required_tools=list(raw.get("new_required_tool_names") or []),
        rationale=str(raw.get("rationale", ""))[:500],
    )


def is_low_risk_skill_recalibration(
    result: SkillRecalibrationResult,
    skill: "Skill",
) -> tuple[bool, str]:
    """Conservative classifier mirroring v0.5.1-c for tools.

    Returns (eligible, reason).  All criteria must pass.  Operator opts in
    via ``SYSTEMU_AUTO_APPROVE_LOW_RISK_SKILL_RECAL`` — this function only
    answers whether the recalibration is low-risk *enough* to be auto-applied
    IF the operator has opted in.
    """
    if not result.success:
        return False, "recalibration did not succeed"
    if result.mode != "fork_new_skill":
        return False, "bump_skill always requires operator approval (modifies shared skill)"
    if result.confidence != "high":
        return False, f"confidence={result.confidence} (need 'high')"
    if result.destructive_risk != "none":
        return False, f"destructive_risk={result.destructive_risk}"
    if result.side_effects:
        return False, f"side effects introduced: {result.side_effects[:2]}"
    skill_name = (getattr(skill, "name", "") or "").lower()
    for hint in _DESTRUCTIVE_NAME_HINTS:
        if hint in skill_name:
            return False, f"skill name '{skill_name}' matches destructive heuristic '{hint}'"
    # The skill's own `produces` cannot include side_effect — that's the
    # universal "this skill touches state" marker.
    if "side_effect" in (getattr(skill, "produces", None) or []):
        return False, "skill produces side_effect — always require operator review"
    return True, "fork-mode + high confidence + no destructive risk + no side-effects"


def apply_recalibration(
    skill: "Skill",
    result: SkillRecalibrationResult,
    *,
    vault: "Vault",
    reason: str,
) -> "Skill":
    """Mutate the skill (bump) or create a forked skill (fork) and persist.

    Returns the resulting skill object.  Writes an audit row to the
    ``data/skill_recalibrations.jsonl`` log.
    """
    from systemu.core.models import Skill

    if not result.success:
        raise ValueError("cannot apply a failed recalibration")

    audit_payload: Dict[str, Any] = {
        "ts": _now_iso(),
        "original_skill_id": skill.id,
        "mode": result.mode,
        "reason": reason,
        "confidence": result.confidence,
        "destructive_risk": result.destructive_risk,
        "rationale": result.rationale,
    }

    if result.mode == "bump_skill":
        # In-place re-author.  Bump version, append evolution_history entry,
        # reset effectiveness_score.
        new_version = (getattr(skill, "skill_version", 1) or 1) + 1
        history = list(getattr(skill, "evolution_history", None) or [])
        history.append({
            "version":  new_version,
            "reason":   reason,
            "rationale": result.rationale,
            "ts":       _now_iso(),
            "prior_body_length": len(getattr(skill, "instructions_md", "") or ""),
        })
        skill.instructions_md     = result.new_instructions_md
        skill.skill_version       = new_version
        skill.evolution_history   = history
        skill.effectiveness_score = 1.0     # fresh start
        if result.new_required_tools:
            skill.required_tool_names = list(result.new_required_tools)
        vault.save_skill(skill)
        audit_payload["new_skill_id"]   = skill.id
        audit_payload["new_skill_version"] = new_version
        _audit(audit_payload)
        return skill

    # fork_new_skill: clone with a new id, new instructions, no inherited
    # evolution_history (this is a fresh branch).
    from systemu.core.utils import generate_id
    fresh_name = f"{getattr(skill, 'name', 'skill')}_v{(getattr(skill, 'skill_version', 1) or 1) + 1}"
    forked = Skill(
        id=generate_id("skill"),
        name=fresh_name,
        description=getattr(skill, "description", ""),
        category=getattr(skill, "category", "general"),
        proficiency_level=getattr(skill, "proficiency_level", "intermediate"),
        evidence_scroll_ids=[],     # fresh — no evidence yet for this fork
        required_tool_ids=[],       # vault.save_skill auto-resolves from required_tool_names below
        required_tool_names=list(result.new_required_tools or skill.required_tool_names or []),
        instructions_md=result.new_instructions_md,
        target_outcomes=list(getattr(skill, "target_outcomes", None) or []),
        produces=list(getattr(skill, "produces", None) or []),
        effectiveness_score=1.0,
        skill_version=1,
        evolution_history=[{
            "version": 1,
            "reason": reason,
            "rationale": result.rationale,
            "ts": _now_iso(),
            "forked_from": skill.id,
        }],
    )
    vault.save_skill(forked)
    audit_payload["new_skill_id"] = forked.id
    audit_payload["forked_from"]  = skill.id
    _audit(audit_payload)
    return forked


# ─────────────────────────────────────────────────────────────────────────────
# Helpers

def _build_tool_catalog(vault: "Vault") -> List[Dict[str, Any]]:
    """Snapshot of the current tool catalog (name + description + summarised
    schemas) for the recalibrator LLM.  Reuses Stage 3 helpers."""
    try:
        from systemu.pipelines.activity_extractor import _enrich_tool_for_catalog
        index = vault.load_index("tools") or []
        return [_enrich_tool_for_catalog(t, vault) for t in index]
    except Exception:
        logger.debug("[SkillRecal] could not build tool catalog", exc_info=True)
        return []


def _audit(record: Dict[str, Any], *, data_dir: Optional[Path] = None) -> None:
    """Append one row to ``data/skill_recalibrations.jsonl``."""
    target = Path(data_dir or "data") / AUDIT_FILENAME
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception:
        logger.debug("[SkillRecal] audit write failed", exc_info=True)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


# ─────────────────────────────────────────────────────────────────────────────
# Public helper used by shadow_runtime to decay scores

def decay_effectiveness(
    skill: "Skill",
    *,
    status: str,
    vault: "Vault",
) -> bool:
    """Apply the effectiveness-score decay on a skill that was loaded during
    a failed/partial execution.  Returns True when the score crossed below
    ``RECAL_THRESHOLD`` (caller should schedule RECALIBRATE_SKILL).

    Idempotent in the sense that re-running for the same execution is OK —
    callers throttle by tracking which skills they've already decayed per
    execution.
    """
    current = float(getattr(skill, "effectiveness_score", 1.0) or 1.0)
    if status == "partial":
        decay = EFFECTIVENESS_DECAY_PARTIAL
    elif status in ("failure", "failed", "error"):
        decay = EFFECTIVENESS_DECAY_FAILURE
    else:
        return False    # no decay for success / unknown

    new_score = max(0.0, current - decay)
    skill.effectiveness_score = new_score
    try:
        vault.save_skill(skill)
    except Exception:
        logger.debug("[SkillRecal] could not persist decayed score", exc_info=True)
    crossed = (current >= RECAL_THRESHOLD) and (new_score < RECAL_THRESHOLD)
    logger.info(
        "[SkillRecal] %s effectiveness %.2f → %.2f (status=%s, crossed=%s)",
        getattr(skill, "id", "?"), current, new_score, status, crossed,
    )
    return crossed
