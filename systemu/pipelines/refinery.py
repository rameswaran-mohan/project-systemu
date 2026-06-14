"""Pipeline F — The Refinery.

Processes completed or failed Shadow executions from the ShadowRuntime.
Evaluates the ExecutionContext history using Tier 1 reasoning and routes
to appropriate feedback loops:
 - Success (Routine)
 - Success (Novel Skill Refinement)
 - Success (Evolution Proposal)
 - Failure (Scroll Refinement)
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from sharing_on.config import Config
from systemu.core.llm_router import llm_call_json
from systemu.core.models import (
    Evolution,
    EvolutionStatus,
    EvolutionType,
    Scroll,
    Shadow,
    Tool,
)
from systemu.core.utils import generate_id, load_prompt, utcnow
from systemu.runtime.context_builder import ExecutionContext
from systemu.vault.vault import Vault

logger = logging.getLogger(__name__)

# [A.4] Execution-ID keyed dedup — prevents double-appraisal if the refinery is
# invoked twice for the same execution (e.g. retry mis-fire, parallel dispatch).
# Capped at _DEDUP_MAX to avoid unbounded memory growth over a long process lifetime.
_DEDUP_MAX         = 1000
_processed_ids:    set[str]  = set()
_processed_order:  List[str] = []   # FIFO eviction queue


def _register_execution(key: str) -> bool:
    """Register *key* as processed.  Returns False if it was already registered."""
    global _processed_ids, _processed_order
    if key in _processed_ids:
        return False
    _processed_ids.add(key)
    _processed_order.append(key)
    if len(_processed_order) > _DEDUP_MAX:
        evict = _processed_order.pop(0)
        _processed_ids.discard(evict)
    return True


_TOOLISH = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")   # snake_case tool-name shape


def _available_tools(vault) -> List[Dict[str, str]]:
    """Enabled + deployed tools as [{name, description}] — the appraiser's ground
    truth so it stops recommending tools that don't exist (root cause X)."""
    out: List[Dict[str, str]] = []
    try:
        for h in (vault.load_index("tools") or []):
            if h.get("enabled") and str(h.get("status", "")).lower() in ("deployed", "upgraded"):
                out.append({"name": h.get("name", ""),
                            "description": (h.get("description") or "")[:140]})
    except Exception:
        logger.debug("[Refinery] available-tools load failed", exc_info=True)
    return out


def _validate_feedback_tools(feedback: str, known: set) -> str:
    """Flag (never delete) snake_case tokens that look like tool names but aren't
    in the registry, so a hallucinated recommendation can't be injected into a
    scroll hint unchallenged. Grounding is the primary fix; this is the net."""
    def _mark(m):
        tok = m.group(0)
        return tok if tok in known else f"{tok} (not an available tool)"
    return _TOOLISH.sub(_mark, feedback or "")


