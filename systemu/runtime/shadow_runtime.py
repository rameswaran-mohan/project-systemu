"""ShadowRuntime — the Claude Code-inspired agentic execution loop.

Orchestrates a Shadow running through its assigned Scroll:

  1. Boot with lightweight skeleton context (Progressive Loading)
  2. Enter the ReAct loop (Reason → Act → Observe → repeat)
  3. At each ActionBlock boundary → trigger Tier 3 snapshot compaction
  4. Safety gate → destructive calls require user approval
  5. Exit on COMPLETE / FAIL / max_iterations

Architecture:
  - LLM calls: Tier 2 (structured reasoning / execution decisions)
  - Snapshot compaction: Tier 3 (fast summarisation)
  - Tool execution: ToolSandbox (subprocess isolation)
  - Context management: ExecutionContext
"""

from __future__ import annotations

import datetime as _datetime_module
import json
import logging
import os
import secrets
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from sharing_on.config import Config
from systemu.core.llm_router import llm_call_json
from systemu.core.models import Activity, Shadow, Skill, Tool, ToolStatus
from systemu.core.utils import load_prompt, utcnow
from systemu.interface.notifications import confirm, notify_user, log_event
from systemu.runtime.context_builder import ExecutionContext
from systemu.runtime.tool_sandbox import ToolResult, ToolSandbox
from systemu.vault.vault import Vault

# v0.6.1-c: Stage 3.5 cure path — decay + recalibrate skill loaded during
# failed executions.  These imports are top-level (not lazy) so tests can
# monkey-patch them at module level.
from systemu.pipelines.skill_recalibrator import (
    apply_recalibration,
    decay_effectiveness,
    is_low_risk_skill_recalibration,
    recalibrate_skill,
)

# ─────────────────────────────────────────────────────────────────────────
#  v0.9.1 (Layer 4) — Durable-outcome verifier hook surface
# ─────────────────────────────────────────────────────────────────────────
from dataclasses import dataclass, field as _dataclass_field

from systemu.runtime import objective_verifier, state_delta
from systemu.runtime.loop_guard import LoopGuard


@dataclass
class ObjectiveState:
    """Per-objective verifier bookkeeping carried across iterations."""
    rejection_count: int = 0
    calls_this_turn: int = 0
    baseline: Optional[object] = None  # state_delta._Baseline


@dataclass
class CompletionOutcome:
    """Result of one process_completion_claim call."""
    credited: bool
    state: ObjectiveState
    feedback_message: Optional[str] = None
    escalate_stuck: bool = False
    bypassed_verifier: bool = False


def process_completion_claim(
    *,
    objective,
    vault,
    config,
    execution_id: str,
    default_output_dir: str,
    chat_result: Optional[str],
    state: ObjectiveState,
    fresh_work_since_last_call: bool = True,
    user_id: Optional[str] = None,
    extensions: Optional[dict] = None,
) -> CompletionOutcome:
    """Judge one completion claim. Returns the credit decision + updated state.

    - If state.calls_this_turn >= config.verifier_per_turn_cap AND no fresh
      effectful work landed → bypass verifier (claim cannot be re-judged this
      turn; runtime should keep iterating). Returns ``bypassed_verifier=True``.
    - Otherwise calls the verifier and credits/rejects accordingly.
    - On reject: increments rejection_count, returns feedback_message, and if
      rejection_count >= config.verifier_rejection_budget sets escalate_stuck.
    """
    cap = int(getattr(config, "verifier_per_turn_cap", 2))
    if state.calls_this_turn >= cap and not fresh_work_since_last_call:
        return CompletionOutcome(
            credited=False, state=state, bypassed_verifier=True,
            feedback_message=(
                "Verifier per-turn cap reached without fresh effectful work. "
                "Produce new durable evidence (write the file, send the action) "
                "before claiming completion again."
            ),
        )

    # Build the state delta against this objective's baseline.
    baseline = state.baseline or state_delta.capture_baseline(
        vault=vault, execution_id=execution_id,
        objective_id=objective.id, default_output_dir=default_output_dir,
    )
    delta = state_delta.compute_delta(
        baseline=baseline, vault=vault, default_output_dir=default_output_dir,
        chat_result=chat_result, config=config,
        execution_id=execution_id, user_id=user_id,
        extensions=extensions or {},
    )

    verdict = objective_verifier.run(objective=objective, delta=delta, config=config)
    state.calls_this_turn += 1

    if verdict["verified"]:
        # Reset rejection counter on success.
        state.rejection_count = 0
        return CompletionOutcome(credited=True, state=state)

    state.rejection_count += 1
    feedback = (
        f"Objective {objective.id} claim REJECTED. Verifier said: "
        f"{verdict['reason']}. Produce the declared evidence before claiming "
        f"completion again."
    )
    budget = int(getattr(config, "verifier_rejection_budget", 3))
    escalate = state.rejection_count >= budget
    return CompletionOutcome(
        credited=False, state=state,
        feedback_message=feedback, escalate_stuck=escalate,
    )


def recredit_on_resume(
    *,
    objective,
    vault,
    config,
    execution_id: str,
    default_output_dir: str,
    chat_result: Optional[str] = None,
    user_id: Optional[str] = None,
) -> CompletionOutcome:
    """Resume hook: judge an uncredited objective against current durable state.

    Baseline is the unix epoch — we want EVERYTHING currently present to count
    as evidence. If the verifier passes, the objective is re-credited without
    re-running its tool path.
    """
    baseline = state_delta._Baseline(iteration_start_ts="1970-01-01T00:00:00Z")
    delta = state_delta.compute_delta(
        baseline=baseline, vault=vault, default_output_dir=default_output_dir,
        chat_result=chat_result, config=config,
        execution_id=execution_id, user_id=user_id,
    )
    verdict = objective_verifier.run(objective=objective, delta=delta, config=config)
    if verdict["verified"]:
        return CompletionOutcome(credited=True, state=ObjectiveState())
    return CompletionOutcome(
        credited=False, state=ObjectiveState(rejection_count=0),
        feedback_message=verdict["reason"],
    )


# ─────────────────────────────────────────────────────────────────────────


def _resolve_verifier_output_dir(config, user_profile) -> str:
    """v0.9.1.1 hotfix: precedence for the verifier's default_output_dir.

    1. user_profile.default_output_dir (if non-empty)  — user's explicit choice
    2. config.output_dir (if non-empty)                — env-var SYSTEMU_OUTPUT_DIR
    3. {vault_dir}/outputs                             — last-resort default

    Without this, a user who runs `sharing_on user init` with a non-default
    output dir but no SYSTEMU_OUTPUT_DIR env var would see the verifier check
    ~/Documents while the LLM wrote files to the profile path → false rejection.
    """
    if user_profile is not None:
        prof_dir = getattr(user_profile, "default_output_dir", None) or ""
        if prof_dir:
            return prof_dir
    cfg_dir = getattr(config, "output_dir", None) or ""
    if cfg_dir:
        return cfg_dir
    vault_dir = getattr(config, "vault_dir", ".")
    return str(Path(vault_dir) / "outputs")


def _intent_engine_enabled(config) -> bool:
    """v0.9.7: master flag for the intent-driven engine behaviours.

    Phase 4.4 (graduated): the intent engine is now **default ON**. When on,
    COMPLETE is accepted on GOAL-level verification even if some refiner-baked
    per-objective criteria weren't individually credited; REQUEST_HARNESS /
    ASK_OPERATOR provisioning, adherence resolution, and the LLM judge are
    active. Set ``SYSTEMU_INTENT_ENGINE=false`` (or ``config.intent_engine_enabled
    = False``) to fall back to the legacy per-objective engine.
    """
    if hasattr(config, "intent_engine_enabled"):
        return bool(config.intent_engine_enabled)
    return os.getenv("SYSTEMU_INTENT_ENGINE", "true").lower() == "true"


def _intent_goal_success(*, vault, config, user_profile, scroll, execution_id,
                         summary=None) -> bool:
    """v0.9.7: goal-level acceptance from CURRENT durable evidence.

    Uses an epoch baseline so EVERYTHING present counts (sidesteps per-objective
    state-delta baseline/timing fragility). Returns True iff the goal verifier
    judges the goal met. Never raises.
    """
    try:
        from systemu.runtime import goal_verifier as _gv
        _gbaseline = state_delta._Baseline(iteration_start_ts="1970-01-01T00:00:00Z")
        _gdelta = state_delta.compute_delta(
            baseline=_gbaseline, vault=vault,
            default_output_dir=_resolve_verifier_output_dir(config, user_profile),
            chat_result=summary, config=config, execution_id=execution_id,
        )
        _gres = _gv.verify_goal(
            goal=(getattr(scroll, "raw_request", None) or getattr(scroll, "intent", "") or ""),
            delta=_gdelta, config=config, chat_result=summary,
        )
        ok = bool(_gres.get("verified"))
        logger.info(
            "[Runtime] intent-engine goal-verify: %s — %s",
            "PASS" if ok else "no-pass", str(_gres.get("reason", ""))[:160],
        )
        return ok
    except Exception:
        logger.debug("[Runtime] goal-level check errored", exc_info=True)
        return False


# ─────────────────────────────────────────────────────────────────────────
#  v0.9.3 (Layer 3) — Tool registry v2 startup discovery + whitelist resolver
# ─────────────────────────────────────────────────────────────────────────

_V2_DISCOVERED: bool = False


def _discover_v2_tools() -> None:
    """Populate the v2 tool registry singleton by AST-scanning the
    ``systemu.runtime.tools`` package for modules that call
    ``registry.register(...)`` at top level.

    Idempotent — only runs once per process. ShadowRuntime calls this at
    init so v2 tools are available without each tool module needing to be
    imported explicitly by name.
    """
    global _V2_DISCOVERED
    if _V2_DISCOVERED:
        return
    try:
        from systemu.runtime.tool_registry_v2 import registry as _v2_registry
        _v2_registry.discover_modules("systemu.runtime.tools")
        _V2_DISCOVERED = True
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning(
            "[Runtime] v2 tool discovery failed: %s", exc,
        )


def _resolve_tool_whitelist(context: str) -> set:
    """Resolve the set of tool names allowed in ``context``.

    Wraps the registry's whitelist_for_context() with a safe fallback:
    unknown contexts return an empty set rather than raising, so the
    runtime can ask about novel contexts without crashing.

    Known contexts:
      - "main"          → every registered tool
      - "verifier_fork" → read-only subset (vault.get_audit_log, file.read, ...)
      - "curator"       → skill/memory lifecycle subset
      - "fact_extractor" → write_user_fact only
      - "delegate_child" → empty (runtime composes parent_whitelist - {delegate})
    """
    from systemu.runtime.tool_registry_v2 import registry as _v2_registry
    try:
        return _v2_registry.whitelist_for_context(context)
    except ValueError:
        import logging
        logging.getLogger(__name__).debug(
            "[Runtime] unknown whitelist context %r — returning empty set",
            context,
        )
        return set()


# ─────────────────────────────────────────────────────────────────────────
#  v0.9.5 T0 — LLM-visible tool catalog builder (v1 + v2 unified)
# ─────────────────────────────────────────────────────────────────────────


def _build_llm_tool_catalog(vault=None, config=None) -> List[Dict[str, Any]]:
    """Build the LLM-visible tool catalog, combining v1 vault tools and v2
    code-registered tools.

    v2 tools whose check_fn returns False are EXCLUDED (so the LLM doesn't
    waste turns calling unavailable tools). v1 vault tools are filtered
    by the existing enabled/dry_run gates (passed in already-filtered via
    the ``vault`` arg — caller responsibility).

    For the test path (vault=None), only v2 tools are returned.

    Each entry has at minimum: name, description, parameters_schema.
    v1 entries also carry id and parameter_names (preserved for backward
    compat with the existing prompt template).
    """
    catalog: List[Dict[str, Any]] = []

    # Ensure v2 tool modules are imported before listing.
    _discover_v2_tools()

    # Resolve config for check_fn availability checks.
    _cfg = config
    if _cfg is None:
        try:
            from sharing_on.config import Config as _Config
            _cfg = _Config.from_env()
        except Exception:
            _cfg = None

    # ── v2 tools ──────────────────────────────────────────────────────────
    from systemu.runtime.tool_registry_v2 import registry as _v2

    for entry in _v2.list():
        # check_fn gating: exclude when unavailable.
        if entry.check_fn is not None:
            if not _v2.available(entry.name, _cfg):
                continue
        catalog.append({
            "name": entry.name,
            "description": entry.description or f"v2 tool: {entry.name}",
            "parameters_schema": dict(entry.schema or {}),
            "toolset": entry.toolset,
            "is_action_tool": entry.is_action_tool,
        })

    # ── v1 vault tools (preserve existing shape) ─────────────────────────
    if vault is not None:
        try:
            from systemu.core.models import ToolStatus as _ToolStatus
            v1_tools = (
                vault.list_tools(status=_ToolStatus.DEPLOYED)
                if hasattr(vault, "list_tools")
                else []
            )
        except Exception:
            v1_tools = []

        _existing_names = {e["name"] for e in catalog}
        for t in (v1_tools or []):
            if t.name in _existing_names:
                continue  # v2 wins on conflict (code-registered tools are
                           # intentional replacements of vault auto-forged stubs)
            catalog.append({
                "id": t.id,
                "name": t.name,
                "description": t.description,
                "parameter_names": list(getattr(t, "parameter_names", []) or []),
                # v0.9.7: surface the tool's REAL parameter schema so the LLM
                # emits correctly-shaped {param: value} args instead of guessing.
                # The old hardcoded {} left the model blind to v1 params → it
                # emitted bare-string args (e.g. "http://…/json/") and crashed.
                "parameters_schema": dict(getattr(t, "parameters_schema", {}) or {}),
            })

    return catalog


# ─────────────────────────────────────────────────────────────────────────
#  v0.9.2 (Layer 2) — Episodic memory capture hook
# ─────────────────────────────────────────────────────────────────────────


def _trigger_episodic_capture(
    *,
    vault,
    config,
    session_id: str,
    intent: str,
    chat_result: Optional[str],
    files_produced: list,
    status: str,
    execution_id: Optional[str] = None,
    user_id: Optional[str] = None,
    raw_chat_id: Optional[str] = None,
) -> None:
    """v0.9.2 hook: summarize+persist the finished run.

    Gated by config.summarize_after_run. Best-effort — failures degrade silently
    so a flaky LLM never blocks the user's task from completing.
    """
    if vault is None or config is None:
        return  # nothing to capture against (e.g. __new__-constructed ShadowRuntime)
    if not getattr(config, "summarize_after_run", True):
        return
    try:
        from systemu.runtime import episodic_memory
        episodic_memory.capture(
            vault=vault,
            session_id=session_id,
            intent=intent,
            chat_result=chat_result,
            files_produced=files_produced,
            status=status,
            config=config,
            execution_id=execution_id,
            user_id=user_id,
            raw_chat_id=raw_chat_id,
        )
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "[Runtime] episodic capture failed for session %s: %s",
            session_id, exc,
        )


# ─────────────────────────────────────────────────────────────────────────

# Deferred refinery dispatch
def _dispatch_refinery(shadow, scroll, result_dict, context, config, vault):
    try:
        from systemu.pipelines.refinery import process_execution_result
        process_execution_result(shadow, scroll, result_dict, context, config, vault)
    except Exception as exc:
        logger.error("[Runtime] Failed to dispatch to Refinery: %s", exc)

logger = logging.getLogger(__name__)

MAX_ITERATIONS       = 30     # Hard ceiling on agentic loop iterations
SNAPSHOT_INTERVAL    = 5      # Compact after every N completed ActionBlocks

# v0.8.17: fail-fast constants + helper for consecutive-degraded-search detection.
_SEARCH_TOOLS = {"web_search"}
_MAX_CONSEC_DEGRADED_SEARCH = 3


def _is_degraded_search_result(tool_name, parsed) -> bool:
    """True iff a search tool returned a degraded result with no usable data
    (the whole provider chain failed/empty). NOT keyed on len(results)==0 —
    engines fuzzy-match and rarely return truly zero; degraded is the real
    'search is down' signal (v0.8.17 POC finding)."""
    if tool_name not in _SEARCH_TOOLS or not isinstance(parsed, dict):
        return False
    return bool(parsed.get("degraded")) and not parsed.get("results")


# v0.8.13 (RC#3): single source of truth for "can this tool be used in a normal
# (non-dry-run) run?" — shared by ShadowRuntime._load_tools and the direct_task
# readiness gate so the loader and the gate cannot drift.
_RUNTIME_READY_STATUSES = frozenset({ToolStatus.DEPLOYED, ToolStatus.TESTED, ToolStatus.UPGRADED})


def tool_is_runtime_ready(status) -> bool:
    """True iff a tool with this status can be used in a normal (non-dry-run) run.

    Single source of truth shared by ShadowRuntime._load_tools and the
    direct_task readiness gate so they cannot drift."""
    return status in _RUNTIME_READY_STATUSES


def _gen_execution_id() -> str:
    return f"exec_{secrets.token_hex(4)}"


def _objective_items(objectives, completed) -> list:
    """v0.8.19 (R2): derive per-objective status for the live checklist.
    done = in completed; in_progress = deps satisfied but not done; else pending."""
    items = []
    for o in objectives:
        if o.id in completed:
            st = "done"
        elif all(d in completed for d in (o.depends_on or [])):
            st = "in_progress"
        else:
            st = "pending"
        items.append({"id": o.id, "goal": o.goal, "status": st})
    return items


def _objective_state_event(objectives, completed, execution_id, *, stamp) -> dict:
    """v0.8.19 (R2): build an objective_state EventBus event (stamp = origin wrapper)."""
    return stamp({
        "ts": utcnow().isoformat() + "Z",
        "level": "INFO",
        "category": "objective_state",
        "message": f"objectives {len(completed)}/{len(objectives)}",
        "context": {"execution_id": execution_id,
                    "items": _objective_items(objectives, completed)},
    })


