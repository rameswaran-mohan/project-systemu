"""skill_lifecycle.py — canonical deprecate / reactivate operations on Skills.

A Skill has NO lifecycle status field; "deprecation" is encoded as
``effectiveness_score`` (default 1.0; treated as deprecated below 0.5) plus an
append-only entry in ``evolution_history``.  Both the CLI
(``sharing_on skills deprecate``) and the Skills-page buttons go through
``deprecate_skill`` so the score flip, the audit history append, the
``vault.save_skill`` and the ``data/skill_deprecations.jsonl`` audit log are
never skipped — the same one-mechanism contract as ``tool_service.enable_tool``.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from systemu.vault.vault import Vault

logger = logging.getLogger(__name__)

# Valid deprecation reasons (mirrors the CLI's click.Choice).
DEPRECATE_REASONS = ("gui_codification", "outdated", "broken")


def deprecate_skill(
    skill_id: str,
    *,
    reason: str,
    reactivate: bool,
    vault: "Vault",
) -> dict:
    """Deprecate (effectiveness_score=0.0) or reactivate (=1.0) a skill.

    Flips the score, appends a ``{ts, action, reason}`` entry to the skill's
    ``evolution_history``, persists via ``vault.save_skill``, and best-effort
    appends an audit record to ``data/skill_deprecations.jsonl``.

    Returns a small summary dict ``{skill_id, name, action, effectiveness_score}``.
    Raises ``KeyError`` if the skill doesn't exist in the vault (propagated from
    ``vault.get_skill``).
    """
    skill = vault.get_skill(skill_id)  # may raise KeyError

    new_score = 1.0 if reactivate else 0.0
    action = "reactivate" if reactivate else "deprecate"
    ts = datetime.now(tz=timezone.utc).isoformat()

    skill.effectiveness_score = new_score
    if getattr(skill, "evolution_history", None) is None:
        skill.evolution_history = []
    skill.evolution_history.append({"ts": ts, "action": action, "reason": reason})
    vault.save_skill(skill)

    # Audit log — append to data/skill_deprecations.jsonl (best-effort).
    try:
        log_path = Path("data/skill_deprecations.jsonl")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "skill_id": skill_id,
                "name": getattr(skill, "name", ""),
                "action": action,
                "reason": reason,
                "ts": ts,
            }) + "\n")
    except Exception:
        logger.debug("[skill_lifecycle] audit log append skipped", exc_info=True)

    return {
        "skill_id": skill_id,
        "name": getattr(skill, "name", ""),
        "action": action,
        "effectiveness_score": new_score,
    }