def process_execution_result(
    shadow: Shadow,
    scroll: Scroll,
    execution_result: Dict[str, Any],
    context: ExecutionContext,
    config: Config,
    vault: Vault,
) -> None:
    """Analyze the result of an execution run and route to feedback systems."""
    exec_key = f"{shadow.id}:{context.execution_id}"
    if not _register_execution(exec_key):
        logger.info(
            "[Refinery] Skipping duplicate invocation for execution %s — already processed",
            context.execution_id,
        )
        return

    logger.info(
        "[Refinery] Appraising execution %s (Status: %s)",
        context.execution_id,
        execution_result.get("status"),
    )

    # 1. Gather context
    history_json = context.get_full_history()
    # Truncate history if massive (Tier 1 can handle big contexts, but play it safe)
    if len(history_json) > 100:
        history_json = history_json[-100:]

    # Include both legacy ActionBlocks and modern Objectives so the appraisal
    # prompt can reason over whichever format this scroll uses.
    payload = {
        "status": execution_result.get("status", "unknown"),
        "final_summary": execution_result.get("summary", ""),
        "scroll_action_blocks": [ab.model_dump(mode="json") for ab in scroll.action_blocks],
        "scroll_objectives": [obj.model_dump(mode="json") for obj in scroll.objectives],
        "execution_log": history_json,
        # Fix 3: ground the appraiser in the REAL tool registry so its feedback
        # only ever names tools that exist (root cause X).
        "available_tools": _available_tools(vault),
    }

    # 2. Tier 1 Appraisal
    try:
        appraisal = llm_call_json(
            tier=1,
            system=load_prompt("appraise_execution.md"),
            user=json.dumps(payload),
            config=config,
            temperature=0.2,
            max_tokens=2048,
        )
    except Exception as exc:
        logger.error("[Refinery] Tier 1 appraisal failed: %s", exc)
        return

    outcome = appraisal.get("outcome", "routine")
    logger.info("[Refinery] Outcome determined: %s", outcome)

    # 3. Route Output
    if outcome == "routine":
        logger.debug("[Refinery] Routine explicit logic processing — no action needed.")

    elif outcome == "scroll_refinement":
        _handle_scroll_refinement(appraisal, scroll, vault)
        # Fix 4 (root cause Y): a STRUCTURAL failure means the activity's
        # tool/skill mapping is broken (a required tool persistently failed) — the
        # refined hint alone never changes the frozen mapping because the
        # extractor's idempotency guard returns the existing activity. Put the
        # scroll back into the re-extractable state (clear activity_id + APPROVED)
        # so the NEXT extraction (recovery-sweep Pass 1 / re-approval) recomputes
        # the mapping with the refined hints. recovery_attempts is owned by the
        # sweep (bounds re-extraction) — do NOT touch it here. Transient failures
        # leave the activity intact for the supervisor retry path.
        if execution_result.get("structural_failure") and getattr(scroll, "activity_id", None):
            try:
                from systemu.core.models import ScrollStatus
                scroll.activity_id = None
                scroll.status = ScrollStatus.APPROVED
                scroll.updated_at = utcnow()
                vault.save_scroll(scroll)
                logger.info(
                    "[Refinery] Scroll %s structurally failed — cleared activity "
                    "for re-extraction (mapping will be recomputed).", scroll.id)
            except Exception:
                logger.warning("[Refinery] re-extract reset failed for scroll %s",
                               getattr(scroll, "id", "?"), exc_info=True)

    elif outcome == "propose_evolution":
        _handle_evolution(appraisal, vault)

    elif outcome == "refine_new_skill":
        _handle_new_skill_refinement(appraisal, shadow, vault, config, scroll=scroll)

    else:
        logger.warning("[Refinery] Unknown outcome type: %s", outcome)

    # Memory extraction runs on every appraisal regardless of outcome — a routine
    # success can still teach the shadow something. Failures contain the most
    # signal, so we never short-circuit this.
    try:
        _extract_memory_candidates(shadow, scroll, execution_result, history_json, config, vault)
    except Exception as exc:
        logger.warning("[Refinery] Memory extraction failed (non-fatal): %s", exc)

    # ── Auto-consolidate if buffer crossed the threshold ──────────────────────
    # Keeps memory current within N executions rather than waiting for the
    # daily cron. The cron remains as a staleness safety net for low-frequency
    # shadows that never accumulate enough lessons to hit the threshold.
    try:
        _maybe_consolidate_buffer(shadow, config, vault)
    except Exception as exc:
        logger.warning("[Refinery] Auto-consolidation failed (non-fatal): %s", exc)