def _stuck_thresholds() -> tuple[int, int, bool]:
    """v0.8.21: per-call read of stuck-guard env vars (live-editable via Settings)."""
    no_progress = int(os.environ.get("SYSTEMU_STUCK_NO_PROGRESS", "5") or "5")
    tool_fails  = int(os.environ.get("SYSTEMU_STUCK_TOOL_FAILS", "3") or "3")
    guard_on    = (os.environ.get("SYSTEMU_STUCK_GUARD", "on") or "on").lower() != "off"
    return (no_progress, tool_fails, guard_on)


def _build_user_context_block(vault) -> str:
    """v0.9.0 (Layer 1): compact one-block summary of the user profile + up to
    5 most-recent facts. Returns "" when no profile is set.

    The block is <= ~10 lines so it fits comfortably in a system prompt without
    risk to the token budget. Layer 2 (episodic memory) will expand this.
    """
    try:
        prof = vault.get_user_profile()
        if prof is None:
            return ""
        lines = [
            "## What you know about the user",
            f"- name: {prof.name}",
            f"- location: {prof.location_text}",
            f"- timezone: {prof.timezone}",
            f"- default_output_dir: {prof.default_output_dir}",
        ]
        facts = vault.load_user_facts(recent=5, include_superseded=False)
        if facts:
            lines.append("- facts (most recent):")
            for f in facts[-5:]:
                conf = f"{f.confidence:.2f}"
                lines.append(f"  - ({conf}) {f.fact}")
        return "\n".join(lines)
    except Exception:
        return ""


def _build_boot_memory(shadow: Any, vault: Any) -> str:
    """Build the boot-time memory block for a shadow.

    Injects full GLOBAL_MEMORY (always) + a one-line SHADOW_MEMORY header
    (lazy — shadow calls LOAD_RESOURCE memory:self to read the full file).
    """
    parts: List[str] = []

    try:
        global_md = vault.load_global_memory()
        if global_md.strip():
            parts.append(
                f"## Global Memory (cross-shadow personalisation)\n\n{global_md.rstrip()}"
            )
    except Exception as exc:
        logger.warning("[Runtime] Could not load global memory (non-fatal): %s", exc)

    try:
        shadow_md, _ = vault.load_shadow_memory(shadow.id)
        entry_count = shadow_md.count("\n- ") if shadow_md else 0
        if entry_count > 0:
            parts.append(
                f"## Specialist Memory — {entry_count} entries available. "
                f"Use `LOAD_RESOURCE resource_type=\"memory\" resource_id=\"self\"` "
                f"to consult your specialist memory if you encounter unfamiliar territory."
            )
    except Exception as exc:
        logger.warning("[Runtime] Could not load shadow memory header (non-fatal): %s", exc)

    return "\n\n".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
#  ShadowRuntime
# ─────────────────────────────────────────────────────────────────────────────

def _apply_terminate_directive(
    directive,
    *,
    context,
    shadow,
    scroll,
    execution_id: str,
    vault=None,
    origin: str = "system",
) -> None:
    """Handle a TERMINATE directive from the Intelligent Supervisor (v0.4.1-b).

    Publishes an operator approval card to the chat feed (via the v0.3.6
    redirect-card pattern) so the operator can choose one of three actions:
    Retry-with-different-shadow / Discard / Inspect.  Also records an
    entry in the affinity log so future shadow-assignment decisions can
    exclude the shadow that just gave up.

    Note: TERMINATE itself is **advisory** at this seam — the existing
    outer loop continues to process the shadow's natural FAIL/COMPLETE
    decision.  The supervisor's role is to surface the situation; the
    operator decides the recovery action via the approval card.
    """
    try:
        from systemu.runtime.affinity_log import compute_intent_hash, get_affinity_log
        intent_hash = compute_intent_hash(
            intent=getattr(scroll, "intent", ""),
            objectives=getattr(scroll, "objectives", None),
        )
        if shadow is not None:
            get_affinity_log().record_termination(
                intent_hash=intent_hash,
                shadow_id=getattr(shadow, "id", "unknown"),
                scroll_id=getattr(scroll, "id", None),
                execution_id=execution_id,
                reason="supervisor_terminate",
            )
    except Exception:
        logger.debug("[Runtime] affinity log record skipped", exc_info=True)

    # Sticky note + reflection block so the LLM still in the loop sees the
    # supervisor's verdict and can wind down cleanly with FAIL.
    context.add_sticky_note(
        f"Supervisor TERMINATEd execution: {directive.rationale[:200]}"
    )
    context.queue_reflection_block(
        "The Intelligent Supervisor has decided this execution should "
        "TERMINATE.  Wind down with a FAIL action and a short reason "
        "referring to the supervisor's diagnosis above.  Operator is "
        "being notified separately and will choose the recovery action."
    )

    # Resolve scroll/shadow names for the operator-facing card; the execution
    # id has no name so it stays as a short companion.
    from systemu.interface.name_resolver import resolve_name, short_id
    _scroll_name = (
        resolve_name(getattr(scroll, "id", ""), vault) if vault is not None and getattr(scroll, "id", "")
        else getattr(scroll, "name", "") or "this scroll"
    )
    _shadow_name = (
        resolve_name(getattr(shadow, "id", ""), vault) if vault is not None and getattr(shadow, "id", "")
        else getattr(shadow, "name", "") or "the shadow"
    )

    # Operator approval card via the v0.3.6 supervisor-flash bus.
    try:
        from datetime import datetime as _dt, timezone as _tz
        from systemu.interface.event_bus import EventBus
        bus = EventBus.get()
        bus.publish({
            "ts":       _dt.now(tz=_tz.utc).isoformat(timespec="seconds"),
            "level":    "WARNING",
            "category": "approval",
            "origin":   origin,   # v0.8.16: trigger origin threaded from execute()
            "message":  f"🛑 Supervisor TERMINATEd: {_scroll_name} · {short_id(execution_id)}",
            "context": {
                "approval_message": (
                    f"The Intelligent Supervisor has decided {_shadow_name}'s run of "
                    f"“{_scroll_name}” cannot succeed and should terminate "
                    f"(execution {short_id(execution_id)}). "
                    f"Reason: {directive.rationale or 'see audit log'}.\n\n"
                    "Choose a recovery action on the workflow detail page."
                ),
                "options":       [],
                "redirect_to":   f"/workflow/{execution_id}",
                "dedup_key":     f"supervisor-terminate:{execution_id}",
                "execution_id":  execution_id,
                "shadow_id":     getattr(shadow, "id", None),
                "scroll_id":     getattr(scroll, "id", None),
                "supervisor_rationale": directive.rationale or "",
                "actions":       ["retry_with_different_shadow", "discard", "inspect"],
            },
        })
    except Exception:
        logger.debug("[Runtime] TERMINATE approval card publish skipped", exc_info=True)


def _auto_approve_recalibration(
    *, result, vault, shadow, scroll, execution_id: str,
) -> None:
    """v0.5.1-c — bypass the operator card, enable + resume immediately.

    Used only when ``is_low_risk_recalibration()`` returned True AND the
    config flag is on.  Mirrors the operator's "Enable & Resume" click on
    the Tools page card, but happens automatically.
    """
    new_tool_id = result.new_tool_id or result.original_tool_id
    try:
        new_tool = vault.get_tool(new_tool_id)
        new_tool.enabled = True
        if (getattr(new_tool, "dry_run_status", None) or "not_run") not in ("passed", "skipped"):
            new_tool.dry_run_status = "skipped"
        vault.save_tool(new_tool)
    except Exception:
        logger.exception("[Runtime] auto-approve: tool enable failed")
        return

    try:
        from systemu.runtime.supervisor import Supervisor
        sup = Supervisor.get()
        sub_id = sup.resume_after_recalibration(
            execution_id=execution_id,
            original_tool_id=result.original_tool_id,
            new_tool_id=new_tool_id,
            mode=result.mode,
            original_shadow_id=shadow.id,
            scroll_id=getattr(scroll, "id", None) if scroll is not None else None,
        )
        logger.info(
            "[Runtime] auto-approved recalibration → resumed activity (sub=%s)",
            sub_id,
        )
    except Exception:
        logger.exception("[Runtime] auto-approve: resume failed")


def _apply_recalibrate_tool_directive(
    directive,
    *,
    context,
    shadow,
    scroll,
    execution_id: str,
    config,
    vault,
    consec_tool_fails: Dict[str, int],
    origin: str = "system",
) -> None:
    """v0.5.0-d: Handle a RECALIBRATE_TOOL supervisor directive.

    1. Identify the failing tool from ``consec_tool_fails`` (highest-fail-count tool).
    2. Run the Tier-1 inadequacy diagnosis (cached per tool × execution).
    3. If verdict says inadequate, run the recalibration pipeline (bump or fork).
    4. Publish operator approval card via v0.3.6 supervisor-flash bus.
    5. Pin a sticky note + reflection block so the LLM winds down with FAIL.

    Never raises; never directly modifies vault state other than via the
    recalibrator (which writes the new/updated tool + dry-run evidence).
    """
    if not consec_tool_fails or vault is None or shadow is None:
        logger.debug("[Runtime] RECALIBRATE_TOOL skipped — missing context")
        return

    # Pick the tool with the most consecutive fails as the candidate.
    tool_name, _fail_count = max(consec_tool_fails.items(), key=lambda kv: kv[1])
    try:
        tool = vault.find_tool_by_name(tool_name)
    except Exception:
        tool = None
    if tool is None:
        logger.debug("[Runtime] RECALIBRATE_TOOL: tool %s not in vault", tool_name)
        return

    # Pull recent failure observations from context for the diagnosis prompt.
    recent_fails: List[Dict[str, Any]] = []
    try:
        for ev in (context._history or [])[-20:]:
            if ev.event_type == "observation":
                c = ev.content
                if isinstance(c, dict) and c.get("success") is False:
                    recent_fails.append(c)
                    if len(recent_fails) >= 3:
                        break
    except Exception:
        pass

    try:
        from systemu.pipelines.tool_inadequacy_diagnosis import diagnose_tool_inadequacy
        diagnosis = diagnose_tool_inadequacy(
            tool=tool, shadow=shadow,
            config=config, vault=vault,
            execution_id=execution_id,
            failing_objective=(directive.rationale or "")[:300],
            recent_failures=recent_fails,
            scroll_intent=getattr(scroll, "intent", "") if scroll is not None else "",
        )
    except Exception:
        logger.exception("[Runtime] RECALIBRATE_TOOL diagnosis crashed")
        return

    if not diagnosis.is_inadequate:
        logger.info(
            "[Runtime] RECALIBRATE_TOOL: diagnosis says tool not inadequate (rationale=%s) — no recalibration",
            diagnosis.rationale[:120],
        )
        context.add_sticky_note(
            f"Supervisor considered recalibrating {tool.name} but diagnosis "
            f"declined: {diagnosis.rationale[:160]}"
        )
        return

    try:
        from systemu.pipelines.tool_recalibrator import (
            is_low_risk_recalibration, publish_recalibration_card, recalibrate_tool,
        )
        result = recalibrate_tool(
            tool=tool, shadow=shadow, diagnosis=diagnosis,
            failure_context=(directive.rationale or "")[:400],
            config=config, vault=vault, execution_id=execution_id,
        )

        # v0.5.1-c: auto-approve low-risk recalibrations when config allows.
        # Bypasses the operator card entirely — enables tool + resumes the
        # activity directly.  Default config has this OFF; opt-in via
        # SYSTEMU_AUTO_APPROVE_LOW_RISK_RECAL=true.
        auto_approved = False
        if (
            getattr(config, "auto_approve_low_risk_recalibrations", False)
            and result.success
        ):
            eligible, reason = is_low_risk_recalibration(
                result=result, tool=tool, diagnosis=diagnosis,
            )
            if eligible:
                logger.info(
                    "[Runtime] auto-approving low-risk recalibration: %s", reason,
                )
                _auto_approve_recalibration(
                    result=result, vault=vault,
                    shadow=shadow, scroll=scroll, execution_id=execution_id,
                )
                auto_approved = True
            else:
                logger.debug(
                    "[Runtime] auto-approve declined: %s — surfacing card", reason,
                )

        if not auto_approved:
            publish_recalibration_card(
                result=result, shadow_id=shadow.id,
                execution_id=execution_id,
                scroll_id=getattr(scroll, "id", None) if scroll is not None else None,
                origin=origin,
            )
    except Exception:
        logger.exception("[Runtime] recalibration pipeline crashed")
        return

    # v0.5.1-e: persist execution snapshot for true resume after the
    # operator approves the recalibrated tool.  When skipped (auto-
    # approve path or write failure), the resume falls back to v0.5.0's
    # fresh-restart-with-sticky behaviour.
    try:
        from systemu.runtime.execution_snapshot import (
            ExecutionSnapshot, capture_from_context, write_snapshot,
        )
        # The shadow_runtime caller stashed iteration / current_ab / completed
        # into context attributes so we can pull them here without threading
        # them through every directive helper.
        snapshot = capture_from_context(
            execution_id=execution_id,
            shadow_id=shadow.id,
            scroll_id=getattr(scroll, "id", "") if scroll is not None else "",
            iteration=int(getattr(context, "_resume_iteration", 0)),
            current_action_block=int(getattr(context, "_resume_current_ab", 1)),
            completed_objectives=getattr(context, "_resume_completed_objectives", set()),
            context=context,
            original_tool_id=tool.id,
            recalibration_dedup_key=(
                f"tool-recalibrate:{tool.id}:{execution_id}"
            ),
        )
        write_snapshot(snapshot)
    except Exception:
        logger.debug("[Runtime] snapshot capture skipped", exc_info=True)

    # Sticky note + reflection so the LLM winds down with FAIL while the
    # operator decides on the approval card.
    context.add_sticky_note(
        f"Supervisor triggered RECALIBRATE_TOOL for {tool.name} → "
        f"{result.mode}{' (fallback)' if result.forced_fallback else ''}. "
        f"Awaiting operator approval on /tools."
    )
    context.queue_reflection_block(
        "The Intelligent Supervisor has initiated tool recalibration because "
        f"{tool.name} appears structurally inadequate.  A new {result.mode} "
        f"has been forged and dry-run "
        f"{result.dry_run_status}.  Wind down with a FAIL action; the operator "
        "will resume this activity once the new tool is approved."
    )


def _maybe_decay_loaded_skills(
    context,
    *,
    vault,
    status: str,
) -> None:
    """v0.6.1-c: per-iteration hook — decay effectiveness on loaded skills
    when the current execution observed a failure / partial.

    Idempotent per (execution × skill) via ``context._decayed_skills_this_exec``.
    Crossing ``RECAL_THRESHOLD`` queues a RECALIBRATE_SKILL directive on
    ``context.pending_directives`` (consumed by ``_apply_supervisor_directives``).
    """
    loaded = getattr(context, "_loaded_skill_ids", None)
    if not loaded:
        return

    decayed = getattr(context, "_decayed_skills_this_exec", None)
    if decayed is None:
        decayed = set()
        context._decayed_skills_this_exec = decayed

    pending = getattr(context, "pending_directives", None)
    if pending is None:
        pending = []
        context.pending_directives = pending

    for skill_id in list(loaded):
        if skill_id in decayed:
            continue
        try:
            skill = vault.get_skill(skill_id)
        except Exception:
            continue
        decayed.add(skill_id)
        crossed = decay_effectiveness(skill, status=status, vault=vault)
        if crossed:
            from types import SimpleNamespace
            pending.append(SimpleNamespace(
                action="RECALIBRATE_SKILL",
                skill_id=skill_id,
            ))
            logger.info(
                "[Runtime] decay crossed threshold for skill %s — "
                "RECALIBRATE_SKILL queued",
                skill_id,
            )


def _apply_recalibrate_skill_directive(
    directive,
    *,
    context,
    vault,
    config,
    execution_id: str,
    origin: str = "system",
) -> None:
    """v0.6.1-c: dispatch RECALIBRATE_SKILL — re-author the failing skill's
    ``instructions_md`` and either auto-apply (low-risk + opt-in env knob)
    or surface a flash card on /skills.

    Mirrors ``_apply_recalibrate_tool_directive`` so the operator UX is
    consistent between the tool and skill recal flows.
    """
    skill_id = getattr(directive, "skill_id", None)
    if not skill_id:
        logger.debug("[Runtime] RECALIBRATE_SKILL missing skill_id — skipping")
        return

    try:
        skill = vault.get_skill(skill_id)
    except Exception:
        logger.debug("[Runtime] RECALIBRATE_SKILL: skill %s not in vault", skill_id)
        return

    failure_context = {
        "execution_id": execution_id,
        "status":       "failure",
        "summary":      "Effectiveness score decayed below threshold",
        "recent_failure_observations": [],
        "objective_in_flight": "",
    }

    try:
        result = recalibrate_skill(
            skill, failure_context=failure_context,
            config=config, vault=vault, mode="bump_skill",
        )
    except Exception:
        logger.exception("[Runtime] RECALIBRATE_SKILL recalibrator crashed")
        return

    if not result.success:
        logger.warning(
            "[Runtime] RECALIBRATE_SKILL did not succeed: %s", result.error,
        )
        return

    # Auto-approve gate — env knob + all conservative criteria must pass.
    auto = bool(getattr(config, "auto_approve_low_risk_skill_recalibrations", False))
    eligible, reason = is_low_risk_skill_recalibration(result, skill)
    if auto and eligible:
        try:
            apply_recalibration(
                skill, result, vault=vault,
                reason=f"auto-approved low-risk (exec={execution_id})",
            )
            logger.info(
                "[Runtime] auto-approved RECALIBRATE_SKILL for %s — applied",
                skill_id,
            )
            return
        except Exception:
            logger.exception(
                "[Runtime] auto-apply RECALIBRATE_SKILL failed — falling back to operator card",
            )

    # Operator approval path — flash a card on /skills.
    try:
        from datetime import datetime as _dt, timezone as _tz
        from systemu.interface.event_bus import EventBus
        EventBus.get().publish({
            "ts": _dt.now(tz=_tz.utc).isoformat(timespec="seconds"),
            "level": "WARNING",
            "category": "approval",
            "origin": origin,   # v0.8.16: trigger origin threaded from execute()
            "message": f"Skill '{skill.name}' needs recalibration",
            "context": {
                "approval_message": (
                    f"Auto-approve declined: {reason}\n\n"
                    f"Proposed new instructions_md:\n\n"
                    f"{result.new_instructions_md[:600]}..."
                ),
                "options": [],
                "redirect_to": "/skills",
                "dedup_key":   f"skill-recalibrate:{skill_id}:{execution_id}",
                "skill_id":    skill_id,
            },
        })
    except Exception:
        logger.debug(
            "[Runtime] could not flash RECALIBRATE_SKILL card", exc_info=True,
        )


