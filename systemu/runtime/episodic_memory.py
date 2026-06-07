"""v0.9.2 Layer 2 — Episodic Memory.

After a chat task / scroll run completes, capture() builds a SessionSummary
via Tier-1 LLM and persists it to the vault. Future sessions can recall via
vault.search_session_summaries.
"""
from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from systemu.core.models import SessionSummary
from systemu.core.llm_router import llm_call_json

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "summarize_session.md"


def _load_system_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _new_summary_id() -> str:
    return f"session_summary_{secrets.token_hex(4)}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def capture(
    *,
    vault,
    session_id: str,
    intent: str,
    chat_result: Optional[str],
    files_produced: List[str],
    status: str,
    config,
    execution_id: Optional[str] = None,
    user_id: Optional[str] = None,
    raw_chat_id: Optional[str] = None,
    started_at: Optional[datetime] = None,
) -> Optional[SessionSummary]:
    """Tier-1 summarize a finished run and persist as SessionSummary.

    Returns the persisted SessionSummary, or None when:
    - config.episodic_memory_enabled is False
    - LLM call fails (degraded — logged at WARNING)
    - a SessionSummary already exists for this session_id (idempotent skip)
    """
    if not getattr(config, "episodic_memory_enabled", True):
        return None

    # Idempotency check
    existing = vault.query_session_summaries(limit=None)
    if any(s.session_id == session_id for s in existing):
        logger.debug("[Episodic] session_id %s already summarized; skipping", session_id)
        return None

    user_payload = {
        "intent": intent,
        "status": status,
        "chat_result": chat_result,
        "files_produced": files_produced,
    }
    try:
        result = llm_call_json(
            tier=1,
            system=_load_system_prompt(),
            user=json.dumps(user_payload, separators=(",", ":")),
            config=config,
            max_tokens=400,
            temperature=0.2,
        )
    except Exception as exc:
        logger.warning("[Episodic] summarize failed for %s: %s", session_id, exc)
        return None

    if not isinstance(result, dict):
        logger.warning("[Episodic] summarize returned non-dict: %r", type(result))
        return None

    max_chars = int(getattr(config, "episodic_summary_max_chars", 800))
    max_tags = int(getattr(config, "episodic_tags_max_count", 8))

    outcome = str(result.get("outcome_summary") or "")[:max_chars]
    facts = result.get("key_facts_learned") or []
    if not isinstance(facts, list):
        facts = []
    facts = [str(f) for f in facts][:20]

    tags = result.get("tags") or []
    if not isinstance(tags, list):
        tags = []
    tags = [str(t).lower().strip() for t in tags if str(t).strip()][:max_tags]

    summary = SessionSummary(
        id=_new_summary_id(),
        session_id=session_id,
        execution_id=execution_id,
        user_id=user_id,
        started_at=started_at or _now(),
        completed_at=_now(),
        status=status,
        intent=intent or "",
        outcome_summary=outcome,
        key_facts_learned=facts,
        files_produced=files_produced or [],
        tags=tags,
        raw_chat_id=raw_chat_id,
    )

    try:
        vault.append_session_summary(summary)
    except Exception as exc:
        logger.warning("[Episodic] persist failed for %s: %s", session_id, exc)
        return None

    logger.info("[Episodic] captured session %s — tags=%s", session_id, tags)
    return summary
