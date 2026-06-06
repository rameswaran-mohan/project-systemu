"""v0.9.0 (Layer 1): LLM-driven extraction of durable user facts from chat.

Best-effort: never raises to the caller. The auto-extract trigger
(direct_task after a chat task resolves) calls extract_from_chat(entry,
vault, config) and continues regardless of outcome.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Dict

from systemu.core.llm_router import llm_call_json
from systemu.core.utils import load_prompt

if TYPE_CHECKING:
    from systemu.vault.vault import Vault
    from sharing_on.config import Config

logger = logging.getLogger(__name__)


def extract_from_chat(
    chat_entry: Dict[str, Any],
    vault: "Vault",
    config: "Config",
) -> int:
    """Extract candidate facts from one chat-history entry. Returns the number
    of facts persisted. Never raises."""
    try:
        prompt_text = (chat_entry.get("prompt") or "").strip()
        if not prompt_text:
            return 0
        user_payload = {
            "user_prompt": prompt_text,
            "status": chat_entry.get("status"),
        }
        system_prompt = load_prompt("extract_user_facts.md")
        result = llm_call_json(
            tier=1,
            system=system_prompt,
            user=json.dumps(user_payload),
            config=config,
            temperature=0.1,
            max_tokens=1500,
        )
        candidates = result.get("facts", []) if isinstance(result, dict) else []
        source_ref = f"chat:{chat_entry.get('ts', '')}"
        n = 0
        for c in candidates:
            if not isinstance(c, dict):
                continue
            fact_text = (c.get("fact") or "").strip()
            if not fact_text:
                continue
            tags = c.get("tags") or []
            if not isinstance(tags, list):
                tags = []
            try:
                conf = float(c.get("confidence", 0.5))
            except Exception:
                conf = 0.5
            try:
                vault.append_user_fact(
                    fact=fact_text,
                    source="auto_extract",
                    tags=[str(t) for t in tags][:3],
                    source_ref=source_ref,
                    confidence=max(0.0, min(1.0, conf)),
                )
                n += 1
            except Exception:
                logger.debug("[FactExtractor] could not persist fact", exc_info=True)
        if n > 0:
            logger.info("[FactExtractor] persisted %d fact(s) from chat:%s",
                        n, chat_entry.get("ts"))
        return n
    except Exception:
        logger.debug("[FactExtractor] extract_from_chat swallowed error", exc_info=True)
        return 0
