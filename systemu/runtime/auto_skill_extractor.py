"""v0.9.6 L7 auto-skill-extraction — Odysseus skill_extractor pattern.

After a successful run with ≥2 rounds OR ≥2 distinct tool calls, optionally
extract a candidate SKILL.md via Tier-1 LLM. Only persist if confidence ≥
``Config.auto_skill_extract_min_confidence``. Conservative — returns None
when uncertain.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from systemu.core.llm_router import llm_call_json

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "extract_skill_from_run.md"
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]+$")


def _load_system_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def extract_skill_candidate(
    *,
    intent: str,
    chat_result: Optional[str],
    n_rounds: int,
    n_tool_calls: int,
    tools_called: List[str],
    config,
) -> Optional[Dict[str, Any]]:
    """Tier-1 extract a SKILL.md candidate from a finished run.

    Returns None when:
    - config.auto_skill_extract_enabled is False
    - Neither threshold met (n_rounds<2 AND n_tool_calls<2)
    - LLM exception
    - confidence below threshold
    """
    if not getattr(config, "auto_skill_extract_enabled", True):
        return None
    if n_rounds < 2 and n_tool_calls < 2:
        return None

    payload = {
        "intent": intent,
        "outcome": chat_result,
        "n_rounds": n_rounds,
        "n_tool_calls": n_tool_calls,
        "tools_called": tools_called,
    }

    try:
        result = llm_call_json(
            tier=1,
            system=_load_system_prompt(),
            user=json.dumps(payload, separators=(",", ":")),
            config=config,
            max_tokens=600,
            temperature=0.2,
        )
    except Exception as exc:
        logger.warning("[AutoSkill] LLM failed: %s", exc)
        return None

    if not isinstance(result, dict):
        return None

    confidence = float(result.get("confidence", 0.0) or 0.0)
    min_conf = float(getattr(config, "auto_skill_extract_min_confidence", 0.6))
    if confidence < min_conf:
        logger.info(
            "[AutoSkill] rejected (confidence=%.2f < %.2f)", confidence, min_conf,
        )
        return None

    name = str(result.get("name", "")).strip().lower()
    if not _NAME_RE.match(name):
        logger.info("[AutoSkill] rejected (bad name=%r)", name)
        return None

    return {
        "name": name,
        "description": str(result.get("description", ""))[:200],
        "procedure": [str(s) for s in (result.get("procedure") or [])][:20],
        "pitfalls": [str(p) for p in (result.get("pitfalls") or [])][:10],
        "confidence": confidence,
    }


def persist_skill_candidate(candidate: Dict[str, Any], *, skills_dir: str) -> Optional[str]:
    """Write a candidate as a SKILL.md file under ``skills_dir``.

    Layout matches Hermes: ``<skills_dir>/<name>/SKILL.md``.

    Returns the path of the written SKILL.md, or None on failure.
    """
    name = candidate.get("name")
    if not name or not _NAME_RE.match(name):
        return None
    skill_dir = Path(skills_dir) / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_dir / "SKILL.md"

    body_lines = []
    body_lines.append(f"# {name}\n")
    body_lines.append("")
    body_lines.append("## When to Use\n")
    body_lines.append(candidate.get("description", "") + "\n")
    body_lines.append("")
    body_lines.append("## Procedure\n")
    for i, step in enumerate(candidate.get("procedure") or [], start=1):
        body_lines.append(f"{i}. {step}\n")
    body_lines.append("")
    pitfalls = candidate.get("pitfalls") or []
    if pitfalls:
        body_lines.append("## Pitfalls\n")
        for p in pitfalls:
            body_lines.append(f"- {p}\n")
    body_lines.append("")

    frontmatter = (
        "---\n"
        f"name: {name}\n"
        f"description: {json.dumps(candidate.get('description', ''))}\n"
        "version: 0.1.0\n"
        "metadata:\n"
        "  systemu:\n"
        f"    confidence: {candidate.get('confidence', 0.0)}\n"
        "    source: auto-extracted\n"
        "---\n"
    )

    skill_md.write_text(frontmatter + "\n".join(body_lines), encoding="utf-8")
    return str(skill_md)
