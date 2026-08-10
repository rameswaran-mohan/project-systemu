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


def _has_llm_provider(config) -> bool:
    """True iff at least one LLM provider is usable. Consumes THE ONE MINT.

    When NO provider is usable the Tier-1 summarize call below cannot succeed —
    it can only 401 or, keyless/offline, stall through the router's retry ladder
    (``_API_TIMEOUT_SECONDS`` × ``_NETWORK_MAX_RETRIES``, ~380s) before failing.
    ``capture`` already degrades to ``None`` on that failure, so short-circuiting
    here is behavior-equivalent — just fast. Never raises.

    F19 / DEC-43 form (i). This used to ENUMERATE FOUR CONFIG ATTRIBUTE NAMES,
    duplicated verbatim in ``open_world_planner`` — which is exactly why a grep
    for the env var could never find every re-derivation site. It also silently
    excluded the keyless provider, so an Ollama-only install had cross-session
    recall switched off with nothing said.

    ``any_provider_usable`` spends the loopback witness ONLY when a tier
    explicitly selects the keyless provider. That matters here: this runs at the
    end of every capture, and an unconditional probe would put ~1-2 s of network
    I/O on a path that has none today."""
    from systemu.runtime import provider_status as _ps
    return _ps.any_provider_usable(config)


def _warn_operator_degraded(*, session_id: str, reason: str) -> None:
    """F12 / P1 — cross-session recall is an ADVERTISED capability. When a run
    produces no summary, the task still reports SUCCESS, so a ``logger.warning``
    leaves the operator believing a feature ran that did not (DEC-34: a false
    assertion of capability). Name the feature, not just "an LLM call failed" —
    the router's own notice cannot tell the operator WHICH capability went dark.
    Best-effort: never break the end-of-run path."""
    logger.warning("[Episodic] summarize failed for %s: %s", session_id, reason)
    try:
        from systemu.interface.notifications import log_event
        # ASCII-only (DEC-32c) — this crosses into event_log.jsonl, the
        # dashboard panes and a Windows console.
        msg = (
            "Cross-session recall did not record this session: no episodic "
            f"summary was stored, so future sessions cannot recall it. Reason: {reason}"
        ).encode("ascii", "backslashreplace").decode("ascii")
        log_event(
            "WARNING", "memory", msg,
            {"kind": "episodic_degraded", "session_id": session_id,
             "reason": reason[:500]},
        )
    except Exception:
        logger.error("[Episodic] degraded notice failed to emit for %s", session_id)


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
    - no LLM provider key is configured (behavior-equivalent to a failed call —
      skips the doomed Tier-1 call instead of stalling on it; see _has_llm_provider)
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

    # No configured LLM provider → the Tier-1 summarize call cannot succeed (it
    # would 401, or — keyless/offline — stall through the router's retry ladder for
    # ~380s before failing). ``capture`` already returns None on that failure, so
    # short-circuit to the SAME degraded value now: behavior-equivalent, just fast.
    # This keeps keyless/offline production runs (and the hermetic test suite) from
    # a doomed end-of-run LLM call.
    if not _has_llm_provider(config):
        logger.debug("[Episodic] no LLM provider configured; skipping summarize for %s", session_id)
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
        _warn_operator_degraded(session_id=session_id, reason=str(exc))
        return None

    if not isinstance(result, dict):
        _warn_operator_degraded(
            session_id=session_id,
            reason=f"the summarizer returned {type(result).__name__}, not a JSON object")
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