def _apply_supervisor_directives(directives, *, context, config, shadow=None, scroll=None, execution_id: str = "", vault=None, consec_tool_fails=None, origin: str = "system") -> None:
    """Apply directives from the Intelligent Supervisor between iterations.

    Each directive is one of the bounded vocabulary actions defined in
    ``systemu/runtime/execution_mind.ACTION_VOCABULARY``.  The actions
    that mutate the shadow's prompt or state are applied directly here;
    DO_NOTHING / ESCALATE / TERMINATE / SWAP_SHADOW return-only signals
    are logged but currently still flow through the standard outcome
    path — v0.4.0-d focuses on the in-shadow effects (NUDGE, REFLECT,
    ROLLBACK, SET_THINK_BUDGET).  Future phases route the operator-
    facing actions through the v0.3.6 approval-flash bus.
    """
    for d in directives:
        try:
            if d.action == "NUDGE" and d.hint:
                context.queue_reflection_block(f"Supervisor nudge: {d.hint}")
                context.add_sticky_note(f"Supervisor nudge: {d.hint[:120]}")
            elif d.action == "INJECT_REFLECTION":
                # Mind's rationale carries the structured reflection text.
                context.queue_reflection_block(
                    f"Supervisor reflection: {d.rationale or 'reassess strategy.'}"
                )
            elif d.action == "FORCE_REFLECT":
                context.queue_reflection_block(
                    "Supervisor requires you to emit a REFLECT decision next, "
                    "naming the strategy you intend to follow. "
                    f"Supervisor reasoning: {d.rationale[:200]}"
                )
            elif d.action == "ROLLBACK":
                target = context.rollback_to_last_snapshot()
                if target is not None:
                    context.queue_reflection_block(
                        "Supervisor rolled back the context to the last snapshot. "
                        "Sticky notes preserved — choose a different approach."
                    )
            elif d.action == "SET_THINK_BUDGET" and d.think_budget_delta:
                # Bump the in-memory ceiling for this run only.
                new_val = (getattr(config, "max_consecutive_think", 5) or 5) + int(d.think_budget_delta)
                try:
                    config.max_consecutive_think = max(1, min(new_val, 30))
                except Exception:
                    pass  # frozen dataclass; supervisor must use a mutable config
            elif d.action == "TERMINATE":
                # v0.4.1-b: TERMINATE now produces an operator-facing approval
                # card + records to the affinity log so future assignment
                # decisions can exclude the shadow that just gave up.
                _apply_terminate_directive(
                    d, context=context, shadow=shadow, scroll=scroll,
                    execution_id=execution_id, vault=vault, origin=origin,
                )
            elif d.action == "RECALIBRATE_TOOL":
                # v0.5.0-d: tool inadequacy → diagnose → bump / fork → operator card.
                # The dispatcher infers the failing tool from the rolling
                # ``_consec_tool_fails`` map (most-recently-failing tool wins).
                _apply_recalibrate_tool_directive(
                    d, context=context, shadow=shadow, scroll=scroll,
                    execution_id=execution_id, config=config, vault=vault,
                    consec_tool_fails=consec_tool_fails or {}, origin=origin,
                )
            elif d.action == "RECALIBRATE_SKILL":
                # v0.6.1-c: skill inadequacy → re-author instructions_md →
                # operator card (or auto-apply when low-risk + env knob set).
                _apply_recalibrate_skill_directive(
                    d, context=context, vault=vault, config=config,
                    execution_id=execution_id, origin=origin,
                )
            elif d.action in ("DO_NOTHING", "SWAP_SHADOW", "ESCALATE"):
                # No-op in-shadow; operator-facing — handled at the
                # supervisor / orchestration layer or future phases.
                pass
        except Exception:
            logger.debug("[Runtime] directive application failed for %s", d.action, exc_info=True)


def _build_reflection_block(
    *,
    tool_name: str,
    category: str,
    keyword,
    consec: int,
    strategies: list,
    force_reflect: bool,
) -> str:
    """Compose the v0.4.0-b in-loop reflection block.

    Compact intentionally — token budget is precious.  References the
    classifier's category, the consecutive-failure count, and the
    recommended strategy enumeration so the LLM can either pick one or
    issue a REFLECT decision (which is mandatory once consec ≥ 3).
    """
    strategy_lines = "\n".join(f"  - {s}" for s in strategies)
    kw = f" (keyword: {keyword})" if keyword else ""
    body = (
        f"The tool **{tool_name}** has failed **{consec}** time(s) "
        f"this run.  Failure category: **{category}**{kw}.\n\n"
        f"Recommended strategies:\n{strategy_lines}\n\n"
        "If a strategy is clearly best, take it directly via the "
        "appropriate action.  Otherwise, emit a single REFLECT decision "
        "that names the strategy you intend to follow next."
    )
    if force_reflect:
        body += (
            "\n\n**Required**: your NEXT decision MUST be `REFLECT` "
            "(this tool has failed ≥3 times — surface your strategy choice "
            "explicitly before any further tool call).  After REFLECT, "
            "proceed with the chosen strategy."
        )
    return body


