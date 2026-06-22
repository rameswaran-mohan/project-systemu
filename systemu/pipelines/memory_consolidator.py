"""Memory Consolidator — integrates buffer entries into bounded memory files.

Two entry points:
  consolidate_shadow_memory(shadow_id, vault, config)
      — called when a shadow's buffer reaches ≥ 5 entries (or on demand).
      — reads SHADOW_MEMORY.md + memory_buffer.jsonl, calls Tier 1,
        writes new SHADOW_MEMORY.md atomically, clears buffer.

  consolidate_global_memory(vault, config)
      — called nightly via the scheduler or on demand.
      — reads GLOBAL_MEMORY.md + elder/memory_buffer.jsonl, calls Tier 1,
        writes new GLOBAL_MEMORY.md atomically, clears global buffer.

The LLM prompt (consolidate_memory.md / consolidate_global_memory.md) outputs
raw markdown — not JSON.  We use llm_call directly (not llm_call_json) and
treat the response content as the new file text.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from typing import Any, Dict, List

from sharing_on.config import Config
from systemu.core.llm_router import _run_coroutine, llm_call
from systemu.core.utils import load_prompt, utcnow
from systemu.vault.vault import Vault

logger = logging.getLogger(__name__)

# Trigger threshold: auto-consolidate when buffer reaches this many entries
SHADOW_BUFFER_THRESHOLD = 5

# [A.4] Per-shadow advisory locks — prevent concurrent consolidation for the same shadow.
# A new lock is created on first access and kept for the process lifetime.
_shadow_locks: Dict[str, threading.Lock] = {}
_shadow_locks_meta = threading.Lock()


def _get_shadow_lock(shadow_id: str) -> threading.Lock:
    with _shadow_locks_meta:
        if shadow_id not in _shadow_locks:
            _shadow_locks[shadow_id] = threading.Lock()
        return _shadow_locks[shadow_id]


def consolidate_shadow_memory(shadow_id: str, vault: Vault, config: Config) -> bool:
    """Consolidate a shadow's memory buffer into SHADOW_MEMORY.md.

    Returns True if consolidation was performed, False if skipped.
    """
    lock = _get_shadow_lock(shadow_id)
    if not lock.acquire(blocking=False):
        logger.info(
            "[Consolidator] Shadow %s consolidation already in progress — skipping concurrent call",
            shadow_id,
        )
        return False

    try:
        return _consolidate_shadow_memory_locked(shadow_id, vault, config)
    finally:
        lock.release()


def _consolidate_shadow_memory_locked(shadow_id: str, vault: Vault, config: Config) -> bool:
    """Internal: runs with the per-shadow lock already held."""
    md_text, buffer = vault.load_shadow_memory(shadow_id)
    if not buffer:
        logger.debug("[Consolidator] Shadow %s buffer empty — skipping", shadow_id)
        return False

    try:
        shadow = vault.get_shadow(shadow_id)
    except KeyError:
        logger.error("[Consolidator] Shadow not found: %s", shadow_id)
        return False

    logger.info("[Consolidator] Consolidating shadow '%s' (%d buffer entries) ...",
                shadow.name, len(buffer))

    try:
        resp = _run_coroutine(llm_call(
            tier=1,
            system=load_prompt("consolidate_memory.md"),
            user=json.dumps({
                "shadow_id":          shadow_id,
                "shadow_name":        shadow.name,
                "current_memory_md":  md_text,
                "buffer_entries":     buffer,
                "today":              utcnow().date().isoformat(),
            }),
            config=config,
            temperature=0.1,
            max_tokens=4000,
        ))
    except Exception as exc:
        logger.error("[Consolidator] LLM call failed for shadow %s: %s", shadow_id, exc)
        return False

    new_md = resp.get("content", "")
    if not new_md or "---" not in new_md[:40]:
        logger.warning(
            "[Consolidator] Unexpected output format for shadow %s — not saved "
            "(expected frontmatter starting with '---')", shadow_id,
        )
        return False

    vault.save_shadow_memory(shadow_id, new_md)
    vault.clear_memory_buffer(shadow_id)
    logger.info("[Consolidator] Shadow '%s' memory consolidated (%d entries merged)",
                shadow.name, len(buffer))
    return True


def consolidate_global_memory(vault: Vault, config: Config) -> bool:
    """Consolidate the global memory buffer into GLOBAL_MEMORY.md.

    Returns True if consolidation was performed, False if skipped.
    """
    current_md = vault.load_global_memory()
    buffer     = vault.load_elder_memory_buffer()

    if not buffer:
        logger.debug("[Consolidator] Global memory buffer empty — skipping")
        return False

    logger.info("[Consolidator] Consolidating global memory (%d buffer entries) ...", len(buffer))

    try:
        resp = _run_coroutine(llm_call(
            tier=1,
            system=load_prompt("consolidate_global_memory.md"),
            user=json.dumps({
                "current_memory_md": current_md,
                "buffer_entries":    buffer,
                "today":             utcnow().date().isoformat(),
            }),
            config=config,
            temperature=0.1,
            max_tokens=3000,
        ))
    except Exception as exc:
        logger.error("[Consolidator] Global memory LLM call failed: %s", exc)
        return False

    new_md = resp.get("content", "")
    if not new_md or "---" not in new_md[:40]:
        logger.warning(
            "[Consolidator] Unexpected output format for global memory — not saved"
        )
        return False

    vault.save_global_memory(new_md)
    vault.clear_global_memory_buffer()
    logger.info("[Consolidator] Global memory consolidated (%d entries merged)", len(buffer))
    return True


def maybe_consolidate_shadow(shadow_id: str, vault: Vault, config: Config) -> None:
    """Consolidate only if the buffer has reached the threshold — safe to call always."""
    _, buffer = vault.load_shadow_memory(shadow_id)
    if len(buffer) >= SHADOW_BUFFER_THRESHOLD:
        consolidate_shadow_memory(shadow_id, vault, config)