def _handle_scroll_refinement(appraisal: Dict[str, Any], scroll: Scroll, vault: Vault) -> None:
    """Inject failure feedback into the Scroll to prevent repeating mistakes.

    Supports both scroll formats:
      - Legacy: ActionBlocks (step_number-indexed) → feedback appended to expected_outcome
      - Modern: Objectives (id-indexed) → feedback appended to objective.hints["feedback"]
    """
    index = appraisal.get("failed_action_block_index")
    feedback = appraisal.get("feedback")

    if not isinstance(index, int) or not feedback:
        logger.warning("[Refinery] Missing refinement data in appraisal payload")
        return

    # Fix 3: a refined hint must not recommend tools that don't exist. Flag any
    # tool-shaped token in the feedback that isn't in the live registry before
    # it is injected into the scroll (defense-in-depth behind the grounded prompt).
    feedback = _validate_feedback_tools(
        feedback, {t["name"] for t in _available_tools(vault)})

    from datetime import datetime as _dt

    # Modern Objectives format
    if scroll.objectives:
        matching_obj = next((obj for obj in scroll.objectives if obj.id == index), None)
        if matching_obj:
            existing = matching_obj.hints.get("feedback", "")
            sep = "\n" if existing else ""
            matching_obj.hints["feedback"] = (
                existing + sep + f"PRIOR FAILURE: {feedback}"
            )
            scroll.updated_at = _dt.utcnow()
            vault.save_scroll(scroll)
            logger.info(
                "[Refinery] Scroll %s objective %d refined with feedback hint.", scroll.id, index
            )
        else:
            logger.warning(
                "[Refinery] Could not find Objective id=%d to refine (scroll %s has %d objectives).",
                index, scroll.id, len(scroll.objectives),
            )
        return

    # Legacy ActionBlocks format
    matching_block = next((ab for ab in scroll.action_blocks if ab.step_number == index), None)
    if matching_block:
        feedback_note = f"\n\nCRITICAL FEEDBACK FROM PRIOR FAILURE: {feedback}"
        matching_block.expected_outcome = matching_block.expected_outcome + feedback_note
        scroll.updated_at = _dt.utcnow()
        vault.save_scroll(scroll)
        logger.info(
            "[Refinery] Scroll %s refined at Block %d with feedback constraints.", scroll.id, index
        )
    else:
        logger.warning(
            "[Refinery] Could not find ActionBlock index %d to refine (scroll %s has %d blocks).",
            index, scroll.id, len(scroll.action_blocks),
        )


def _handle_evolution(appraisal: Dict[str, Any], vault: Vault) -> None:
    """Create a new pending evolution based on execution enhancement."""
    target_type = appraisal.get("target_entity_type", "skill")
    target_id = appraisal.get("target_entity_id", "")
    desc = appraisal.get("description", "")
    rationale = appraisal.get("rationale", "")

    if not desc or not target_id:
        logger.warning("[Refinery] Malformed evolution appraisal")
        return

    evolution = Evolution(
        id=generate_id("evolution"),
        evolution_type=EvolutionType.UPGRADE,
        target_entity_type=target_type,
        target_entity_ids=[target_id],
        description=f"[Auto-Appraised] {desc}",
        rationale=f"Observed during successful execution: {rationale}",
        status=EvolutionStatus.PROPOSED,
    )
    vault.save_evolution(evolution)
    logger.info("[Refinery] Proposed evolution %s for %s %s", evolution.id, target_type, target_id)


# Canonical enum lives in systemu/core/memory_types.py so the vault's
# tier-allowlist and this LLM-output validator can never drift.
from systemu.core.memory_types import SHADOW_CLAIM_TYPES as _VALID_MEMORY_CATEGORIES


