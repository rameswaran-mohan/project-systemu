"""v0.9.0 (Layer 1): LLM-driven extraction of durable user facts from chat.

Best-effort: never raises to the caller. The auto-extract trigger
(direct_task after a chat task resolves) calls extract_from_chat(entry,
vault, config) and continues regardless of outcome.

v0.9.1: SHA256 fingerprint short-circuit — skips the LLM call when the
chat_entry is unchanged from the last extraction (entry-fingerprinting
pattern).
"""
from __future__ import annotations

import hashlib
import json as _json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict

from systemu.core.llm_router import llm_call_json
from systemu.core.utils import load_prompt

if TYPE_CHECKING:
    from systemu.vault.vault import Vault
    from sharing_on.config import Config

logger = logging.getLogger(__name__)


def _fingerprint_entry(chat_entry: Dict[str, Any]) -> str:
    """SHA256 of canonical-JSON of the prompt+ts+status fields. Stable across runs.

    Design: entry-fingerprinting pattern (canonical-JSON hash of stable fields).
    """
    canon = _json.dumps(
        {
            "prompt": chat_entry.get("prompt"),
            "status": chat_entry.get("status"),
            "ts": chat_entry.get("ts"),
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


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

        # v0.9.1: SHA256 fingerprint short-circuit. Skip the LLM when the
        # chat_entry content is unchanged from the last extraction's fingerprint.
        # Design: entry-fingerprinting pattern (canonical-JSON hash of stable fields).
        fp = _fingerprint_entry(chat_entry)
        fp_path = Path(vault.root) / "_fact_extractor.fp"
        try:
            prev_fp = fp_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            prev_fp = ""
        if fp and fp == prev_fp:
            logger.debug("[FactExtractor] chat fingerprint unchanged; skipping LLM call")
            return 0

        user_payload = {
            "user_prompt": prompt_text,
            "status": chat_entry.get("status"),
        }
        system_prompt = load_prompt("extract_user_facts.md")
        # DEC-12: binder-class stage (advisory bind-judgment — these candidates
        # feed the profile binder). `binder_tier` decides the model; its
        # default is tier 1, so the shipped behaviour is unchanged.
        result = llm_call_json(
            stage="binder_assist",
            system=system_prompt,
            user=_json.dumps(user_payload),
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
                    # R-A16 (IMPL-5): the fact's VALUE is an LLM extraction from
                    # ``chat_entry["prompt"]`` — operator-DELIVERED text, not
                    # operator-AUTHORED. An operator who pastes an email, a log or a
                    # scraped page authored none of it; the EXTRACTOR picks which
                    # sentences become durable facts and the operator never reviews
                    # the output. Extraction INPUT is content by definition, so this
                    # is ``content_derived`` and not ``systemu_authored`` — the
                    # latter is a TRUSTED axis (``_bind_provided_params``: "systemu's
                    # own reasoning, non-content") and would still silent-bind.
                    #
                    # Absent this stamp the fact inherits the ``_fact_origin``
                    # grandfather (absent ⇒ operator) and, at the >= 0.9 confidence
                    # the extraction prompt asks for, binds SILENTLY. Stamped, it is
                    # forced into the ask_bundle as a one-click operator confirm.
                    origin_class="content_derived",
                )
                n += 1
            except Exception:
                logger.debug("[FactExtractor] could not persist fact", exc_info=True)
        if n > 0:
            logger.info("[FactExtractor] persisted %d fact(s) from chat:%s",
                        n, chat_entry.get("ts"))

        # Stamp the fingerprint after a successful LLM extraction.
        try:
            fp_path.write_text(fp, encoding="utf-8")
        except Exception:
            logger.debug("[FactExtractor] could not write fingerprint file", exc_info=True)

        return n
    except Exception:
        logger.debug("[FactExtractor] extract_from_chat swallowed error", exc_info=True)
        return 0