def _record_terminal_telemetry(
    *,
    shadow,
    execution_id: str,
    scroll,
    status: str,
    iteration: int,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """v0.4.0-0 — best-effort telemetry write at execution terminal state.

    Skipped for ``status="success"`` (we only want failure-mode data).
    Failures inside this function are swallowed inside the telemetry module
    so the shadow's exit path is never affected.

    v0.4.3-a: Also records the outcome to ShadowMetrics keyed by
    (shadow_id, intent_hash) — this runs for ALL statuses including
    success.  The metrics feed the supervisor's affinity-routing
    alternative-selection so shadows with proven track records on
    similar work get preferred.
    """
    try:
        from systemu.runtime.failure_telemetry import record_execution_terminal
        record_execution_terminal(
            shadow_id=(shadow.id if shadow is not None else None),
            execution_id=execution_id,
            activity_id=None,                # not directly available at this seam
            scroll_id=(scroll.id if scroll is not None else None),
            status=status,
            iterations=iteration,
            extra=extra,
        )
    except Exception:
        logger.debug("[Runtime] terminal telemetry skipped", exc_info=True)

    # v0.4.3-a: separate metric update path — runs for every status.
    _record_shadow_metric(shadow=shadow, scroll=scroll, status=status)


def _record_shadow_metric(*, shadow, scroll, status: str) -> None:
    """Update ShadowMetrics for this terminal state.

    Computes the intent_hash from the scroll and records the outcome.
    Skipped silently when the shadow / scroll / status can't be resolved.
    """
    if shadow is None or scroll is None:
        return
    try:
        from systemu.runtime.affinity_log import compute_intent_hash
        from systemu.runtime.shadow_metrics import get_shadow_metrics
        intent_hash = compute_intent_hash(
            intent=getattr(scroll, "intent", "") or "",
            objectives=getattr(scroll, "objectives", None),
        )
        get_shadow_metrics().record(
            shadow_id=getattr(shadow, "id", ""),
            intent_hash=intent_hash,
            status=status,
        )
    except Exception:
        logger.debug("[Runtime] shadow_metrics record skipped", exc_info=True)


def _build_history_slice(context, max_events: int = 30) -> list:
    """Return the LAST N tool-call/observation/thought events (in chronological
    order) as a compact list for LLM context.

    v0.9.7 fix (round-about-loop root cause): collect from the NEWEST event
    backward, then restore chronological order. The previous implementation
    iterated the recent window oldest-first and ``break``-ed after N, so on longer
    runs it returned the OLDEST N events and silently dropped the most recent
    ones — the model could not see what it had just done and re-proposed it.
    """
    recent = []
    for event in reversed(context._history):   # newest-first so we never drop recent events
        if event.event_type == "tool_call":
            recent.append({
                "role": "tool_call",
                "tool": event.content.get("tool_name"),
                "params": event.content.get("parameters"),
                "completes_objective": event.content.get("completes_objective"),
            })
        elif event.event_type == "observation":
            c = event.content
            # Truncate large data fields so they don't bloat the prompt
            if isinstance(c, dict):
                preview = {}
                for k, v in c.items():
                    preview[k] = str(v)[:400] if isinstance(v, (dict, list)) and len(str(v)) > 400 else v
                c = preview
            recent.append({"role": "tool_result", "result": c})
        elif event.event_type == "thought":
            recent.append({"role": "thought", "thought": event.content.get("thought", "")[:300]})
        if len(recent) >= max_events:
            break
    recent.reverse()   # restore chronological (oldest→newest) order for the prompt
    return recent


def _coerce_scalar_parameter(value, tool_name: str, tools) -> dict:
    """v0.9.7: coerce a non-dict tool-call ``parameters`` into a kwargs dict.

    Some LLMs emit a bare scalar (e.g. ``"http://ip-api.com/json/"``) instead of
    ``{param: value}`` for a single-argument tool. If the named tool declares
    exactly one parameter, wrap the scalar as ``{that_param: value}``; otherwise
    return ``{}`` (the tool's required-arg guard will then surface a clear error
    rather than the runtime crashing on ``parameters.keys()``).
    """
    names: list = []
    for t in (tools or []):
        if getattr(t, "name", None) == tool_name:
            names = list(getattr(t, "parameter_names", []) or [])
            if not names:
                schema = getattr(t, "parameters_schema", {}) or {}
                if isinstance(schema, dict):
                    names = list(schema.keys())
            break
    if len(names) == 1:
        return {names[0]: value}
    return {}


import re as _re

_PKG_TOKEN_RE = _re.compile(r"^[A-Za-z0-9._-]+$")


def _install_hint(missing_packages: list) -> str:
    """Return an actionable install hint, never the misleading 'pip install unknown'.

    If the first entry looks like a real package token, suggest pip; otherwise
    (it's a human fallback phrase, or the list is empty) point at the manifest.
    """
    pkg = (missing_packages or [None])[0]
    if pkg and _PKG_TOKEN_RE.match(pkg):
        return f"pip install {pkg}"
    return "see the tool's manifest"


def _resolve_missing_packages(result_missing, declared) -> list:
    """Pick the most honest missing-package list for operator messaging.

    Priority: the tool result's own missing_packages → the tool's declared
    manifest dependencies → a clear human phrase. NEVER returns ['unknown'].
    """
    if result_missing:
        return list(result_missing)
    if declared:
        return list(declared)
    return ["a required package (see tool manifest)"]


def _dep_failure_messages(
    *,
    tool_name:        str,
    error_type:       str,
    missing_packages: list,
    hint:             str,
    pip_tail:         Optional[str] = None,
) -> tuple[str, str, str]:
    """Build (LLM-facing, operator-facing, op-log-level) messages for a dep-failure result.

    Centralised so the four error_types stay consistent in tone and
    actionability.  The LLM message always tells the Shadow "do not retry,
    FAIL the objective" — the variability is in *why* and in what the
    operator should do.
    """
    pkgs = ", ".join(missing_packages) if missing_packages else "a required package (see tool manifest)"
    if error_type == "missing_dependency":
        return (
            f"Tool '{tool_name}' cannot run: Python package '{pkgs}' is not installed "
            f"and the tool manifest does not declare it. This tool is permanently "
            f"unavailable for this execution. Do not call it again. Issue a FAIL "
            f"action for any objective that requires it.",
            f"Tool '{tool_name}' failed — undeclared missing package '{pkgs}'. "
            f"Add it to the tool's manifest and install with: {hint}",
            "WARNING",
        )
    if error_type == "dependency_install_pending_approval":
        return (
            f"Tool '{tool_name}' cannot run: it requires Python package(s) '{pkgs}' "
            f"which need operator approval before installing. This tool is "
            f"permanently unavailable for this execution. Do not call it again. "
            f"Issue a FAIL action for any objective that requires it.",
            f"Tool '{tool_name}' is awaiting operator approval to install: {pkgs}. "
            f"{hint}",
            "WARNING",
        )
    if error_type == "dependency_install_blocked":
        return (
            f"Tool '{tool_name}' cannot run: dependency installation is disabled in "
            f"this environment, and required package(s) '{pkgs}' are not pre-installed. "
            f"This tool is permanently unavailable for this execution. Do not call "
            f"it again. Issue a FAIL action for any objective that requires it.",
            f"Tool '{tool_name}' blocked: install mode is OFF and package(s) '{pkgs}' "
            f"are missing. Bake them into the base image or enable "
            f"SYSTEMU_TOOL_DEP_INSTALL_MODE=prompt.",
            "ERROR",
        )
    # dependency_install_failed
    extra = f" (pip stderr tail: {pip_tail[:200]})" if pip_tail else ""
    return (
        f"Tool '{tool_name}' cannot run: automatic install of '{pkgs}' failed. "
        f"This tool is permanently unavailable for this execution. Do not call it "
        f"again. Issue a FAIL action for any objective that requires it.",
        f"Tool '{tool_name}' install failed for {pkgs}{extra}. Investigate network / "
        f"environment and retry. {hint}",
        "ERROR",
    )


class ShadowRuntime:
    """Runs a Shadow through an Activity's Scroll using the agentic loop.

    Args:
        config:       Config carrying OpenRouter key + tier model names.
        vault:        Vault instance for entity lookups and persistence.
        executions_dir: Path where execution snapshots are persisted.
    """

    def __init__(
        self,
        config: Config,
        vault:  Vault,
        executions_dir: Optional[Path] = None,
    ):
        self.config        = config
        self.vault         = vault
        # v0.9.1.1 fix: load user_profile once at init so _resolve_verifier_output_dir
        # can actually prefer user_profile.default_output_dir over config.output_dir.
        try:
            self.user_profile = vault.get_user_profile() if vault is not None else None
        except Exception:
            self.user_profile = None
        _vault_root = Path(config.vault_dir).resolve()
        # Pick the backend from config (resolved at Config.from_env() time
        # from SYSTEMU_TOOL_BACKEND; defaults to "local").
        _backend_is_docker = (config.tool_backend == "docker")

        # Resolve dependency-installer policy once per Shadow runtime so the
        # sandbox + registry agree on InstallMode/approvals.  In docker
        # backends the registry isn't attached and the installer is dormant;
        # we still resolve the mode so a future docker-side hook can read it.
        from systemu.runtime.dependency_installer import resolve_install_mode
        from systemu.runtime.dep_approvals import init_default_store
        install_mode = resolve_install_mode(
            config_mode=getattr(config, "tool_dep_install_mode", None),
            systemu_mode=getattr(config, "systemu_mode", None),
        )
        # Approval store lives alongside other runtime state (data/).  When
        # the operator hasn't run anything yet the file is created lazily on
        # first approve/record_pending call.
        approvals = init_default_store(Path("data"))

        self.sandbox       = ToolSandbox(
            vault_root=_vault_root,
            backend=config.tool_backend,
            default_timeout=(
                config.docker_tool_timeout if _backend_is_docker else 30
            ),
            install_mode=install_mode,
            approvals=approvals,
            vault=vault,         # v0.9.1: T8 must-wire — enables _after_successful_call audit hook
            config=config,       # v0.9.1: T8 must-wire — enables max_result_size_chars truncation
        )
        # Attach ToolRegistry for the direct-call fast path (avoids subprocess overhead
        # and fixes path-resolution issues with relative vault_dir configurations).
        # The fast path is only safe for the in-process local backend; Docker / SSH /
        # WSL backends always go through the sandbox protocol.
        if not _backend_is_docker:
            from systemu.runtime.tool_registry import ToolRegistry
            _impl_dir = _vault_root / "tools" / "implementations"
            self.sandbox.attach_registry(
                ToolRegistry(
                    _impl_dir, vault,
                    install_mode=install_mode,
                    approvals=approvals,
                )
            )
        self.executions_dir = (
            executions_dir or Path(config.vault_dir) / "executions"
        )
        # Tools that returned a dep-related error during this execution.
        # Mapped to the list of packages that blocked them so we can clear the
        # suppression precisely when an approval lands (v0.3.6 no-restart fix).
        # Older code paths that used this as a set still see truthy membership
        # via ``tool_name in self._dep_failed_tools``.
        self._dep_failed_tools: dict[str, list[str]] = {}
        # v0.4.0-b: per-tool consecutive-failure counter for in-loop reflection.
        # Reset whenever the same tool succeeds.
        self._consec_tool_fails: dict[str, int] = {}
        # v0.8.16: canonical trigger origin for every event this runtime
        # publishes.  Defaults to "manual"; `execute()` resets it from the
        # passed `origin` (or the activity's stamped origin) at the top of a run.
        self._origin: str = "manual"
        # v0.8.17: consecutive degraded web-search counter; reset per run in execute().
        self._consec_degraded_search: int = 0
        # v0.8.21: stuck-loop guard counters; reset at top of execute() like _consec_degraded_search.
        self._iters_since_obj_credit: int = 0
        self._same_tool_fail_streak: dict[str, int] = {}
        self._stuck_round_for_obj: dict[int, int] = {}
        self._operator_hint: "str | None" = None
        # v0.9.1 (Layer 4): per-objective verifier state + fresh-work flag.
        # Reset per run in execute(); mutated during completes_objective path.
        self._objective_states: dict[int, ObjectiveState] = {}
        self._fresh_work_since_last_verifier_call: bool = False
        # Directory is created lazily when the vault's prune_old_executions needs it;
        # we no longer eagerly create it since snapshot/SKILL.md disk writes are removed.

        # v0.9.3: discover code-registered tools at runtime startup so the
        # main loop + verifier fork can use them.
        _discover_v2_tools()

    # ─────────────────────────────────────────────────────────────────────────

    def _stamp(self, event: dict) -> dict:
        """v0.8.16: stamp the canonical trigger origin onto an event payload.

        ``setdefault`` so an event that already carries an explicit ``origin``
        is never clobbered.  Used to wrap every EventBus publish so the
        origin-partitioned live panes can filter on ``event["origin"]``.
        """
        event.setdefault("origin", getattr(self, "_origin", "manual"))
        return event

    # ─────────────────────────────────────────────────────────────────────────

    def _iteration_event(
        self,
        *,
        iteration,
        decision,
        tool_result=None,
        execution_id=None,
        llm_ref=None,
    ) -> dict:
        """v0.8.16: build a bounded per-iteration event with expandable details.

        The ``details`` dict carries the reasoning + tool I/O the live panes
        render on expand.  ``tool_result`` is truncated (≤4000 chars) and the
        raw LLM completion is NOT inlined — only referenced by ``llm_ref``
        ({exec_id, call_index}) for lazy load from the per-execution transcript.
        """
        d = decision or {}
        action = d.get("action", "?")
        message = f"iter={iteration} {action}"
        if action == "TOOL_CALL":
            message += f" {d.get('tool_name', '')}"
        return self._stamp({
            "ts":       utcnow().isoformat() + "Z",
            "level":    "INFO",
            "category": "runtime",
            "message":  message,
            "context":  {"execution_id": execution_id},
            "details": {
                "reasoning":   d.get("reasoning") or d.get("thought"),
                "action":      action,
                "tool_name":   d.get("tool_name"),
                "tool_params": d.get("parameters"),
                "tool_result": (str(tool_result)[:4000] if tool_result is not None else None),
                "llm_ref":     llm_ref,
            },
        })

    # ─────────────────────────────────────────────────────────────────────────

    def _gate3_check(self, tool) -> "dict | None":
        """Return structured error dict if the tool can't be invoked, else None.

        v0.6.9: messages now point operators to the dashboard recovery URL
        instead of the misleading "Re-forge with feedback" instruction —
        most blockers are dep approval / dry-run re-runs, not re-forges.
        """
        from systemu.recovery.links import recover_url
        if not getattr(tool, "enabled", False):
            return {
                "reason": "GATE_3_DISABLED",
                "action_url": recover_url("tool", tool.id),
                "message": (
                    f"Tool {tool.name} is disabled. "
                    f"Apply the fix at {recover_url('tool', tool.id)}"
                ),
            }
        if getattr(tool, "dry_run_status", None) == "failed":
            ev = getattr(tool, "dry_run_evidence", None) or {}
            classified = ev.get("classified_reason", "DRY_RUN_FAILED_BUG")
            return {
                "reason": classified,
                "missing_package": ev.get("missing_package"),
                "action_url": recover_url("tool", tool.id),
                "message": (
                    f"Tool {tool.name} dry-run failed ({classified}). "
                    f"Apply the fix at {recover_url('tool', tool.id)}"
                ),
            }
        return None

    CIRCUIT_BREAKER_FAILURES = 3  # v0.6.9: bail after N consecutive same-tool same-reason failures

    def _record_tool_failure(self, tool_name: str, reason: str) -> bool:
        """v0.6.9 iteration-loop circuit breaker.

        Append a failure to the consecutive-failures window. Returns True
        when the circuit is now tripped (caller should bail the iteration
        loop with a useful summary that points to the recovery URL).

        The window resets on any change in (tool_name, reason): a different
        tool or a different failure class indicates the LLM is exploring,
        not stuck in a retry loop.
        """
        if not hasattr(self, "_consecutive_failures"):
            self._consecutive_failures = []
        key = (tool_name, reason)
        if self._consecutive_failures and self._consecutive_failures[-1] != key:
            self._consecutive_failures = []
        self._consecutive_failures.append(key)
        return len(self._consecutive_failures) >= self.CIRCUIT_BREAKER_FAILURES

    # ─────────────────────────────────────────────────────────────────────────
    # v0.8.21 — stuck-loop guard helpers (pure; wired into execute() in T6).
    # ─────────────────────────────────────────────────────────────────────────

    def _update_stuck_counters(self, *, action: str, tool_name: "str | None",
                                 tool_success: "bool | None", credited_obj_id: "int | None") -> None:
        """v0.8.21: per-iteration counter update.
        Progress (objective credited) resets BOTH counters.
        TOOL_CALL failure increments same_tool_fail_streak.
        Any iteration without a credit increments iters_since_obj_credit."""
        if credited_obj_id is not None:
            self._iters_since_obj_credit = 0
            self._same_tool_fail_streak.clear()
            return
        self._iters_since_obj_credit += 1
        if action == "TOOL_CALL" and tool_name:
            if tool_success:
                self._same_tool_fail_streak[tool_name] = 0
            else:
                self._same_tool_fail_streak[tool_name] = \
                    self._same_tool_fail_streak.get(tool_name, 0) + 1

    def _stuck_trigger(self) -> "tuple[bool, str]":
        """v0.8.21: hybrid trigger — no-progress OR same-tool-failure streak."""
        no_progress, tool_fails, guard_on = _stuck_thresholds()
        if not guard_on:
            return (False, "")
        if self._iters_since_obj_credit >= no_progress:
            return (True, f"no objective credit for {self._iters_since_obj_credit} iterations")
        worst = max(self._same_tool_fail_streak.items(),
                    key=lambda kv: kv[1], default=(None, 0))
        if worst[1] >= tool_fails:
            return (True, f"tool '{worst[0]}' failed {worst[1]} consecutive times")
        return (False, "")

    def _ask_stuck_or_degrade(self, *, execution_id, current_objective,
                                 tools_tried, reason: str,
                                 scroll_id: str = "", activity_id: str = "",
                                 shadow_id: str = ""):
        """v0.8.21: post stuck-loop decision via v0.8.19 R3 request_choice.
        Returns the answer dict on resume, None when no queue (headless),
        raises PendingChoiceRequest while awaiting operator.
        v0.8.22.1 (R2): dedup_key is execution-INDEPENDENT (keyed by scroll +
        objective + round) so a resumed run reaches the same decision. (R4):
        the decision context carries the resume coordinates."""
        round_n = self._stuck_round_for_obj.get(current_objective.id, 0) + 1
        self._stuck_round_for_obj[current_objective.id] = round_n
        dedup = f"stuck:{scroll_id or execution_id}:obj_{current_objective.id}:r{round_n}"
        goal_short = (getattr(current_objective, "goal", "") or "")[:120]
        tried = ", ".join(sorted(set(tools_tried or [])))
        qs = [{
          "id": "action",
          "prompt": (f"Stuck on Objective {current_objective.id}: '{goal_short}'.  "
                     f"{reason}. Tools tried: {tried or '(none)'}."),
          "multi": False,
          "options": [
            {"label": "Provide hint",   "desc": "free-text suggestion folded into next iteration"},
            {"label": "Accept partial", "desc": "finalize with completed objectives; mark this as N/A"},
            {"label": "Cancel run",     "desc": "stop the run cleanly"},
          ],
          "allow_free_text": True,
        }]
        from systemu.interface.notifications import request_choice
        return request_choice(qs, dedup_key=dedup, extra_context={
            "execution_id": execution_id,
            "activity_id":  activity_id,
            "scroll_id":    scroll_id,
            "shadow_id":    shadow_id,
            "objective_id": current_objective.id,
            "stuck_round":  round_n,
        })

    def _finalize_stuck(self, *, context, status: str, reason: str,
                          stuck_on: int, completed, iteration: int,
                          tool_calls_made: int, scroll, shadow,
                          execution_id: str, exec_start: float,
                          total_objectives: int):
        """v0.8.21: terminal finalize for stuck-loop. Mirrors the MaxIterations path
        (build_result + telemetry + refinery + shadow-log) so downstream consumers
        treat 'partial' / 'cancelled' here identically."""
        try:
            self._append_to_shadow_log(
                shadow, execution_id, status, f"Stuck-loop: {reason}",
                iteration_count=iteration, tool_calls_made=tool_calls_made,
                objectives_completed=len(completed or []),
                objectives_total=total_objectives,
                duration_seconds=(__import__("time").time() - exec_start),
            )
        except Exception:
            pass
        # v0.8.22.1 (Fix 3): a deliberate operator cancel is not a system "stuck"
        # failure — don't mislabel it or stamp it with the StuckLoopDetected error.
        if status == "cancelled":
            _summary = f"Run cancelled by operator (was working on objective {stuck_on})."
            _err = None
        else:
            _summary = f"Stuck on objective {stuck_on}: {reason}"
            _err = "StuckLoopDetected"
        res = context.build_result(
            status=status,
            final_summary=_summary,
            error=_err,
        )
        try:
            _record_terminal_telemetry(
                shadow=shadow, execution_id=execution_id, scroll=scroll,
                status=status, iteration=iteration,
                extra={"reason": "StuckLoopDetected",
                       "stuck_on_objective": stuck_on},
            )
        except Exception:
            pass
        try:
            _dispatch_refinery(shadow, scroll, res, context, self.config, self.vault)
        except Exception:
            pass
        # v0.9.2: episodic capture — best-effort, never raises
        _trigger_episodic_capture(
            vault=getattr(self, 'vault', None),
            config=getattr(self, 'config', None),
            session_id=execution_id,
            intent=getattr(scroll, "intent", ""),
            chat_result=None,
            files_produced=[],
            status=status,
            execution_id=execution_id,
        )
        return res

    def _apply_stuck_answer(self, stuck_obj, ans: dict, *, finalize):
        """v0.8.22.1 (R6): map a resolved stuck answer to an action.
        Returns ("continue", None) to keep looping (hint applied), or
        ("finalize", <result>) when the answer is partial/cancel.
        `finalize` is a callable(**kwargs) -> result (the caller binds the
        _finalize_stuck context/scroll/shadow/etc.)."""
        action_choice = (ans or {}).get("action") or ""
        _canonical = {"Provide hint", "Accept partial", "Cancel run"}
        if action_choice in _canonical:
            hint_text = ""
        else:
            hint_text = action_choice.strip()
            action_choice = "Provide hint" if hint_text else action_choice
        if action_choice == "Provide hint" and hint_text:
            self._operator_hint = (
                f"## Operator hint (use to retry Objective {stuck_obj.id})\n{hint_text}"
            )
            self._iters_since_obj_credit = 0
            self._same_tool_fail_streak.clear()
            return ("continue", None)
        if action_choice == "Accept partial":
            return ("finalize", finalize(status="partial"))
        if action_choice == "Cancel run":
            return ("finalize", finalize(status="cancelled"))
        # ambiguous → treat as partial
        return ("finalize", finalize(status="partial"))

    def _build_memory_context_for_prompt(self) -> str:
        """LLM-facing memory view (consolidated, not the raw execution_log).
        v0.6.9: also includes refined lessons from memory_buffer, filtered
        for resolved causes.
        v0.7-g: buffer comes via the configurable memory backend (defaults to
        filesystem, lifts the existing vault layout — operators can switch to
        Mem0 via SYSTEMU_MEMORY_BACKEND=mem0)."""
        from systemu.runtime.memory_consolidator import MemoryConsolidator
        log = self.shadow.execution_log or []
        try:
            from systemu.runtime.memory_backends import get_backend
            backend = get_backend(getattr(self, "config", None))
            buffer_entries = backend.load_buffer(self.shadow.id)
        except Exception:
            # Fall back to the legacy vault path if backend init fails
            try:
                buffer_entries = self.vault.load_shadow_memory(self.shadow.id)[1]
            except Exception:
                buffer_entries = []
        return MemoryConsolidator().consolidate_with_buffer(
            execution_log=log, buffer_entries=buffer_entries or [], vault=self.vault,
        )

    # ─────────────────────────────────────────────────────────────────────────

    async def execute(
        self,
        shadow:   Shadow,
        activity: Activity,
        *,
        dry_run: bool = False,
        cancel_event: Optional[threading.Event] = None,
        resume_from_execution_id: Optional[str] = None,
        origin: Optional[str] = None,
        chat_submission_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Run a Shadow through its assigned Activity.

        Args:
            shadow:        The Shadow persona to execute under.
            activity:      The Activity to execute (carries scroll_id + tool_ids).
            dry_run:       If True, no tools are actually invoked — prints plan only.
            cancel_event:  Optional threading.Event set by the Supervisor watchdog to
                           request clean cancellation.  Checked at the top of each
                           loop iteration — shadow exits with status="cancelled".
            origin:        v0.8.16 — canonical trigger origin threaded from the
                           Supervisor queue payload.  Falls back to the activity's
                           own ``origin`` field, then "manual".  Stamped onto every
                           event this run publishes so the origin-partitioned live
                           panes can filter on it.

        Returns:
            Execution result dict with status, summary, snapshots_taken, etc.
        """
        # v0.8.16: resolve + remember the trigger origin for the whole run so
        # `_stamp` can tag every published event.
        self._origin = origin or getattr(activity, "origin", None) or "manual"
        # v0.8.17: reset per-run consecutive-degraded-search counter.
        self._consec_degraded_search = 0
        # v0.8.21: reset stuck-guard counters per run (declared in __init__).
        self._iters_since_obj_credit = 0
        self._same_tool_fail_streak.clear()
        self._stuck_round_for_obj.clear()
        self._operator_hint = None
        self._resume_stuck_answer = None  # v0.8.22.1 (R6): (obj_id, answer) lifted from snapshot
        # v0.9.1 (Layer 4): reset verifier bookkeeping per run.
        self._objective_states.clear()
        self._fresh_work_since_last_verifier_call = False
        # v0.9.0 (Layer 1): one-block user context computed once per run.
        # Prompt assembly can read self._user_context_block; Layer 2 will
        # extend this with episodic memory.
        self._user_context_block = _build_user_context_block(self.vault)
        # v0.8.22 (C): carry chat_submission_id for the run so the R3 producers
        # can thread it into OperatorDecision.context, enabling the chat UI to
        # surface decisions inline.
        from systemu.runtime.chat_submission_ctx import set_chat_submission_id
        self._chat_submission_id = chat_submission_id
        self._chat_submission_token = set_chat_submission_id(chat_submission_id)
        try:
            execution_id = _gen_execution_id()
            exec_start   = __import__("time").time()
            tool_call_count = 0
            logger.info(
                "[Runtime] Starting execution %s — shadow='%s' activity='%s'",
                execution_id, shadow.name, activity.name,
            )

            # ── Load entities from vault ──────────────────────────────────────────
            scroll = self.vault.get_scroll(activity.scroll_id)
            tools  = self._load_tools(activity.required_tool_ids, dry_run=dry_run)
            skills = self._load_skills(activity.required_skill_ids)

            # ── Determine execution mode: objectives (new) or action_blocks (legacy) ─
            use_objectives = bool(scroll.objectives)
            objectives     = scroll.objectives if use_objectives else []
            scroll_json    = [obj.model_dump(mode="json") for obj in objectives] if use_objectives \
                             else [ab.model_dump(mode="json") for ab in scroll.action_blocks]

            if use_objectives and not objectives:
                return {"status": "failure", "error": "Scroll has no objectives", "execution_id": execution_id}
            if not use_objectives and not scroll.action_blocks:
                return {"status": "failure", "error": "Scroll has no ActionBlocks", "execution_id": execution_id}

            if not tools and not dry_run:
                return {"status": "failure", "error": "No deployed tools available for this Shadow", "execution_id": execution_id}

            if not tools and dry_run:
                logger.warning("[Runtime] Dry-run with 0 tools — executing as THINK-only planning mode")

            # ── Build skeleton indexes (Progressive Disclosure) ─
            # Include parameter_names so the LLM knows which kwargs each tool
            # expects WITHOUT needing a LOAD_RESOURCE round-trip.  Without this
            # the LLM has to guess, leading to tool_call(args={}) and the tool's
            # required-arg guard rejecting every call.
            tool_index = [
                {
                    "id": t.id,
                    "name": t.name,
                    "description": t.description,
                    "parameter_names": list(getattr(t, "parameter_names", []) or []),
                    # v0.9.7: surface the REAL parameter schema (was hardcoded {},
                    # which left the executor LLM blind to v1 params → bare-string
                    # args → AttributeError on parameters.keys()).
                    "parameters_schema": dict(getattr(t, "parameters_schema", {}) or {}),
                }
                for t in tools
            ]
            # v0.9.5 T0: augment v1 tool_index with v2-registered tools so the
            # LLM can actually call them (file_tools, skill_tools, capability_tools,
            # etc.). Without this, all L3/L5/L6 LLM tools are dead code.
            _existing_names = {t["name"] for t in tool_index}
            _v2_entries = _build_llm_tool_catalog(
                vault=None,  # v2 portion only — v1 already in tool_index above
                config=getattr(self, "config", None),
            )
            for _entry in _v2_entries:
                if _entry["name"] not in _existing_names:
                    tool_index.append(_entry)
                    _existing_names.add(_entry["name"])
            skill_index = [
                {"id": s.id, "name": s.name, "category": s.category, "description": s.description}
                for s in skills
            ]

            # ── Boot-time memory injection ────────────────────────────────────────
            # Global memory: always full (personalisation applies to every task).
            # Shadow memory: header-only at boot; shadow calls LOAD_RESOURCE on demand.
            recalled_memory = _build_boot_memory(shadow, self.vault)

            # ── Initialise context ────────────────────────────────────────────────
            context = ExecutionContext(
                execution_id=execution_id,
                system_prompt=shadow.system_prompt,
                scroll_json=scroll_json,
                tool_index=tool_index,
                skill_index=skill_index,
                recalled_memory=recalled_memory,
                use_objectives=use_objectives,
                scroll_intent=scroll.intent,
            )

            step_prompt = load_prompt("execute_step.md")

            # Objective tracking (intent-driven mode)
            completed_objectives: set[int] = set()
            total_objectives = len(objectives)

            # v0.8.19 (R2): publish the initial objective_state so the live pane
            # can render the full checklist at boot.  Best-effort — EventBus is
            # optional and a publish failure must never break execution.
            if use_objectives:
                try:
                    from systemu.interface.event_bus import EventBus
                    EventBus.get().publish(_objective_state_event(
                        objectives, completed_objectives, execution_id, stamp=self._stamp))
                except Exception:
                    pass  # EventBus is optional — never break execution

            # Legacy ActionBlock tracking
            current_ab   = 1

            # ── v0.5.1-e: resume from prior-execution snapshot ─────────────────
            # When the supervisor's RECALIBRATE_TOOL → operator-approval flow
            # triggers re-queue with resume_from_execution_id, load the snapshot
            # and pre-populate sticky notes + completed_objectives so the new
            # run picks up where the prior one left off.  Snapshot is consumed
            # (deleted) after read so a subsequent restart starts clean.
            if resume_from_execution_id:
                try:
                    from systemu.runtime.execution_snapshot import (
                        apply_to_context, delete_snapshot, read_snapshot,
                    )
                    snap = read_snapshot(resume_from_execution_id)
                    if snap is not None:
                        apply_to_context(snap, context=context)
                        if use_objectives and snap.completed_objective_ids:
                            # Restore completed objective ids — the runtime won't
                            # ask the LLM to redo them.
                            completed_objectives.update(snap.completed_objective_ids)
                            # v0.9.1 (Layer 4): re-credit any objectives NOT in the snapshot
                            # that already have durable evidence on disk.  This covers the
                            # case where the shadow completed work but the snapshot was taken
                            # before the verifier ran (e.g., mid-run restart).
                            try:
                                _uncredited = [
                                    o for o in objectives
                                    if o.id not in completed_objectives
                                ]
                                for _uc_obj in _uncredited:
                                    _rc = recredit_on_resume(
                                        objective=_uc_obj,
                                        vault=self.vault,
                                        config=self.config,
                                        execution_id=resume_from_execution_id or execution_id,
                                        default_output_dir=_resolve_verifier_output_dir(
                                            self.config, getattr(self, "user_profile", None)
                                        ),
                                    )
                                    if _rc.credited:
                                        completed_objectives.add(_uc_obj.id)
                                        logger.info(
                                            "[Runtime] recredit_on_resume: obj=%d re-credited "
                                            "from existing durable evidence.",
                                            _uc_obj.id,
                                        )
                            except Exception:
                                logger.debug(
                                    "[Runtime] recredit_on_resume hook crashed — skipping",
                                    exc_info=True,
                                )
                        if not use_objectives and snap.current_action_block:
                            current_ab = max(current_ab, int(snap.current_action_block))
                        # v0.8.22.1 (R6): if the operator answered a stuck decision,
                        # lift it (and the round counters) from the snapshot stickies
                        # so we can apply it at resume-start instead of re-triggering.
                        try:
                            import json as _json
                            _ans_note = next((n for n in snap.sticky_notes
                                              if n.startswith("__STUCK_ANSWER__::")), None)
                            _rounds_note = next((n for n in snap.sticky_notes
                                                 if n.startswith("__STUCK_ROUNDS__::")), None)
                            if _rounds_note:
                                self._stuck_round_for_obj = {
                                    int(k): v for k, v in
                                    _json.loads(_rounds_note.split("::", 1)[1]).items()
                                }
                            if _ans_note:
                                _parts = _ans_note.split("::", 2)  # __STUCK_ANSWER__::obj_<id>::<choice_json>
                                _obj_id = int(_parts[1].replace("obj_", ""))
                                try:
                                    _ans = _json.loads(_parts[2])
                                except Exception:
                                    _ans = {"action": _parts[2]}
                                self._resume_stuck_answer = (_obj_id, _ans)
                        except Exception:
                            logger.debug("[Runtime] resume stuck-answer parse failed", exc_info=True)
                        logger.info(
                            "[Runtime] resumed from snapshot of %s — restored %d completed objective(s), %d sticky note(s)",
                            resume_from_execution_id,
                            len(snap.completed_objective_ids),
                            len(snap.sticky_notes),
                        )
                        delete_snapshot(resume_from_execution_id)
                    else:
                        logger.info(
                            "[Runtime] resume requested for %s but no snapshot found — starting fresh",
                            resume_from_execution_id,
                        )
                except Exception:
                    logger.exception("[Runtime] resume hook crashed — starting fresh")

            # v0.8.22.1 (R6): consume a resume-start stuck answer if present.
            _pending = getattr(self, "_resume_stuck_answer", None)
            if _pending and use_objectives:
                _obj_id, _ans = _pending
                self._resume_stuck_answer = None
                _stuck_obj = next((o for o in objectives if o.id == _obj_id),
                                  objectives[-1] if objectives else None)
                if _stuck_obj is not None:
                    from functools import partial as _partial
                    _fin = _partial(self._finalize_stuck, context=context,
                                    reason="resumed", stuck_on=_stuck_obj.id,
                                    completed=list(completed_objectives),
                                    iteration=int(getattr(context, "_resume_iteration", 0)),
                                    tool_calls_made=0, scroll=scroll, shadow=shadow,
                                    execution_id=execution_id, exec_start=exec_start,
                                    total_objectives=len(objectives))
                    _action, _res = self._apply_stuck_answer(_stuck_obj, _ans, finalize=_fin)
                    if _action == "finalize":
                        return _res
                    # else "continue": hint is now in self._operator_hint; loop retries

            last_snap_ab = 0
            iteration    = 0
            consecutive_thinks = 0  # throttle THINK storms
            # v0.8.16: llm_ref for the most-recent tier-2 decision LLM call —
            # {exec_id, call_index} into the per-execution transcript file.  Set
            # right after each LLM call (Task 8); consumed by _iteration_event so
            # the panes can lazily load the raw completion on expand.
            _last_llm_ref: Optional[Dict[str, Any]] = None

            # v0.4.0-d: Intelligent Supervisor (opt-in via config).  When enabled,
            # an ExecutionMind subscribes to events for this run, observes failures,
            # and emits directives into a small inbox the loop drains each tick.
            # When disabled the inbox stays empty and shadow runtime behaves
            # exactly as in v0.3.x.
            # v0.4.1-a: per-shadow opt-in.  The supervisor activates when EITHER
            # the global config flag OR the shadow's own ``supervisor_enabled`` is
            # True.  Lets the operator A/B test on one specialist before flipping
            # the global switch.
            execution_mind = None
            directive_inbox = None
            _supervisor_globally_on = bool(getattr(self.config, "intelligent_supervisor_enabled", False))
            _supervisor_per_shadow_on = bool(getattr(shadow, "supervisor_enabled", False))
            if _supervisor_globally_on or _supervisor_per_shadow_on:
                try:
                    from systemu.runtime.execution_mind import ExecutionMind, DirectiveInbox
                    directive_inbox = DirectiveInbox()
                    execution_mind = ExecutionMind(
                        execution_id=execution_id,
                        shadow_id=getattr(shadow, "id", None),
                        config=self.config,
                        directive_sink=directive_inbox.append,
                        # When only the per-shadow flag is on (global still off),
                        # force the Mind to enable itself rather than reading from
                        # the global config it doesn't know about.
                        force_enabled=_supervisor_per_shadow_on,
                        origin=self._origin,   # v0.8.16: strategy-stream ticks partition on origin
                    )
                    # Stash on self so _handle_tool_call (a method) can reach it
                    # without threading another parameter through the call chain.
                    self._execution_mind = execution_mind
                except Exception:
                    logger.exception("[Runtime] ExecutionMind construction failed — disabling supervisor")
                    execution_mind = None
                    directive_inbox = None
                    self._execution_mind = None
            else:
                self._execution_mind = None

            import asyncio

            # ─── THE AGENTIC LOOP ─────────────────────────────────────────────────
            # v0.9.7: deterministic stall corrector — detects round-about repetition
            # (same tool+args+result, or A↔B ping-pong) and nudges/forces a finish.
            loop_guard = LoopGuard(self.config)
            loop_guard_nudge = None  # pending verdict to inject next iteration
            # v0.9.7 Phase 2: one Governor per execution so harness leases + the
            # ledger stay coherent across REQUEST_HARNESS calls; leases are
            # revoked at terminal state (default-deny never leaks across runs).
            governor = None
            if _intent_engine_enabled(self.config):
                try:
                    from systemu.runtime.governor import Governor as _GovernorCls
                    governor = _GovernorCls(self.config)
                except Exception:
                    governor = None

            def _revoke_harness_leases():
                if governor is not None:
                    try:
                        governor.revoke_leases(execution_id)
                    except Exception:
                        logger.debug("[Runtime] lease revocation failed", exc_info=True)

            # v0.9.7 Phase 3: resolve execution-adherence + a mutable iteration
            # budget for THIS run.
            #   • A COMPUTE harness grant can extend ``_iter_budget`` at runtime.
            #   • ``_adherence`` (free/guided/strict) is resolved from the operator
            #     pin (config.execution_adherence) → else auto: records honor the
            #     per-SOP adherence saved at save-time, chat → free. Under "strict"
            #     the lenient goal-level acceptance shortcut is suppressed so the
            #     per-objective / SOP contract is honored verbatim.
            # All adherence-conditioned behavior remains behind the intent-engine
            # flag (the resolver itself is side-effect-free).
            _iter_budget = MAX_ITERATIONS
            _sop_adherence = (getattr(scroll, "adherence", None) or "").strip() or None
            _origin_l = (origin or getattr(self, "_origin", "") or "").strip().lower()
            _req_kind = "record" if (_origin_l in {"record", "sop", "replay"} or _sop_adherence) else "chat"
            _adherence = "free"
            try:
                from systemu.runtime.adherence import resolve_adherence as _resolve_adh
                _adherence = _resolve_adh(self.config, request_kind=_req_kind, sop_adherence=_sop_adherence)
            except Exception:
                _adherence = "free"
            if _intent_engine_enabled(self.config):
                logger.info("[Runtime] intent-engine: execution adherence=%s (kind=%s).", _adherence, _req_kind)

            while iteration < _iter_budget:
                iteration += 1

                # v0.9.1 (Layer 4): reset per-turn verifier call counter for all objectives
                # at the start of each new LLM turn so the cap is per-turn, not per-run.
                for _vs in self._objective_states.values():
                    _vs.calls_this_turn = 0

                # ── v0.4.0-d: drain supervisor directive inbox and apply ─────────
                # ExecutionMind populates this asynchronously; we apply pending
                # directives at the top of each iteration so they shape the
                # next LLM decision.  Empty when supervisor is disabled.
                if directive_inbox is not None and len(directive_inbox) > 0:
                    # v0.5.1-e: stash loop state on the context so RECALIBRATE_TOOL
                    # snapshot capture can read it without threading every state
                    # variable through every helper.
                    context._resume_iteration = iteration
                    context._resume_current_ab = current_ab
                    context._resume_completed_objectives = (
                        set(completed_objectives) if use_objectives else set()
                    )
                    _apply_supervisor_directives(
                        directive_inbox.drain(),
                        context=context,
                        config=self.config,
                        shadow=shadow,
                        scroll=scroll,
                        execution_id=execution_id,
                        vault=self.vault,
                        consec_tool_fails=self._consec_tool_fails,
                        origin=self._origin,   # v0.8.16: stamp origin on supervisor cards
                    )

                # ── Cancellation gate — Supervisor watchdog may request clean exit ──
                if cancel_event is not None and cancel_event.is_set():
                    logger.info(
                        "[Runtime] Cancellation requested by Supervisor watchdog at iter=%d "
                        "— exiting cleanly (execution_id=%s)",
                        iteration, execution_id,
                    )
                    _record_terminal_telemetry(
                        shadow=shadow, execution_id=execution_id, scroll=scroll,
                        status="cancelled", iteration=iteration,
                    )
                    return {
                        "status":        "cancelled",
                        "final_summary": f"Shadow cancelled by watchdog at iteration {iteration}.",
                        "error":         "WatchdogCancelled",
                        "execution_id":  execution_id,
                    }

                if use_objectives:
                    # Only show objectives whose dependencies are fully satisfied.
                    # Objectives with unmet depends_on are withheld — they become
                    # visible once their prerequisite IDs appear in completed_objectives.
                    pending_objs = [
                        obj.model_dump(mode="json") for obj in objectives
                        if obj.id not in completed_objectives
                        and all(dep in completed_objectives for dep in obj.depends_on)
                    ]
                    logger.debug("[Runtime] Iteration %d/%d — %d/%d objectives done",
                                 iteration, MAX_ITERATIONS, len(completed_objectives), total_objectives)
                else:
                    pending_objs = None
                    logger.debug("[Runtime] Iteration %d/%d — ActionBlock %d",
                                 iteration, MAX_ITERATIONS, current_ab)

                # Build and send the decision prompt
                messages = context.build_messages(
                    current_ab if not use_objectives else 0,
                    completed_objectives=completed_objectives if use_objectives else None,
                )
                # v0.4.0-a: THINK throttle ceiling is now config-driven.
                think_ceiling = getattr(self.config, "max_consecutive_think", 5) or 5
                # v0.8.16: build the decision-prompt system/user once so the LLM
                # transcript writer can record a request summary (the raw
                # completion is recorded after the call returns).
                _llm_system = (
                    step_prompt
                    if consecutive_thinks < think_ceiling else
                    step_prompt + (
                        "\n\n# ENFORCEMENT OVERRIDE\n"
                        f"You have produced {consecutive_thinks} consecutive THINK responses "
                        "with NO tool call. Your NEXT response MUST have "
                        "action==TOOL_CALL, COMPLETE, or FAIL. No more THINK "
                        "will be accepted. Act now."
                    )
                )
                _user_payload = {
                    "shadow_name":        shadow.name,
                    # output_dir: where Shadow-generated files must be written.
                    # Bind-mounted to the host's ./outputs/ directory so files
                    # are accessible outside the container.
                    "output_dir":         self.config.output_dir,
                    # Temporal context — avoids LLM THINK storms over "what is today's date?"
                    "current_date":        _datetime_module.date.today().isoformat(),
                    "current_datetime_utc": utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                    **(
                        {
                            "intent":              scroll.intent,
                            "objectives":          scroll_json,
                            "completed_objectives": list(completed_objectives),
                            "pending_objectives":  pending_objs,
                        }
                        if use_objectives else
                        {
                            "current_action_block": current_ab,
                            "pending_action_blocks": [
                                ab for ab in scroll_json
                                if ab.get("step_number", 0) >= current_ab
                            ],
                        }
                    ),
                    "available_tools": tool_index,
                    "history":         _build_history_slice(context),
                    "last_snapshot":   context._snapshots[-1].summary if context._snapshots else None,
                }
                # v0.9.6 T0: per-iteration guard — ensure v2 tools are always
                # present in the LLM's available_tools even if tool_index was
                # assembled before v2 discovery completed, or if a future code
                # path resets/filters tool_index between the boot-time augmentation
                # (v0.9.5 T0, ~line 1759) and here.  This closes the gap observed
                # in the v0.9.5 live burrito test where the LLM never saw v2 tools.
                _v2_live = _build_llm_tool_catalog(
                    vault=None,  # v2 portion only — v1 already in tool_index
                    config=getattr(self, "config", None),
                )
                _at_live_names = {t.get("name") for t in _user_payload["available_tools"]}
                for _at_entry in _v2_live:
                    if _at_entry["name"] not in _at_live_names:
                        _user_payload["available_tools"].append(_at_entry)
                        _at_live_names.add(_at_entry["name"])
                # v0.8.21: one-shot operator hint fold-back (cleared after this iteration).
                if self._operator_hint:
                    _user_payload["operator_hint"] = self._operator_hint
                    self._operator_hint = None
                # v0.9.7: inject the loop-guard verdict (round-about detection).
                # On 'block', strip tools so the agent MUST COMPLETE or FAIL.
                if loop_guard_nudge is not None:
                    _user_payload["loop_guard_notice"] = loop_guard_nudge.get("message", "")
                    if loop_guard_nudge.get("level") == "block":
                        _user_payload["available_tools"] = []
                        _user_payload["loop_guard_force_finalize"] = True
                    loop_guard_nudge = None
                _llm_user = json.dumps(_user_payload)
                loop = asyncio.get_event_loop()
                try:
                    decision = await loop.run_in_executor(
                        None,
                        lambda: llm_call_json(
                            tier=2,
                            system=_llm_system,
                            user=_llm_user,
                            config=self.config,
                            temperature=0.1,
                            max_tokens=4096,
                        )
                    )
                    # v0.8.16: record the raw decision completion to the per-
                    # execution transcript and remember its index so the
                    # per-iteration event (Task 7) can reference it via llm_ref
                    # for lazy UI expand.  Best-effort — append_call never raises.
                    try:
                        from systemu.runtime.llm_transcript import append_call
                        _call_index = append_call(
                            self.vault.root, execution_id,
                            {
                                "iteration": iteration,
                                "tier":      2,
                                "system":    _llm_system,
                                "user":      _llm_user,
                                "response":  json.dumps(decision),
                            },
                        )
                        _last_llm_ref = (
                            {"exec_id": execution_id, "call_index": _call_index}
                            if _call_index >= 0 else None
                        )
                    except Exception:
                        _last_llm_ref = None
                except Exception as exc:
                    logger.error("[Runtime] LLM error on iteration %d: %s", iteration, exc)
                    log_event("ERROR", "shadow", f"LLM error in execution {execution_id} iteration {iteration}: {exc}", {"shadow_id": shadow.id, "origin": getattr(self, "_origin", "manual")})
                    context.add_thought(f"LLM call failed: {exc}", current_ab)
                    continue

                action = decision.get("action", "THINK")
                # v0.8.21: stuck-guard — every iteration without a TOOL_CALL still counts toward stuck.
                if action != "TOOL_CALL":
                    self._update_stuck_counters(action=action, tool_name=None,
                                                  tool_success=None, credited_obj_id=None)
                if action == "THINK":  # LOAD_RESOURCE is productive — only throttle idle THINK
                    consecutive_thinks += 1
                else:
                    consecutive_thinks = 0
                logger.info("[Runtime] iter=%d action=%s", iteration, action)

                # ── Supervisor heartbeat — signals watchdog this shadow is alive ──
                try:
                    from systemu.runtime.supervisor import Supervisor
                    Supervisor.get().update_heartbeat(activity.id)
                except Exception:
                    pass  # Supervisor not running in CLI/test mode — safe to ignore

                # ── Publish per-iteration event to Systemu Chat ───────────────────
                try:
                    from systemu.interface.event_bus import EventBus
                    EventBus.get().publish(self._stamp({
                        "ts":       utcnow().isoformat() + "Z",
                        "level":    "INFO",
                        "category": "shadow",
                        "message":  f"[{shadow.name}] iter={iteration} action={action}",
                        "context":  {
                            "shadow_id":    shadow.id,
                            "execution_id": execution_id,
                            "iteration":    iteration,
                            "action":       action,
                            "objectives_done": len(completed_objectives) if use_objectives else current_ab - 1,
                            "objectives_total": total_objectives if use_objectives else len(scroll_json),
                        },
                    }))
                except Exception:
                    pass  # EventBus is optional — never break execution

                # ── v0.8.16: per-iteration detail event (reasoning + tool I/O) ─────
                # Carries a bounded `details` dict the live panes render on expand.
                # For TOOL_CALL the publish is deferred to AFTER the tool runs (so
                # tool_result is captured); every other action publishes here.
                if action != "TOOL_CALL":
                    try:
                        from systemu.interface.event_bus import EventBus
                        EventBus.get().publish(self._iteration_event(
                            iteration=iteration,
                            decision=decision,
                            execution_id=execution_id,
                            llm_ref=_last_llm_ref,
                        ))
                    except Exception:
                        pass  # EventBus is optional — never break execution

                # ── COMPLETE ───────────────────────────────────────────────────────
                if action == "COMPLETE":
                    # v0.9.7 intent-engine (flagged, default OFF): GOAL-level
                    # acceptance. Accept COMPLETE when the GOAL is verified from
                    # durable evidence, even if some refiner-baked per-objective
                    # criteria (possibly mis-framed — e.g. a durable-evidence
                    # check on an in-memory "determine X" step) weren't credited.
                    _goal_ok = False
                    if (_intent_engine_enabled(self.config)
                            and _adherence != "strict"
                            and use_objectives
                            and len(completed_objectives) < total_objectives):
                        try:
                            from systemu.runtime import goal_verifier as _gv
                            _gbaseline = state_delta._Baseline(
                                iteration_start_ts="1970-01-01T00:00:00Z")
                            _gdelta = state_delta.compute_delta(
                                baseline=_gbaseline, vault=self.vault,
                                default_output_dir=_resolve_verifier_output_dir(
                                    self.config, getattr(self, "user_profile", None)),
                                chat_result=decision.get("summary"),
                                config=self.config, execution_id=execution_id,
                            )
                            _gres = _gv.verify_goal(
                                goal=(getattr(scroll, "raw_request", None) or getattr(scroll, "intent", "") or ""),
                                delta=_gdelta, config=self.config,
                                chat_result=decision.get("summary"),
                            )
                            _goal_ok = bool(_gres.get("verified"))
                            logger.info(
                                "[Runtime] intent-engine goal-verify: %s — %s",
                                "PASS" if _goal_ok else "no-pass",
                                str(_gres.get("reason", ""))[:160],
                            )
                        except Exception:
                            logger.debug("[Runtime] goal-level verify errored", exc_info=True)

                    # Reject premature COMPLETE when objectives are still pending,
                    # UNLESS goal-level verification accepted it.
                    if use_objectives and len(completed_objectives) < total_objectives and not _goal_ok:
                        missing = [obj.id for obj in objectives if obj.id not in completed_objectives]
                        logger.warning(
                            "[Runtime] COMPLETE rejected — %d/%d objectives still pending: %s",
                            len(missing), total_objectives, missing,
                        )
                        context.add_observation(
                            {
                                "warning": (
                                    f"COMPLETE rejected: {len(missing)} objective(s) not yet "
                                    f"verified: {missing}. Finish all objectives before COMPLETE."
                                )
                            },
                            current_ab,
                        )
                        continue  # Return to loop — shadow must finish remaining objectives

                    summary = decision.get("summary", "Task completed.")
                    logger.info("[Runtime] Execution COMPLETE: %s", summary)
                    self._append_to_shadow_log(
                        shadow, execution_id, "success", summary,
                        iteration_count=iteration, tool_calls_made=tool_call_count,
                        objectives_completed=len(completed_objectives) if use_objectives else len(scroll_json),
                        objectives_total=total_objectives if use_objectives else len(scroll_json),
                        duration_seconds=__import__("time").time() - exec_start,
                    )
                    res = context.build_result(
                        status="success",
                        final_summary=summary,
                    )
                    # v0.4.3-a: record success in ShadowMetrics so the supervisor's
                    # affinity-routing alternative-selection learns this shadow
                    # handles this intent_hash well.
                    _record_shadow_metric(shadow=shadow, scroll=scroll, status="success")
                    _dispatch_refinery(shadow, scroll, res, context, self.config, self.vault)
                    # v0.9.2: episodic capture — best-effort, never raises
                    _trigger_episodic_capture(
                        vault=getattr(self, 'vault', None),
                        config=getattr(self, 'config', None),
                        session_id=execution_id,
                        intent=getattr(scroll, "intent", ""),
                        chat_result=summary,
                        files_produced=[],
                        status="success",
                        execution_id=execution_id,
                    )
                    return res

                # ── FAIL ───────────────────────────────────────────────────────────
                elif action == "FAIL":
                    reason = decision.get("reason", "Unknown failure.")
                    logger.warning("[Runtime] Execution FAIL: %s", reason)
                    self._append_to_shadow_log(
                        shadow, execution_id, "failure", reason,
                        iteration_count=iteration, tool_calls_made=tool_call_count,
                        objectives_completed=len(completed_objectives) if use_objectives else current_ab - 1,
                        objectives_total=total_objectives if use_objectives else len(scroll_json),
                        duration_seconds=__import__("time").time() - exec_start,
                    )
                    res = context.build_result(
                        status="failure",
                        final_summary=f"Shadow reported failure: {reason}",
                        error=reason,
                    )
                    _record_terminal_telemetry(
                        shadow=shadow, execution_id=execution_id, scroll=scroll,
                        status="failure", iteration=iteration,
                    )
                    _dispatch_refinery(shadow, scroll, res, context, self.config, self.vault)
                    # v0.9.2: episodic capture — best-effort, never raises
                    _trigger_episodic_capture(
                        vault=getattr(self, 'vault', None),
                        config=getattr(self, 'config', None),
                        session_id=execution_id,
                        intent=getattr(scroll, "intent", ""),
                        chat_result=reason,
                        files_produced=[],
                        status="failure",
                        execution_id=execution_id,
                    )
                    return res

                # ── THINK ──────────────────────────────────────────────────────────
                elif action == "THINK":
                    thought = decision.get("thought", "")
                    context.add_thought(thought, current_ab)
                    logger.debug("[Runtime] THINK: %s", thought[:120])
                    # THINK is reasoning-only — it cannot credit objective completion.
                    # Only a successful TOOL_CALL result gates objective advancement.

                # ── REFLECT (v0.4.0-b) ─────────────────────────────────────────────
                # An explicit diagnosis step after a cluster of failures.  The LLM
                # names the strategy it intends to follow and may optionally
                # invoke ROLLBACK to rewind context to the last snapshot.  Treated
                # like a structured THINK: persists a thought and a sticky note,
                # does NOT credit objective completion, does NOT count as a fresh
                # THINK for the throttle (resets the consecutive count).
                elif action == "REFLECT":
                    strategy = (decision.get("strategy") or "").strip().upper()
                    rationale = (decision.get("rationale") or "").strip()
                    logger.info(
                        "[Runtime] REFLECT strategy=%s rationale=%s",
                        strategy or "(none)", rationale[:160],
                    )
                    context.add_thought(
                        f"REFLECT — strategy={strategy or 'unspecified'}: {rationale}"[:500],
                        current_ab,
                    )
                    # Sticky note survives a subsequent rollback so the LLM keeps
                    # memory of "we tried X, then we said we'd try Y" even after
                    # context is rewound.
                    if strategy:
                        context.add_sticky_note(
                            f"Reflected: chose strategy {strategy}"
                            + (f" — {rationale[:160]}" if rationale else "")
                        )
                    # Optional ROLLBACK in the same decision: lets the LLM combine
                    # "I'm changing strategy" with "and I want to rewind context".
                    if decision.get("rollback") or strategy == "ROLLBACK_AND_REPLAN":
                        rolled = context.rollback_to_last_snapshot()
                        if rolled is None:
                            context.add_thought(
                                "Requested rollback but no snapshot available — continuing forward.",
                                current_ab,
                            )
                        else:
                            # Replace any pending reflection so the rolled-back
                            # LLM sees a fresh post-rollback nudge, not stale.
                            context.queue_reflection_block(
                                "Context was rolled back to the last snapshot at your "
                                f"request.  Sticky notes above record what was tried.  "
                                f"Now choose a different approach than before."
                            )

                # ── LOAD_RESOURCE ──────────────────────────────────────────────────
                elif action == "LOAD_RESOURCE":
                    resource_type = decision.get("resource_type", "")
                    resource_id   = decision.get("resource_id", "")
                    md_content    = ""
                    load_error    = None

                    try:
                        if resource_type == "skill":
                            obj = self.vault.get_skill(resource_id)
                            if obj.skill_md_path and Path(obj.skill_md_path).exists():
                                md_content = Path(obj.skill_md_path).read_text(encoding="utf-8")
                            else:
                                md_content = f"# {obj.name}\n\n{obj.instructions_md or '_No instructions available._'}"
                        elif resource_type == "tool":
                            obj = self.vault.get_tool(resource_id)
                            if obj.tool_md_path and Path(obj.tool_md_path).exists():
                                md_content = Path(obj.tool_md_path).read_text(encoding="utf-8")
                            else:
                                md_content = f"# {obj.name}\n\n{obj.description}"
                        elif resource_type == "memory":
                            if resource_id == "global":
                                md_content = self.vault.load_global_memory()
                                if not md_content.strip():
                                    md_content = "# Global Memory\n\n_No global memory yet._"
                            else:
                                # resource_id == "self" or shadow id
                                md_path = shadow.memory_md_path
                                if md_path and Path(md_path).exists():
                                    md_content = Path(md_path).read_text(encoding="utf-8")
                                    resource_id = shadow.id
                                else:
                                    md_content = (
                                        f"# Memory: {shadow.name}\n\n"
                                        f"_No memory persisted yet — this shadow has not been consolidated._"
                                    )
                                    resource_id = shadow.id
                        else:
                            load_error = (
                                f"Unknown resource_type {resource_type!r}. "
                                f"Use 'skill', 'tool', or 'memory'."
                            )
                    except KeyError:
                        load_error = f"Resource {resource_type}/{resource_id!r} not found in vault."
                    except OSError as exc:
                        load_error = f"Could not read {resource_type} manifest for {resource_id!r}: {exc}"

                    if load_error:
                        context.add_observation({"error": load_error}, current_ab)
                    else:
                        context.add_resource_load(resource_type, resource_id, md_content, current_ab)
                        logger.debug("[Runtime] LOAD_RESOURCE %s/%s", resource_type, resource_id)
                        # v0.6.1-c: track loaded skills so _maybe_decay_loaded_skills
                        # knows which skills were in scope when failures hit.
                        if resource_type == "skill":
                            loaded = getattr(context, "_loaded_skill_ids", None)
                            if loaded is None:
                                loaded = set()
                                context._loaded_skill_ids = loaded
                            loaded.add(resource_id)

                # ── REQUEST_HARNESS / ASK_OPERATOR (v0.9.7 Reverse-Harness) ─────────
                # The inverse of TOOL_CALL: the agent asks the Governor to provision
                # a capability it lacks (forge a tool) or to ask the operator. Phase
                # 1: GRANT materialises inline + the new tool is offered to the
                # executor; DENY/ESCALATE return a structured observation (full
                # snapshot/suspend/resume operator round-trip is Phase 2).
                elif action in ("REQUEST_HARNESS", "ASK_OPERATOR"):
                    if not _intent_engine_enabled(self.config):
                        context.add_observation({
                            "type": "harness_disabled",
                            "message": "Capability provisioning is not enabled; use an available tool or FAIL.",
                        }, current_ab)
                        continue
                    try:
                        from systemu.runtime.governor import Governor
                        from systemu.core.models import (
                            HarnessRequest, HarnessKind, HarnessDecision,
                        )
                        if action == "ASK_OPERATOR":
                            _req = HarnessRequest(
                                kind=HarnessKind.INPUT,
                                spec={"question": decision.get("question") or decision.get("rationale", "")},
                                rationale=decision.get("rationale", ""),
                                fallback=decision.get("fallback", ""),
                            )
                        else:
                            try:
                                _hk = HarnessKind((decision.get("kind") or "tool").lower())
                            except Exception:
                                _hk = HarnessKind.TOOL
                            _req = HarnessRequest(
                                kind=_hk, spec=decision.get("spec") or {},
                                rationale=decision.get("rationale", ""),
                                fallback=decision.get("fallback", ""),
                            )
                        _gov = governor or Governor(self.config)
                        _verdict = _gov.arbitrate(_req)
                        if _verdict.decision == HarnessDecision.GRANT:
                            _mat = _gov.materialise(
                                _req, _verdict, vault=self.vault,
                                config=self.config, execution_id=execution_id,
                            )
                            if _mat.get("materialised"):
                                # v0.9.7 Phase 3: the Governor materialises one of
                                # several harness KINDs; apply each into THIS run.
                                if _mat.get("tool") is not None:
                                    # ── TOOL: resolve → deploy inline → offer back ──
                                    _tref = _mat.get("tool")
                                    _nt = None
                                    for _resolve in (
                                        lambda: self.vault.get_tool(_tref),
                                        lambda: self.vault.find_tool_by_name(_tref),
                                    ):
                                        try:
                                            _nt = _resolve()
                                            if _nt is not None:
                                                break
                                        except Exception:
                                            _nt = None
                                    # v0.9.7 Phase 2: deploy the freshly-forged tool
                                    # synchronously (dry-run → DEPLOYED + enabled) so it
                                    # is callable in THIS run, not just a future one.
                                    if _nt is not None and not getattr(_nt, "enabled", False):
                                        try:
                                            from systemu.pipelines.tool_deploy import deploy_forged_tool
                                            if deploy_forged_tool(_nt.id, self.vault, self.config).get("deployed"):
                                                try:
                                                    _nt = self.vault.get_tool(_nt.id)
                                                except Exception:
                                                    pass
                                        except Exception:
                                            logger.debug("[Runtime] forge-deploy failed", exc_info=True)
                                    if _nt is not None and getattr(_nt, "enabled", False):
                                        tools.append(_nt)
                                        tool_index.append({
                                            "id": _nt.id, "name": _nt.name,
                                            "description": _nt.description,
                                            "parameter_names": list(getattr(_nt, "parameter_names", []) or []),
                                            "parameters_schema": dict(getattr(_nt, "parameters_schema", {}) or {}),
                                        })
                                        context.add_observation({
                                            "type": "harness_granted",
                                            "message": f"Capability provisioned and ready: '{_nt.name}'. You may call it now.",
                                            "tool": _nt.name,
                                        }, current_ab)
                                    else:
                                        context.add_observation({
                                            "type": "harness_granted_pending",
                                            "message": (
                                                f"Capability '{getattr(_nt, 'name', _tref)}' was forged but is pending "
                                                "enablement/dry-run; it is not callable this run. Use an existing tool or FAIL."
                                            ),
                                        }, current_ab)
                                elif _mat.get("compute_grant"):
                                    # ── COMPUTE: extend THIS run's iteration budget ──
                                    _cg = _mat.get("compute_grant") or {}
                                    _extra_it = max(0, min(int(_cg.get("extra_iterations", 0) or 0), 100))
                                    if _extra_it:
                                        _iter_budget += _extra_it
                                    context.add_observation({
                                        "type": "harness_granted",
                                        "message": (
                                            f"Compute granted: +{_extra_it} iteration(s) "
                                            f"(budget now {_iter_budget}). Continue toward the goal."
                                        ),
                                    }, current_ab)
                                elif _mat.get("skill"):
                                    # ── SKILL: procedure authored to the vault ──
                                    context.add_observation({
                                        "type": "harness_granted",
                                        "message": (
                                            f"Skill provisioned: {_mat.get('skill')}. Its procedure is now "
                                            "available — follow it to complete the task."
                                        ),
                                    }, current_ab)
                                elif _mat.get("access"):
                                    # ── ACCESS: a scoped capability lease was granted ──
                                    context.add_observation({
                                        "type": "harness_granted",
                                        "message": (
                                            f"Access granted (scoped lease): {_mat.get('access')}. "
                                            "Proceed with the operation it authorizes."
                                        ),
                                    }, current_ab)
                                elif _mat.get("subagent"):
                                    # ── SUBAGENT: delegation capability granted ──
                                    _sa = _mat.get("subagent") or {}
                                    context.add_observation({
                                        "type": "harness_granted",
                                        "message": (
                                            "Sub-agent delegation granted for: "
                                            f"{str(_sa.get('task', ''))[:160]}. Decompose and proceed within "
                                            "the granted depth/budget."
                                        ),
                                    }, current_ab)
                                else:
                                    context.add_observation({
                                        "type": "harness_granted",
                                        "message": "Capability provisioned. Proceed toward the goal.",
                                    }, current_ab)
                            else:
                                context.add_observation({
                                    "type": "harness_grant_failed",
                                    "message": f"Provisioning failed: {_mat.get('reason')}. {_req.fallback or 'Try an alternative or FAIL.'}",
                                }, current_ab)
                        else:
                            # ESCALATE / DENY. v0.9.7 Phase 2: an ESCALATE surfaces an
                            # operator decision card (the operator can approve/deny it
                            # on the Pending Actions tab). The agent is told to proceed
                            # with its fallback meanwhile. The full async suspend →
                            # resume_after_grant → resume-injection round-trip is the
                            # remaining P2.3 integration (building blocks committed:
                            # supervisor.resume_after_grant + the suspend contract +
                            # surface_harness_request).
                            if _verdict.decision == HarnessDecision.ESCALATE:
                                try:
                                    from systemu.interface.harness_review import surface_harness_request
                                    _did = surface_harness_request(
                                        _req, _verdict, execution_id=execution_id, vault=self.vault,
                                    )
                                    logger.info("[Runtime] harness ESCALATE surfaced to operator: %s", _did)
                                except Exception:
                                    logger.debug("[Runtime] surface_harness_request failed", exc_info=True)
                            _alts = ", ".join(_verdict.alternatives) if _verdict.alternatives else ""
                            context.add_observation({
                                "type": "harness_" + _verdict.decision.value,
                                "message": (
                                    f"Harness request {_verdict.decision.value}: {_verdict.rationale}. "
                                    f"{('Alternatives: ' + _alts + '. ') if _alts else ''}"
                                    f"{_req.fallback or 'Proceed with an alternative approach or FAIL.'}"
                                    + (" (An operator approval card was raised; proceed with your fallback meanwhile.)"
                                       if _verdict.decision == HarnessDecision.ESCALATE else "")
                                ),
                            }, current_ab)
                    except Exception:
                        logger.debug("[Runtime] REQUEST_HARNESS handling errored", exc_info=True)
                        context.add_observation({
                            "type": "harness_error",
                            "message": "Harness request could not be processed; proceed with available tools or FAIL.",
                        }, current_ab)

                # ── TOOL_CALL ──────────────────────────────────────────────────────
                elif action == "TOOL_CALL":
                    result = await self._handle_tool_call(
                        decision, tools, context, current_ab, dry_run,
                        shadow=shadow, execution_id=execution_id,
                    )
                    if result is None:
                        continue   # User denied destructive call
                    tool_call_count += 1

                    # v0.8.21: stuck-guard — record tool outcome.
                    # (objective-credit reset is applied below at the credit site.)
                    self._update_stuck_counters(
                        action="TOOL_CALL",
                        tool_name=decision.get("tool_name") or "?",
                        tool_success=bool(getattr(result, "success", False)),
                        credited_obj_id=None,
                    )

                    # v0.9.7: deterministic loop-guard — signature is
                    # (tool, args, success-class), so a tool repeatedly called
                    # with the same args and the same outcome escalates to a
                    # corrective nudge (warn) then a forced finish (block) on the
                    # NEXT iteration. Never let it crash the loop.
                    try:
                        _lg = loop_guard.record(
                            decision.get("tool_name", "") or "",
                            decision.get("parameters") or {},
                            bool(getattr(result, "success", False)),
                        )
                        if _lg:
                            loop_guard_nudge = _lg
                    except Exception:
                        logger.debug("[Runtime] loop_guard.record failed", exc_info=True)

                    # v0.8.16: per-iteration detail event AFTER the tool runs, so
                    # the bounded `details` dict carries the tool result the live
                    # panes render on expand.  Raw LLM is referenced via llm_ref.
                    try:
                        from systemu.interface.event_bus import EventBus
                        _tool_result_for_event = (
                            result.parsed if getattr(result, "parsed", None) is not None
                            else getattr(result, "output", None) or result
                        )
                        EventBus.get().publish(self._iteration_event(
                            iteration=iteration,
                            decision=decision,
                            tool_result=_tool_result_for_event,
                            execution_id=execution_id,
                            llm_ref=_last_llm_ref,
                        ))
                    except Exception:
                        pass  # EventBus is optional — never break execution

                    # v0.8.17: fail-fast after 3 consecutive degraded web-search results.
                    # "degraded" means the entire provider chain failed (not just zero results) —
                    # reset on any non-degraded search so a single blip doesn't end the run.
                    _tool_name_for_ff = decision.get("tool_name", "")
                    _parsed_for_ff = getattr(result, "parsed", None)
                    if _is_degraded_search_result(_tool_name_for_ff, _parsed_for_ff):
                        self._consec_degraded_search += 1
                    else:
                        self._consec_degraded_search = 0
                    if self._consec_degraded_search >= _MAX_CONSEC_DEGRADED_SEARCH:
                        _ff_msg = (
                            f"Web search capability unavailable — search backends failed "
                            f"{self._consec_degraded_search}x. Set SYSTEMU_TAVILY_API_KEY or "
                            f"SYSTEMU_EXA_API_KEY for reliable search."
                        )
                        logger.warning("[Runtime] fail-fast: %s", _ff_msg)
                        self._append_to_shadow_log(
                            shadow, execution_id, "failure", _ff_msg,
                            iteration_count=iteration, tool_calls_made=tool_call_count,
                            objectives_completed=len(completed_objectives) if use_objectives else current_ab - 1,
                            objectives_total=total_objectives if use_objectives else len(scroll_json),
                            duration_seconds=__import__("time").time() - exec_start,
                        )
                        _ff_res = context.build_result(
                            status="failure",
                            final_summary=f"Shadow reported failure: {_ff_msg}",
                            error=_ff_msg,
                        )
                        _record_terminal_telemetry(
                            shadow=shadow, execution_id=execution_id, scroll=scroll,
                            status="failure", iteration=iteration,
                        )
                        _dispatch_refinery(shadow, scroll, _ff_res, context, self.config, self.vault)
                        return _ff_res

                    # v0.6.9: iteration-loop circuit breaker — bail when the LLM
                    # is stuck re-invoking the same broken tool with the same
                    # failure class. Saves 20+ wasted iterations on a recoverable
                    # blocker (op needs to use the recovery URL).
                    if not result.success:
                        cb_tool_name = decision.get("tool_name", "") or "?"
                        cb_reason = (
                            (result.parsed or {}).get("error_type")
                            or (result.parsed or {}).get("classified_reason")
                            or "TOOL_FAILED"
                        )
                        tripped = self._record_tool_failure(cb_tool_name, cb_reason)
                        if tripped:
                            logger.warning(
                                "[Runtime] v0.6.9 circuit breaker tripped: "
                                "tool=%s reason=%s after %d consecutive failures",
                                cb_tool_name, cb_reason, self.CIRCUIT_BREAKER_FAILURES,
                            )
                            return {
                                "status": "failure",
                                "final_summary": (
                                    f"Circuit breaker: tool {cb_tool_name} failed "
                                    f"{self.CIRCUIT_BREAKER_FAILURES} consecutive "
                                    f"times with reason {cb_reason}. Apply the fix at "
                                    f"the recovery URL surfaced in prior iterations."
                                ),
                                "execution_id": execution_id,
                            }

                    if use_objectives:
                        # Credit objective only when the tool actually succeeded.
                        # A failed tool (result.success=False) cannot advance the objective —
                        # the shadow must try again or choose a different approach.
                        completed_obj = decision.get("completes_objective")
                        if isinstance(completed_obj, int) and completed_obj not in completed_objectives:
                            if result is not None and result.success:
                                # v0.9.1 (Layer 4): run the durable-outcome verifier before
                                # crediting.  Best-effort: verifier errors fall through to the
                                # legacy credit so a bad verifier config can't stall the run.
                                _do_credit = True
                                try:
                                    _obj_for_verify = next(
                                        (o for o in objectives if o.id == completed_obj), None)
                                    if _obj_for_verify is not None:
                                        _vstate = self._objective_states.setdefault(
                                            completed_obj, ObjectiveState())
                                        _v_outcome = process_completion_claim(
                                            objective=_obj_for_verify,
                                            vault=self.vault,
                                            config=self.config,
                                            execution_id=execution_id,
                                            default_output_dir=_resolve_verifier_output_dir(
                                                self.config, getattr(self, "user_profile", None)
                                            ),
                                            chat_result=None,
                                            state=_vstate,
                                            fresh_work_since_last_call=self._fresh_work_since_last_verifier_call,
                                            user_id=None,
                                        )
                                        self._objective_states[completed_obj] = _v_outcome.state
                                        self._fresh_work_since_last_verifier_call = False
                                        _do_credit = _v_outcome.credited
                                        if not _v_outcome.credited:
                                            if _v_outcome.bypassed_verifier:
                                                logger.debug(
                                                    "[Runtime] Verifier per-turn cap hit for obj=%d "
                                                    "— bypassed, not crediting this turn.",
                                                    completed_obj,
                                                )
                                            else:
                                                logger.warning(
                                                    "[Runtime] Verifier rejected obj=%d: %s",
                                                    completed_obj, _v_outcome.feedback_message,
                                                )
                                            if _v_outcome.feedback_message:
                                                context.add_observation(
                                                    {
                                                        "type": "verifier_rejection",
                                                        "objective_id": completed_obj,
                                                        "message": _v_outcome.feedback_message,
                                                    },
                                                    current_ab,
                                                )
                                            if _v_outcome.escalate_stuck:
                                                # Treat budget-exceeded rejection as a stuck event.
                                                self._iters_since_obj_credit = max(
                                                    self._iters_since_obj_credit,
                                                    _stuck_thresholds()[0],
                                                )
                                except Exception:
                                    logger.debug(
                                        "[Runtime] v0.9.1 verifier hook crashed — crediting without verify",
                                        exc_info=True,
                                    )

                                if _do_credit:
                                    completed_objectives.add(completed_obj)
                                    # v0.8.19 (R2): publish updated objective_state so the
                                    # live pane ticks the checklist.  Best-effort.
                                    try:
                                        from systemu.interface.event_bus import EventBus
                                        EventBus.get().publish(_objective_state_event(
                                            objectives, completed_objectives, execution_id, stamp=self._stamp))
                                    except Exception:
                                        pass
                                    # v0.8.21: stuck-guard — credit resets BOTH counters.
                                    self._update_stuck_counters(
                                        action="TOOL_CALL",
                                        tool_name=decision.get("tool_name") or "?",
                                        tool_success=True,
                                        credited_obj_id=completed_obj,
                                    )
                                    logger.info("[Runtime] Objective %d complete. %d/%d done.",
                                                completed_obj, len(completed_objectives), total_objectives)

                                    if (len(completed_objectives) % SNAPSHOT_INTERVAL) == 0:
                                        context.take_snapshot(len(completed_objectives), self.config)

                                    if len(completed_objectives) >= total_objectives:
                                        logger.info("[Runtime] All objectives complete via advancement.")
                                        self._append_to_shadow_log(
                                            shadow, execution_id, "success", "All objectives completed.",
                                            iteration_count=iteration, tool_calls_made=tool_call_count,
                                            objectives_completed=len(completed_objectives),
                                            objectives_total=total_objectives,
                                            duration_seconds=__import__("time").time() - exec_start,
                                        )
                                        res = context.build_result(
                                            status="success",
                                            final_summary="All objectives completed successfully.",
                                        )
                                        _record_shadow_metric(shadow=shadow, scroll=scroll, status="success")
                                        _dispatch_refinery(shadow, scroll, res, context, self.config, self.vault)
                                        return res
                            else:
                                logger.warning(
                                    "[Runtime] TOOL_CALL claimed completes_objective=%d "
                                    "but tool failed (success=%s) — objective NOT credited.",
                                    completed_obj,
                                    result.success if result is not None else None,
                                )
                    else:
                        # Legacy ActionBlock completion tracking
                        completed_ab = decision.get("completes_action_block")
                        if isinstance(completed_ab, int) and completed_ab >= current_ab:
                            current_ab = completed_ab + 1
                            logger.info("[Runtime] Advanced to ActionBlock %d", current_ab)

                            if (current_ab - last_snap_ab) >= SNAPSHOT_INTERVAL:
                                context.take_snapshot(completed_ab, self.config)
                                last_snap_ab = completed_ab

                            if current_ab > len(scroll_json):
                                logger.info("[Runtime] All ActionBlocks complete via advancement.")
                                self._append_to_shadow_log(
                                    shadow, execution_id, "success", "All steps completed.",
                                    iteration_count=iteration, tool_calls_made=tool_call_count,
                                    objectives_completed=current_ab - 1, objectives_total=len(scroll_json),
                                    duration_seconds=__import__("time").time() - exec_start,
                                )
                                res = context.build_result(
                                    status="success",
                                    final_summary="All ActionBlocks completed successfully.",
                                )
                                _record_shadow_metric(shadow=shadow, scroll=scroll, status="success")
                                _dispatch_refinery(shadow, scroll, res, context, self.config, self.vault)
                                return res

                    # v0.8.21: stuck-guard — check after this iteration's effects are recorded.
                    triggered, reason = self._stuck_trigger()
                    if triggered and use_objectives:
                        # v0.9.7 intent-engine: before parking on per-objective stuck,
                        # accept on GOAL-level success. The per-objective contract is
                        # fragile (impl-path / path-mangling / delta-timing all reject
                        # legitimate work); the goal verifier (epoch baseline) just
                        # checks whether the GOAL's artifact exists. Default OFF.
                        if _intent_engine_enabled(self.config) and _adherence != "strict" and _intent_goal_success(
                                vault=self.vault, config=self.config,
                                user_profile=getattr(self, "user_profile", None),
                                scroll=scroll, execution_id=execution_id,
                                summary=(decision.get("summary") if isinstance(decision, dict) else None)):
                            logger.info(
                                "[Runtime] intent-engine: goal met at stuck-park — "
                                "finalizing SUCCESS instead of parking (%d/%d objectives credited).",
                                len(completed_objectives), total_objectives,
                            )
                            self._append_to_shadow_log(
                                shadow, execution_id, "success",
                                "Goal completed (goal-level verification at stuck-park).",
                                iteration_count=iteration, tool_calls_made=tool_call_count,
                                objectives_completed=len(completed_objectives),
                                objectives_total=total_objectives,
                                duration_seconds=__import__("time").time() - exec_start,
                            )
                            res = context.build_result(
                                status="success",
                                final_summary="Goal completed (verified at goal level).",
                            )
                            _revoke_harness_leases()
                            _record_shadow_metric(shadow=shadow, scroll=scroll, status="success")
                            _dispatch_refinery(shadow, scroll, res, context, self.config, self.vault)
                            return res
                        # which objective are we stuck on? the first pending (lowest id whose deps are met & not done)
                        pending = [o for o in objectives if o.id not in completed_objectives
                                   and all(d in completed_objectives for d in (o.depends_on or []))]
                        stuck_obj = pending[0] if pending else objectives[-1]
                        # v0.8.22.1 (Fix 4): exclude tools that ultimately succeeded.
                        # _update_progress_counters sets a succeeded tool's streak to 0
                        # but keeps the key; only tools with an active failure streak
                        # belong in the operator's "tools tried" line.
                        tools_tried = sorted(k for k, v in self._same_tool_fail_streak.items() if v > 0)
                        # v0.8.22.1 (R1): persist a resume snapshot at stuck-park so
                        # the operator's answer (via the daemon re-dispatch handler)
                        # can resume this run with completed objectives intact.
                        try:
                            from systemu.runtime.execution_snapshot import (
                                capture_from_context, write_snapshot,
                            )
                            _snap = capture_from_context(
                                execution_id=execution_id,
                                shadow_id=getattr(shadow, "id", ""),
                                scroll_id=getattr(scroll, "id", ""),
                                iteration=iteration,
                                current_action_block=current_ab,
                                completed_objectives=set(completed_objectives),
                                context=context,
                                activity_id=getattr(activity, "id", ""),
                            )
                            # R3: persist the per-objective stuck round counters so
                            # rounds accumulate across resumes (carried as a sticky tag).
                            import json as _json
                            _snap.sticky_notes.append(
                                "__STUCK_ROUNDS__::" + _json.dumps(self._stuck_round_for_obj)
                            )
                            write_snapshot(_snap)
                        except Exception:
                            logger.debug("[Runtime] stuck-park snapshot failed", exc_info=True)
                        ans = self._ask_stuck_or_degrade(execution_id=execution_id,
                                                           current_objective=stuck_obj,
                                                           tools_tried=tools_tried, reason=reason,
                                                           scroll_id=getattr(scroll, "id", ""),
                                                           activity_id=getattr(activity, "id", ""),
                                                           shadow_id=getattr(shadow, "id", ""))
                        if ans is None:
                            # headless — degrade as 'partial' (closest to MaxIterations semantics)
                            return self._finalize_stuck(context=context, status="partial",
                                                         reason=reason, stuck_on=stuck_obj.id,
                                                         completed=list(completed_objectives),
                                                         iteration=iteration,
                                                         tool_calls_made=tool_call_count,
                                                         scroll=scroll, shadow=shadow,
                                                         execution_id=execution_id,
                                                         exec_start=exec_start,
                                                         total_objectives=total_objectives)
                        from functools import partial as _partial
                        _fin = _partial(self._finalize_stuck, context=context,
                                        reason=reason, stuck_on=stuck_obj.id,
                                        completed=list(completed_objectives),
                                        iteration=iteration, tool_calls_made=tool_call_count,
                                        scroll=scroll, shadow=shadow,
                                        execution_id=execution_id, exec_start=exec_start,
                                        total_objectives=total_objectives)
                        _action, _res = self._apply_stuck_answer(stuck_obj, ans, finalize=_fin)
                        if _action == "finalize":
                            return _res
                        # else "continue": hint applied, loop proceeds

                else:
                    logger.warning("[Runtime] Unknown action: %s — treating as THINK", action)
                    context.add_thought(f"Unrecognised action type: {action}", current_ab)

            # ── Max iterations hit ─────────────────────────────────────────────────
            logger.warning("[Runtime] Max iterations (%d) reached without COMPLETE.", MAX_ITERATIONS)
            self._append_to_shadow_log(
                shadow, execution_id, "partial",
                f"Reached max iterations ({MAX_ITERATIONS}).",
                iteration_count=iteration, tool_calls_made=tool_call_count,
                objectives_completed=len(completed_objectives) if use_objectives else current_ab - 1,
                objectives_total=total_objectives if use_objectives else len(scroll_json),
                duration_seconds=__import__("time").time() - exec_start,
            )
            _record_terminal_telemetry(
                shadow=shadow, execution_id=execution_id, scroll=scroll,
                status="partial", iteration=iteration,
                extra={"reason": "MaxIterationsExceeded"},
            )
            return context.build_result(
                status="partial",
                final_summary=f"Execution reached max iterations ({MAX_ITERATIONS}). Task may be incomplete.",
                error="MaxIterationsExceeded",
            )
        finally:
            try:
                from systemu.runtime.chat_submission_ctx import set_chat_submission_id
                set_chat_submission_id(None, reset_token=self._chat_submission_token)
            except Exception:
                pass

    # ─── Private helpers ──────────────────────────────────────────────────────

    async def _handle_tool_call(
        self,
        decision:    Dict[str, Any],
        tools:       List[Tool],
        context:     ExecutionContext,
        current_ab:  int,
        dry_run:     bool,
        *,
        shadow:      Optional[Shadow] = None,
        execution_id: Optional[str] = None,
    ) -> Optional[ToolResult]:
        """Execute a TOOL_CALL decision. Returns None if user denied.

        ``shadow`` + ``execution_id`` are optional for backward-compatibility
        with any older test paths that call the method directly, but the
        runtime's own call site supplies both so the memory invalidator
        can write contradicting lessons when a previously-failed tool now
        succeeds.
        """
        tool_name  = decision.get("tool_name", "")
        # Extract args, tolerating aliases that some LLMs emit instead of
        # the prompt-specified `parameters` key.  Observed: Deepseek
        # occasionally uses `args` or `inputs`; left unhandled this leads
        # to every tool call running with no kwargs and failing its
        # required-arg guard.
        parameters = decision.get("parameters")
        if not parameters:
            for alias in ("args", "inputs", "kwargs", "input", "arguments"):
                if decision.get(alias):
                    parameters = decision[alias]
                    logger.warning(
                        "[Runtime] LLM used '%s' key instead of 'parameters' "
                        "for tool=%s — accepting; prompt may need clarification",
                        alias, tool_name,
                    )
                    break
        parameters = parameters or {}
        # v0.9.7: some LLMs emit a bare scalar (e.g. a URL string) instead of a
        # {param: value} dict for single-argument tools. Without this guard the
        # next line crashes on ``parameters.keys()`` (AttributeError on str).
        if not isinstance(parameters, dict):
            _orig_params = parameters
            parameters = _coerce_scalar_parameter(parameters, tool_name, tools)
            logger.warning(
                "[Runtime] tool=%s received non-dict parameters %r — coerced to %s",
                tool_name, _orig_params, list(parameters.keys()) or "{}",
            )
        logger.debug("[Runtime] TOOL_CALL tool=%s args=%s",
                     tool_name, list(parameters.keys()))
        reasoning  = decision.get("reasoning", "")
        is_destructive = decision.get("is_destructive", False)

        # Fallback heuristic check
        if not is_destructive:
            is_destructive = ToolSandbox.is_destructive_call(tool_name, parameters)

        # Safety gate for destructive calls
        if is_destructive and not dry_run:
            approved = confirm(
                f"Shadow '{self.vault}' wants to perform a potentially destructive action:\n"
                f"  Tool: {tool_name}\n"
                f"  Params: {json.dumps(parameters)}\n"
                f"  Reason: {reasoning}\n"
                "  Allow?",
                default=False,
            )
            if not approved:
                context.add_observation(
                    {"type": "user_denied", "tool_name": tool_name,
                     "message": "User denied this destructive action."},
                    current_ab,
                )
                return None

        # Find the Tool object
        tool_obj = next((t for t in tools if t.name == tool_name), None)
        if tool_obj is None:
            obs = {"error": f"Tool '{tool_name}' not found in available tools."}
            context.add_observation(obs, current_ab)
            return None

        # Record the call
        context.add_tool_call(decision, current_ab)

        # DRY RUN — skip actual execution
        if dry_run:
            fake_result = ToolResult(
                success=True,
                parsed={"dry_run": True, "tool": tool_name, "params": parameters},
            )
            context.add_observation(fake_result.to_dict(), current_ab)
            logger.debug("[Runtime] DRY RUN: %s(%s)", tool_name, parameters)
            return fake_result

        # LIVE — execute in sandbox
        if not tool_obj.implementation_path:
            obs = {"error": f"Tool '{tool_name}' has no implementation (status: {tool_obj.status})."}
            context.add_observation(obs, current_ab)
            return None

        # Suppress retries for tools that already failed with a dep error in
        # THIS run — but first re-check whether the blocking packages are now
        # approved (v0.3.6 no-restart fix).  When all are approved, drop the
        # suppression and let the actual call proceed; the registry's
        # self-heal path will install + retry the import.
        if tool_name in self._dep_failed_tools:
            blocking = self._dep_failed_tools.get(tool_name) or []
            cleared  = self._maybe_clear_dep_suppression(tool_name, blocking)
            if not cleared:
                obs = {
                    "success":    False,
                    "error":      (f"Tool '{tool_name}' is permanently unavailable this run: "
                                   f"a required Python package is missing. "
                                   f"Do not retry — check Notifications for install instructions."),
                    "error_type": "missing_dependency",
                }
                context.add_observation(obs, current_ab)
                return ToolResult(success=False, parsed=obs, error=obs["error"])

        result = await self.sandbox.execute_tool(
            tool_obj.implementation_path,
            parameters,
            extra_packages=tool_obj.dependencies or [],
            tool_type=getattr(tool_obj.tool_type, "value", tool_obj.tool_type),
        )

        # v0.9.1 (T8 must-wire): apply max_result_size_chars truncation.
        # truncate_result is a module-level function in tool_sandbox; it is a
        # no-op when tool_obj.max_result_size_chars is None.
        try:
            from systemu.runtime.tool_sandbox import truncate_result as _truncate_result
            result = _truncate_result(result, tool_obj)
        except Exception:
            logger.debug("[Runtime] truncate_result hook skipped", exc_info=True)

        # Detect dependency-related result types and suppress retries.
        # Four error_types map to a single behaviour ("don't call this tool
        # again in this run") but trigger distinct operator-facing event-log
        # lines so the action to take is unambiguous.
        error_type = result.parsed.get("error_type") if result.parsed else None
        if error_type in (
            "missing_dependency",
            "dependency_install_blocked",
            "dependency_install_pending_approval",
            "dependency_install_failed",
        ):
            missing_list = _resolve_missing_packages(
                result.parsed.get("missing_packages"),
                list(getattr(tool_obj, "dependencies", []) or []),
            )
            hint = result.parsed.get("install_hint") or _install_hint(missing_list)
            # Remember the EXACT packages that blocked this tool so we can
            # clear the suppression precisely when they're approved.
            self._dep_failed_tools[tool_name] = list(missing_list)

            llm_msg, op_msg, op_level = _dep_failure_messages(
                tool_name=tool_name,
                error_type=error_type,
                missing_packages=missing_list,
                hint=hint,
                pip_tail=result.parsed.get("pip_stderr_tail"),
            )

            enriched_obs = dict(result.to_dict())
            enriched_obs["error"] = llm_msg
            context.add_observation(enriched_obs, current_ab)

            try:
                log_event(
                    op_level, "tool",
                    op_msg,
                    {
                        "tool_name":        tool_name,
                        "error_type":       error_type,
                        "missing_packages": missing_list,
                        "install_hint":     hint,
                        "origin":           getattr(self, "_origin", "manual"),
                    },
                )
            except Exception:
                pass
            logger.warning(
                "[Runtime] Tool '%s' dep issue (%s, pkgs=%s) — suppressing retries for this run",
                tool_name, error_type, missing_list,
            )
            return ToolResult(success=False, parsed=enriched_obs, error=enriched_obs["error"])

        context.add_observation(result.to_dict(), current_ab)

        if not result.success:
            logger.warning(
                "[Runtime] Tool %s failed: %s", tool_name, result.error or result.stderr[:500]
            )
            # v0.4.0-0: structured telemetry so we can build a real failure-mode
            # histogram before designing the supervisor.  Best-effort: telemetry
            # write failures are swallowed inside the module.
            try:
                from systemu.runtime.failure_telemetry import record_tool_failure
                error_type = None
                if result.parsed:
                    error_type = result.parsed.get("error_type")
                record_tool_failure(
                    shadow_id=(shadow.id if shadow is not None else None),
                    execution_id=execution_id,
                    tool_name=tool_name,
                    error_type=error_type,
                    error=result.error or (result.stderr[:500] if result.stderr else None),
                    extra={
                        "exit_code":      result.exit_code,
                        "timed_out":      result.timed_out,
                        "missing_packages": (result.parsed or {}).get("missing_packages"),
                    },
                )
            except Exception:
                logger.debug("[Runtime] telemetry write skipped", exc_info=True)

            # v0.4.4-a: tool-level metrics (per-tool lifetime success rate).
            # Used for operator visibility + Evolution proposals when tools
            # have chronically low success rates.  Keyed by tool_id so cross-
            # shadow signal accumulates.  Dependency-blocked failures are
            # tracked separately and excluded from the success-rate
            # denominator (those reflect the install env, not the tool).
            try:
                from systemu.runtime.tool_metrics import get_tool_metrics
                err_type_for_metrics = (result.parsed or {}).get("error_type") if result.parsed else None
                get_tool_metrics().record(
                    tool_id=getattr(tool_obj, "id", "") or tool_name,
                    success=False,
                    error_type=err_type_for_metrics,
                    timed_out=bool(result.timed_out),
                )
            except Exception:
                logger.debug("[Runtime] tool_metrics record skipped", exc_info=True)
            # v0.9.3: capability ledger — record failed invocation.
            try:
                self.sandbox._record_capability_outcome(
                    tool=tool_obj,
                    success=False,
                    error=str(result.error or result.stderr[:200] if result.stderr else result.error or ""),
                )
            except Exception:
                logger.debug("[Runtime] capability ledger (failure) skipped", exc_info=True)

            # v0.4.0-b: in-loop reflection.  Classify cheaply, count
            # consecutive failures for THIS tool, and queue a reflection
            # block for the next iteration.  After 3 consecutive failures,
            # the block explicitly forces a strategy choice via REFLECT.
            cls = None
            consec = 0
            try:
                from systemu.runtime.failure_classifier import (
                    classify_tool_result, reflection_strategies_for,
                )
                cls = classify_tool_result(result)
                self._consec_tool_fails[tool_name] = (
                    self._consec_tool_fails.get(tool_name, 0) + 1
                )
                consec = self._consec_tool_fails[tool_name]

                # v0.6.1-c: decay loaded-skill effectiveness on this failure.
                # Threshold-crossing queues RECALIBRATE_SKILL on pending_directives.
                try:
                    _maybe_decay_loaded_skills(
                        context, vault=self.vault, status="failure",
                    )
                except Exception:
                    logger.debug(
                        "[Runtime] skill decay hook crashed (per-iteration failure)",
                        exc_info=True,
                    )
                strategies = list(reflection_strategies_for(cls.category))
                force_reflect = consec >= 3
                block = _build_reflection_block(
                    tool_name=tool_name,
                    category=cls.category,
                    keyword=cls.keyword,
                    consec=consec,
                    strategies=strategies,
                    force_reflect=force_reflect,
                )
                context.queue_reflection_block(block)
            except Exception:
                logger.debug("[Runtime] reflection injection skipped", exc_info=True)

            # v0.4.0-d: notify Intelligent Supervisor of this failure so it
            # can decide whether to layer additional intervention on top of
            # the rule-based reflection block already queued above.
            mind = getattr(self, "_execution_mind", None)
            if mind is not None and mind.enabled:
                try:
                    mind.evaluate(
                        trigger="tool_failure",
                        recent_events=_build_history_slice(context, max_events=3),
                        classifier=(cls.category if cls else None),
                        consec_failures=consec,
                        iteration=0,  # exact iteration unknown at this seam; supervisor records the count of failures instead
                    )
                except Exception:
                    logger.debug("[Runtime] supervisor evaluate failed", exc_info=True)
        else:
            # Reset the per-tool failure counter on success.
            self._consec_tool_fails.pop(tool_name, None)
            # v0.9.1 (Layer 4): mark that fresh effectful work has landed so the
            # verifier per-turn cap clears for the next completion claim.
            self._fresh_work_since_last_verifier_call = True
            # v0.9.1 (final-review fix): invoke action-tool audit hook.
            # _after_successful_call was implemented in T8 (tool_sandbox) but
            # never called from production; without this wire, action-tool audit
            # is dead code and audit_log verifier hints always return verified=False.
            try:
                self.sandbox._after_successful_call(
                    tool=tool_obj,
                    params=parameters or {},
                    execution_id=execution_id,
                    objective_id=int(decision.get("completes_objective") or 0),
                    user_id=None,
                )
            except Exception:
                logger.debug("[Runtime] action-audit hook skipped", exc_info=True)
            # v0.9.3: capability ledger — record successful invocation.
            try:
                self.sandbox._record_capability_outcome(
                    tool=tool_obj, success=True, error=None,
                )
            except Exception:
                logger.debug("[Runtime] capability ledger (success) skipped", exc_info=True)
            # v0.4.4-a: record success in tool metrics.
            try:
                from systemu.runtime.tool_metrics import get_tool_metrics
                get_tool_metrics().record(
                    tool_id=getattr(tool_obj, "id", "") or tool_name,
                    success=True,
                )
            except Exception:
                logger.debug("[Runtime] tool_metrics record (success) skipped", exc_info=True)
            # v0.5.0-a: capture successful params for the backward-compat
            # replay used by RECALIBRATE_TOOL's bump-version path.  Rolling
            # buffer capped at 20 entries; secret-like keys redacted.
            try:
                from systemu.pipelines.tool_dry_run import record_successful_params
                record_successful_params(tool_obj, parameters or {}, self.vault)
            except Exception:
                logger.debug("[Runtime] last_successful_params capture skipped", exc_info=True)

        # v0.3.4: On a successful tool call, check whether this tool was
        # previously gated by a missing-dep failure (either earlier in
        # *this* run via ``_dep_failed_tools``, or in a prior run that
        # left a stale ``failure_patterns`` lesson in the shadow's buffer).
        # When so, append a contradicting memory entry so the consolidator
        # downweights the obsolete "switch formats" advice.
        if result.success and shadow is not None:
            previously_failed_in_run = tool_name in self._dep_failed_tools
            try:
                from systemu.runtime.memory_invalidator import maybe_invalidate_dep_lesson
                maybe_invalidate_dep_lesson(
                    self.vault, shadow, tool_name,
                    previously_failed=previously_failed_in_run,
                    execution_id=execution_id,
                )
            except Exception:
                # Never let memory bookkeeping crash an execution.
                logger.debug("[Runtime] memory invalidation hook errored", exc_info=True)

        return result

    def _maybe_clear_dep_suppression(self, tool_name: str, blocking: List[str]) -> bool:
        """Re-check whether every blocking package is now approved.

        When all are approved (operator clicked ✓ since this tool last
        failed), drop the suppression so the next call attempts the tool
        again.  Returns True when the suppression was cleared.

        Reads the approval store via the ToolSandbox's already-resolved
        ``_approvals`` reference so we re-use the same store the
        registry consults — and so the read picks up out-of-process
        mutations (v0.3.6 store re-reads on every check).
        """
        if not blocking:
            self._dep_failed_tools.pop(tool_name, None)
            return True
        approvals = getattr(self.sandbox, "_approvals", None)
        if approvals is None:
            return False
        try:
            all_approved = all(approvals.is_approved(p) for p in blocking)
        except Exception:
            logger.debug("[Runtime] approval re-check failed", exc_info=True)
            return False
        if all_approved:
            logger.info(
                "[Runtime] Dep suppression cleared for tool '%s' — all blocking "
                "packages now approved: %s", tool_name, blocking,
            )
            self._dep_failed_tools.pop(tool_name, None)
            return True
        return False

    def _load_skills(self, skill_ids: List[str]) -> List[Skill]:
        """Load all skills required by the activity."""
        skills = []
        for sid in skill_ids:
            try:
                skills.append(self.vault.get_skill(sid))
            except KeyError:
                logger.warning("[Runtime] Skill %s not found in vault", sid)
        return skills

    def _load_tools(self, tool_ids: List[str], *, dry_run: bool = False) -> List[Tool]:
        """Load tool objects that are ready for execution.

        Normal run : DEPLOYED, TESTED (dry-run passed), UPGRADED (evolved)
        Dry-run    : also includes FORGED (code exists, not yet enabled)
        """
        allowed_statuses = set(_RUNTIME_READY_STATUSES)
        if dry_run:
            allowed_statuses.add(ToolStatus.FORGED)

        tools = []
        for tid in tool_ids:
            try:
                t = self.vault.get_tool(tid)
                if t.status in allowed_statuses:
                    tools.append(t)
                else:
                    logger.warning(
                        "[Runtime] Tool %s (%s) is %s — skipping%s",
                        t.name, tid, t.status,
                        " (use --dry-run to include forged/tested tools)" if not dry_run else ""
                    )
            except KeyError:
                logger.warning("[Runtime] Tool %s not found in vault", tid)
        return tools

    def _append_to_shadow_log(
        self,
        shadow:        Shadow,
        execution_id:  str,
        status:        str,
        summary:       str,
        *,
        iteration_count:     int = 0,
        tool_calls_made:     int = 0,
        objectives_completed: int = 0,
        objectives_total:    int = 0,
        duration_seconds:    float = 0.0,
    ) -> None:
        """Append execution result to Shadow's log, persist, and record flywheel metrics."""
        from datetime import datetime
        timestamp = utcnow().isoformat()
        shadow.execution_log.append({
            "execution_id": execution_id,
            "status":       status,
            "summary":      summary[:500],
            "timestamp":    timestamp,
        })
        # Keep last 100 log entries
        shadow.execution_log = shadow.execution_log[-100:]
        try:
            self.vault.save_shadow(shadow)
            self.vault.prune_old_executions(
                max_keep=getattr(self.config, "execution_retention_count", 50)
            )
        except Exception as exc:
            logger.warning("[Runtime] Could not persist shadow log: %s", exc)

        # Record flywheel metrics
        try:
            from systemu.runtime.metrics_tracker import record_execution
            shadow_dir = (
                Path(self.config.vault_dir) / "shadow_army" / f"shadow_{shadow.id}"
            )
            record_execution(
                shadow_id=shadow.id,
                shadow_name=shadow.name,
                shadow_dir=shadow_dir,
                execution_id=execution_id,
                status=status,
                iteration_count=iteration_count,
                tool_calls_made=tool_calls_made,
                objectives_completed=objectives_completed,
                objectives_total=objectives_total,
                duration_seconds=duration_seconds,
                memory_md_path=shadow.memory_md_path,
            )
        except Exception as exc:
            logger.warning("[Runtime] Metrics recording failed (non-fatal): %s", exc)