def _extract_memory_candidates(
    shadow: Shadow,
    scroll: Scroll,
    execution_result: Dict[str, Any],
    history_json,
    config: Config,
    vault: Vault,
) -> None:
    """Tier-1 distillation: 0–3 lessons appended to the shadow's memory buffer.

    The buffer is the fast path; the daily consolidation job is what folds these
    into the canonical SHADOW_MEMORY.md. Keeping extraction cheap here means we
    can run it on every execution without paying full consolidation cost each time.
    """
    payload = {
        "shadow_name":        shadow.name,
        "shadow_description": shadow.description,
        "scroll_name":        scroll.name,
        "scroll_action_blocks": [ab.model_dump(mode="json") for ab in scroll.action_blocks],
        "scroll_objectives":  [obj.model_dump(mode="json") for obj in scroll.objectives],
        "execution_status":   execution_result.get("status", "unknown"),
        "final_summary":      execution_result.get("final_summary", "") or execution_result.get("summary", ""),
        "execution_log":      history_json,
    }

    response = llm_call_json(
        tier=1,
        system=load_prompt("extract_memory.md"),
        user=json.dumps(payload, default=str),
        config=config,
        temperature=0.2,
        max_tokens=1024,
    )

    lessons = response.get("lessons") or []
    if not isinstance(lessons, list):
        logger.warning("[Refinery] extract_memory returned non-list for 'lessons': %r", lessons)
        return

    from datetime import datetime
    exec_id = execution_result.get("execution_id", "")
    written = 0
    # v0.4.0-e: dedup against signature-bearing entries already in the
    # shadow's buffer.  This stops the refinery from creating duplicate
    # lessons when the supervisor (or _analyze_failure) has already
    # written the same pattern live.
    #
    # v0.6.9: also count prior occurrences per signature so we can gate
    # `failure_patterns` lessons behind an N>=3 recurrence threshold.
    # A single failed run was previously enough to write "tool fails
    # persistently" to the buffer; the next run would then read that
    # lesson and refuse to retry the tool even after the operator fixed
    # the underlying cause.  Reuse the same vault read for both.
    existing_sigs: set = set()
    sig_count: Dict[str, int] = {}
    try:
        _md_unused, _prior_entries = vault.load_shadow_memory(shadow.id)
    except Exception:
        _prior_entries = []
    for _e in (_prior_entries or []):
        if not isinstance(_e, dict):
            continue
        _s = _e.get("_pattern_signature")
        if _s:
            existing_sigs.add(_s)
            sig_count[_s] = sig_count.get(_s, 0) + 1

    # v0.6.9: failure_patterns require N>=3 corroborating executions
    # before promotion to the buffer.  Observational categories
    # (tool_quirks, heuristics, domain_glossary, self_assessment) still
    # write on first occurrence — they describe stable facts, not a
    # resolvable failure.
    FAILURE_RECURRENCE_THRESHOLD = 3

    from systemu.core.memory_types import pattern_signature as _ps
    for lesson in lessons:
        if not isinstance(lesson, dict):
            continue
        category = lesson.get("category", "")
        text     = (lesson.get("lesson") or "").strip()
        if category not in _VALID_MEMORY_CATEGORIES or not text:
            continue
        # Compute a signature so cross-shadow promotion can pick this up.
        sig = _ps(
            error_type=category,
            tool_name=lesson.get("tool_name"),
            error_message=text,
            top_keyword=lesson.get("keyword"),
        )
        # v0.6.9: failure_patterns use the N>=3 occurrence gate instead
        # of the strict 1-and-done dedup.  The supervisor and
        # _analyze_failure write raw failure observations to the buffer
        # live (one per occurrence); the refinery distills the recurring
        # pattern only once we have enough corroborating evidence.  The
        # dedup check would otherwise block the refinery from ever
        # writing its distilled lesson, because the raw observations
        # already carry the same signature.
        if category == "failure_patterns":
            occurrences_with_current = sig_count.get(sig, 0) + 1
            if occurrences_with_current < FAILURE_RECURRENCE_THRESHOLD:
                logger.debug(
                    "[Refinery] v0.6.9: deferring failure_patterns lesson "
                    "for shadow %s sig=%r — %d/%d occurrences",
                    shadow.id, sig, occurrences_with_current,
                    FAILURE_RECURRENCE_THRESHOLD,
                )
                continue
        elif sig in existing_sigs:
            # Observational categories: strict 1-and-done dedup.
            logger.debug(
                "[Refinery] skipping duplicate signature %r for shadow %s",
                sig, shadow.id,
            )
            continue
        entry = {
            "created_at":             utcnow().isoformat(),
            "exec_id":                exec_id,
            "category":               category,
            "lesson":                 text[:500],     # hard cap to bound buffer growth
            "evidence_action_blocks": lesson.get("evidence_action_blocks", []),
            "_pattern_signature":     sig,
        }
        # Use the v0.2.2 gate-keeper helper — stamps tier provenance and
        # rejects cross-tier writes per docs/memory-model.md.
        vault.append_shadow_memory_buffer(shadow.id, entry, source="refinery")
        existing_sigs.add(sig)
        # v0.6.9: include this write in subsequent threshold checks so
        # multiple lessons in the same extraction call don't double-count.
        sig_count[sig] = sig_count.get(sig, 0) + 1
        written += 1

    if written:
        logger.info("[Refinery] Buffered %d memory candidate(s) for shadow %s", written, shadow.id)


