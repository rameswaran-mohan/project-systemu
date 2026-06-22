"""Tool-inadequacy diagnosis (v0.5.0-c).

When the supervisor sees a tool fail repeatedly with non-recoverable
errors, it calls :func:`diagnose_tool_inadequacy` to decide:

* Is the tool actually inadequate, or is the shadow just struggling?
* If inadequate, should the response be **bump_version** (fix the tool —
  flaw affects everyone) or **fork_new_tool** (specialise — only this
  shadow's use case needs the new behaviour)?

Uses a single Tier-1 LLM call per (tool_id × execution_id), cached so
we don't spend the budget re-asking the same question.  Counted against
the supervisor's per-run LLM budget (v0.4.0-d).

Output is a :class:`InadequacyDiagnosis` consumed by v0.5.0-d's
RECALIBRATE_TOOL action.
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from sharing_on.config import Config
    from systemu.core.models import Shadow, Tool
    from systemu.vault.vault import Vault

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class InadequacyDiagnosis:
    """LLM verdict on whether a tool is structurally inadequate."""
    is_inadequate:               bool
    recalibration_mode:          str   # "bump_version" | "fork_new_tool" | "none"
    rationale:                   str
    spec_diff_summary:           str = ""
    new_tool_name_suggestion:    str = ""
    affected_shadows:            List[str] = field(default_factory=list)
    confidence:                  str = "low"   # high | medium | low


# Cache: (tool_id × execution_id) → InadequacyDiagnosis.  Per process.
_cache: Dict[str, InadequacyDiagnosis] = {}
_cache_lock = threading.Lock()


def _cache_key(tool_id: str, execution_id: str) -> str:
    return f"{tool_id}|{execution_id}"


def diagnose_tool_inadequacy(
    *,
    tool: "Tool",
    shadow: "Shadow",
    config: "Config",
    vault: "Vault",
    execution_id: str,
    failing_objective: str,
    recent_failures: List[Dict[str, Any]],
    scroll_intent: str = "",
) -> InadequacyDiagnosis:
    """Tier-1 diagnosis of whether ``tool`` is structurally inadequate.

    Cached per (tool_id × execution_id) — the first call in a run does
    the work; subsequent calls within the same execution return the
    cached verdict.

    Returns :class:`InadequacyDiagnosis` with ``is_inadequate=False`` on
    LLM error or low-quality output (fail-safe).
    """
    cache_key = _cache_key(tool.id, execution_id)
    with _cache_lock:
        cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    # v0.5.1-d: register the inadequacy flag and check for cluster signal.
    cluster_summary: Optional[Dict[str, Any]] = None
    try:
        from systemu.runtime.inadequacy_tracker import get_inadequacy_tracker
        tracker = get_inadequacy_tracker()
        tracker.flag(
            tool_id=tool.id, shadow_id=shadow.id,
            execution_id=execution_id,
            rationale=(failing_objective or "")[:200],
        )
        cluster = tracker.cluster_signal_for(tool.id)
        cluster_summary = {
            "is_cluster":        cluster.is_cluster,
            "distinct_shadows":  cluster.distinct_shadows,
            "total_flags":       cluster.total_flags,
            "sample_rationales": cluster.sample_rationales,
        }
    except Exception:
        logger.debug("[InadequacyDiagnosis] tracker hookup skipped", exc_info=True)

    # Compose the payload.  ``other_shadows_using_tool`` queried from
    # the vault — distinct shadows whose available_tool_ids include this
    # tool, with their recent execution_log success count.
    other_shadows: List[Dict[str, Any]] = []
    try:
        for sh in (vault.list_shadows() or []):
            sid = sh.get("id") if isinstance(sh, dict) else getattr(sh, "id", None)
            if not sid or sid == shadow.id:
                continue
            try:
                rec = vault.get_shadow(sid)
            except Exception:
                continue
            tools = getattr(rec, "available_tool_ids", []) or []
            if tool.id in tools:
                # Count recent successes from execution_log
                log = getattr(rec, "execution_log", []) or []
                success_count = sum(
                    1 for e in log[-20:]
                    if isinstance(e, dict) and e.get("status") == "success"
                )
                other_shadows.append({
                    "shadow_id":           sid,
                    "recent_success_count": success_count,
                })
    except Exception:
        logger.debug("[InadequacyDiagnosis] could not enumerate other shadows", exc_info=True)

    payload = {
        "tool_name":           tool.name,
        "tool_description":    tool.description,
        "current_spec": {
            "parameters_schema":    tool.parameters_schema or {},
            "return_schema":        tool.return_schema or {},
            "implementation_notes": tool.implementation_notes or "",
        },
        "shadow_id":              shadow.id,
        "shadow_description":     getattr(shadow, "description", "") or "",
        "scroll_intent":          (scroll_intent or "")[:400],
        "failing_objective":      (failing_objective or "")[:400],
        "recent_failures":        recent_failures[-3:],
        "other_shadows_using_tool": other_shadows,
        # v0.5.1-d: cluster signal — when ≥3 distinct shadows have flagged
        # this tool inadequate within the recent window, the prompt is told
        # to bias toward bump_version (universal flaw, not specialised need).
        "cluster_signal":         cluster_summary or {},
    }

    try:
        from systemu.core.llm_router import llm_call_json
        from systemu.core.utils import load_prompt
        raw = llm_call_json(
            tier=1,
            system=load_prompt("diagnose_tool_inadequacy.md"),
            user=json.dumps(payload, ensure_ascii=False),
            config=config,
            temperature=0.1,
            max_tokens=512,
        )
    except Exception as exc:
        logger.warning("[InadequacyDiagnosis] LLM call failed: %s", exc)
        # Fail-safe: don't claim a tool is inadequate when we can't tell.
        verdict = InadequacyDiagnosis(
            is_inadequate=False,
            recalibration_mode="none",
            rationale=f"diagnosis LLM error: {exc}",
            confidence="low",
        )
        with _cache_lock:
            _cache[cache_key] = verdict
        return verdict

    verdict = _parse_diagnosis(raw, fallback_affected=[s["shadow_id"] for s in other_shadows])
    with _cache_lock:
        _cache[cache_key] = verdict
    logger.info(
        "[InadequacyDiagnosis] tool=%s shadow=%s mode=%s confidence=%s",
        tool.name, shadow.id, verdict.recalibration_mode, verdict.confidence,
    )
    return verdict


def _parse_diagnosis(raw: Any, fallback_affected: List[str]) -> InadequacyDiagnosis:
    """Convert the LLM's JSON response into a typed verdict.

    Conservative: any parse error → ``is_inadequate=False``.
    """
    if not isinstance(raw, dict):
        return InadequacyDiagnosis(
            is_inadequate=False, recalibration_mode="none",
            rationale="diagnosis returned non-object", confidence="low",
        )
    mode = str(raw.get("recalibration_mode") or "").strip()
    if mode not in ("bump_version", "fork_new_tool"):
        return InadequacyDiagnosis(
            is_inadequate=False, recalibration_mode="none",
            rationale=f"diagnosis returned invalid mode {mode!r}", confidence="low",
        )
    return InadequacyDiagnosis(
        is_inadequate=True,
        recalibration_mode=mode,
        rationale=str(raw.get("rationale") or "")[:600],
        spec_diff_summary=str(raw.get("spec_diff_summary") or "")[:500],
        new_tool_name_suggestion=str(raw.get("new_tool_name_suggestion") or "")[:80],
        affected_shadows=list(raw.get("affected_shadows") or fallback_affected),
        confidence=str(raw.get("confidence") or "low").lower(),
    )


def reset_cache_for_tests() -> None:
    with _cache_lock:
        _cache.clear()
