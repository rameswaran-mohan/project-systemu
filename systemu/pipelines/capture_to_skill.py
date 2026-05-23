"""v0.7.1 — Capture session → exported Anthropic Agent Skill bundle.

Thin sequencer over the existing v0.6.0 / v0.7-d pipeline:
  1. Ensure session_dir has instructions.md + session.json (else error).
  2. refine_scroll(session_dir) → Scroll (idempotent on session_id).
  3. Find a Skill that already has this scroll as evidence; else
     extract_and_process(scroll) and pick up the newly-created Skill.
  4. export_skill(...) — writes the spec-conformant bundle.

This is the strategic-wedge entry point: ``record once → portable skill``.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from sharing_on.config import Config
from systemu.pipelines.activity_extractor import extract_and_process
from systemu.pipelines.scroll_refiner import refine_scroll
from systemu.pipelines.skill_exporter import export_skill
from systemu.vault.vault import Vault

logger = logging.getLogger(__name__)


def _find_skill_for_scroll(vault: Vault, scroll_id: str) -> Optional[str]:
    """Return the first Skill.id whose evidence_scroll_ids contains scroll_id."""
    for header in (vault.list_skills() or []):
        evidence = header.get("evidence_scroll_ids") or []
        if scroll_id in evidence:
            return header["id"]
    return None


def export_skill_from_capture(
    *,
    session_dir: Path,
    target_dir: Path,
    config: Config,
    vault: Vault,
    auto_approve: bool = False,
) -> Path:
    """Export a capture session as a portable Anthropic Agent Skill bundle.

    Args:
        session_dir: Path to a completed ``sharing_on record`` directory.
                     MUST contain ``instructions.md`` + ``session.json``.
        target_dir:  Where to write ``<target>/<kebab-name>/SKILL.md``.
        config:      Live ``sharing_on.config.Config`` (Tier 1 model + creds).
        vault:       Vault instance for persistence.
        auto_approve: When True, bypass scroll PENDING_APPROVAL gate. Mirrors
                      the ``--auto-approve`` flag on ``sharing_on analyze``.
                      Does NOT bypass the tool-dep allow-list (v0.6.8-d).

    Returns:
        Path to the exported bundle directory (``target_dir/<kebab-name>``).

    Raises:
        FileNotFoundError: when session_dir is missing required files.
        RuntimeError:      when extraction completes without producing a Skill.
        KeyError:          re-raised from export_skill when the resolved
                           skill_id is no longer in the vault.
    """
    session_dir = Path(session_dir)
    instructions = session_dir / "instructions.md"
    session_json = session_dir / "session.json"
    if not instructions.exists() or not session_json.exists():
        missing = [str(p.name) for p in (instructions, session_json) if not p.exists()]
        raise FileNotFoundError(
            f"capture session_dir {session_dir} missing {', '.join(missing)} — "
            f"run `sharing_on analyze {session_dir}` first."
        )

    logger.info("[capture_to_skill] refining scroll for session %s", session_dir.name)
    scroll = refine_scroll(
        session_dir=session_dir,
        config=config,
        vault=vault,
        auto_proceed=auto_approve,
    )

    # Reuse path: skill already extracted from this scroll.
    skill_id = _find_skill_for_scroll(vault, scroll.id)
    if skill_id:
        logger.info(
            "[capture_to_skill] scroll %s already has skill %s — reusing",
            scroll.id, skill_id,
        )
    else:
        logger.info("[capture_to_skill] extracting skills from scroll %s", scroll.id)
        result: Dict[str, Any] = extract_and_process(scroll, config, vault)
        skill_ids: List[str] = result.get("skill_ids", []) if isinstance(result, dict) else []
        if not skill_ids:
            raise RuntimeError(
                f"extract_and_process produced no Skill for scroll {scroll.id} — "
                f"check capture had a recognisable goal."
            )
        skill_id = skill_ids[0]
        if len(skill_ids) > 1:
            logger.warning(
                "[capture_to_skill] scroll %s produced %d skills; exporting first (%s)",
                scroll.id, len(skill_ids), skill_id,
            )

    out = export_skill(skill_id=skill_id, target_dir=Path(target_dir), vault=vault)
    logger.info("[capture_to_skill] exported %s -> %s", skill_id, out)
    return out