def _existing_signatures(vault, shadow_id: str) -> set:
    """Collect the set of ``_pattern_signature`` values already in the buffer.

    Used to dedupe refinery writes against entries the supervisor or
    diagnosis pipeline put there live.  Empty when the shadow has no
    signature-bearing entries yet (which is fine — first write wins).
    """
    sigs: set = set()
    try:
        _md, entries = vault.load_shadow_memory(shadow_id)
    except Exception:
        return sigs
    for e in entries:
        if not isinstance(e, dict):
            continue
        sig = e.get("_pattern_signature")
        if sig:
            sigs.add(sig)
    return sigs


def _handle_new_skill_refinement(
    appraisal: Dict[str, Any], shadow: Shadow, vault: Vault, config: Config,
    scroll: Optional[Scroll] = None,
) -> None:
    """Refine a brand new skill from the execution."""
    name = appraisal.get("new_skill_name")
    desc = appraisal.get("new_skill_description")
    tools = appraisal.get("required_tool_names", [])

    if not name or not desc:
        logger.warning("[Refinery] Skill refinement missing name or description.")
        return

    # Map tool names back to Vault Tool IDs.
    tool_indexes = vault.load_index("tools")
    mapped_tool_ids = []
    mapped_tool_names = []
    for req_tool in tools:
        t = next((t for t in tool_indexes if t["name"] == req_tool), None)
        if t:
            mapped_tool_ids.append(t["id"])
            mapped_tool_names.append(t["name"])
        else:
            mapped_tool_names.append(req_tool)

    from systemu.core.models import Skill

    # Link to the source scroll for evidence traceability
    evidence_ids = [scroll.id] if scroll and scroll.id != "stub" else []

    skill = Skill(
        id=generate_id("skill"),
        name=name,
        description=desc,
        category="refined",
        required_tool_ids=mapped_tool_ids,
        required_tool_names=mapped_tool_names,
        evidence_scroll_ids=evidence_ids,
    )

    vault.save_skill(skill)
    logger.info("[Refinery] Refined new skill %s (%s) linked to %d scroll(s)",
                skill.name, skill.id, len(evidence_ids))


def _maybe_consolidate_buffer(shadow: Shadow, config, vault: Vault) -> None:
    """Consolidate this shadow's memory buffer if it has crossed the threshold.

    Shares the same consolidation logic as the cron job via direct import from
    jobs.py. Wrapped in try/except at the call site — never raises into the
    execution path.
    """
    from systemu.scheduler.jobs import (
        BUFFER_THRESHOLD,
        _consolidate_one,
        _graduate_memory_to_skills,
    )

    md_text, buffer_entries = vault.load_shadow_memory(shadow.id)
    if len(buffer_entries) < BUFFER_THRESHOLD:
        return

    logger.info(
        "[Refinery] Buffer threshold reached (%d/%d) for shadow '%s' — auto-consolidating",
        len(buffer_entries), BUFFER_THRESHOLD, shadow.name,
    )
    new_md = _consolidate_one(shadow, md_text, buffer_entries, config)
    if not new_md or not new_md.lstrip().startswith("---"):
        logger.warning(
            "[Refinery] Auto-consolidation produced invalid output for shadow '%s' — buffer preserved",
            shadow.name,
        )
        return

    vault.save_shadow_memory(shadow.id, new_md)
    vault.clear_memory_buffer(shadow.id)
    logger.info("[Refinery] Auto-consolidated memory for shadow '%s'.", shadow.name)

    try:
        _graduate_memory_to_skills(shadow, new_md, vault)
    except Exception as exc:
        logger.warning(
            "[Refinery] Skill graduation after auto-consolidation failed for '%s': %s",
            shadow.name, exc,
        )
