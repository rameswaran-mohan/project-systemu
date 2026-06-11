"""Direct Task Pipeline — chat/CLI free-text tasks.

Runs a user-typed prompt through the full pipeline without a capture session:

  1. /continue detection  — injects prior chat Scroll as context if requested
  2. refine_from_text     — Tier 1 synthesises prompt into a Scroll (APPROVED)
  3. extract_and_process  — extracts skills/tools → Activity
                             (skip_shadow_decision=True: we own the shadow call)
  4. decide_shadow        — heuristic + Wild Card; PARTIAL → Wild Card immediately
  5. ShadowRuntime.execute — agentic loop
  6. Wild Card reflection  — if shadow == Wild Card, emit evolution proposals
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any, Dict, Optional

from systemu.core.utils import utcnow

from sharing_on.config import Config
from systemu.approval.exceptions import PendingOperatorDecision
from systemu.core.llm_router import _run_coroutine
from systemu.vault.vault import Vault

logger = logging.getLogger(__name__)


def _waiting_on_tools_message(not_ready_names, not_ready_tools) -> str:
    """v0.8.22.1 (Fix 1c): adapt the waiting-on-tools wording to the blocker.
    If every not-ready tool already has code (forged/deployed/tested) and is
    merely disabled, the operator just needs to ENABLE it — not forge it."""
    from systemu.core.models import ToolStatus
    names = ", ".join(not_ready_names)
    only_disabled = bool(not_ready_tools) and all(
        getattr(t, "status", None) != ToolStatus.PROPOSED for t in not_ready_tools
    )
    if only_disabled:
        return (f"Waiting on tools: {names}. Enable them in the Tools Registry "
                f"(toggle ON); the task runs automatically once enabled.")
    return (f"Waiting on tools: {names}. Approve their dependencies "
            f"(Notifications → Approve & install) or forge them; the task runs "
            f"automatically once they deploy.")


def _handle_pending_decision_in_chat(vault, ts, *, decision_id, dedup_key, options) -> None:
    """v0.8.22 (C): when a chat-submitted run pauses on PendingOperatorDecision,
    update the chat history entry to status='pending_decision' so the chat UI
    can render an inline card instead of a cryptic 'failed' message."""
    vault.update_chat_history_entry(ts, {
        "status": "pending_decision",
        "decision_id": decision_id,
        "dedup_key": dedup_key,
        "options": list(options or []),
        # No secret values; redacted snapshot only
    })


def _maybe_trigger_fact_extraction(vault, config, ts: str) -> None:
    """v0.9.0 (Layer 1): after a chat task resolves to a terminal status, run
    LLM-driven fact extraction on the entry — gated by config flag,
    best-effort, never raises."""
    try:
        if not getattr(config, "auto_extract_user_facts", True):
            return
        entries = vault.load_chat_history(limit=200)
        entry = next((e for e in entries if e.get("ts") == ts), None)
        if entry is None:
            return
        terminal = {"completed", "failed", "pending_decision", "partial", "cancelled"}
        if entry.get("status") not in terminal:
            return
        from systemu.pipelines import fact_extractor as fe
        fe.extract_from_chat(entry, vault, config)
    except Exception:
        logger.debug("[DirectTask] fact-extraction hook swallowed error", exc_info=True)


def _maybe_extract_skill_and_consolidate(*, vault, config, scroll, shadow, result) -> None:
    """v0.9.6 (Layer 7): after a real chat run completes, run two best-effort,
    never-raising post-processing passes:

    1. **Auto-skill extraction** (Odysseus pattern) — ONLY on a successful,
       multi-step run (>=2 rounds OR >=2 tool calls). Tier-1 LLM decides
       whether the workflow is worth capturing as a SKILL.md; if confidence is
       high enough the recipe is persisted to the user skills dir. This is the
       PRIMARY way the skill library grows — skills are EARNED, not bundled.
    2. **Memory consolidation** — folds the run's intent + outcome into the
       run-level consolidated memory cache (facts_learned / patterns_observed),
       idempotent via SHA256 fingerprint.

    Both are config-gated inside their respective modules and wrapped so a
    failure can NEVER affect the user-visible run outcome.
    """
    from pathlib import Path as _Path
    try:
        res = result or {}
        status = res.get("status", "")
        intent = getattr(scroll, "intent", "") or ""
        summary = res.get("summary") or res.get("final_summary") or ""

        # ── (1) auto-skill extraction — success + threshold gated ──────────
        if status == "success":
            try:
                from systemu.runtime import auto_skill_extractor as _ase
                tools_called = list(res.get("tools_called") or [])
                n_tool_calls = int(res.get("tool_calls", len(tools_called)) or 0)
                n_rounds = int(res.get("rounds", res.get("total_events", 0)) or 0)
                candidate = _ase.extract_skill_candidate(
                    intent=intent,
                    chat_result=summary,
                    n_rounds=n_rounds,
                    n_tool_calls=n_tool_calls,
                    tools_called=tools_called,
                    config=config,
                )
                if candidate:
                    skills_dir = getattr(config, "skills_user_dir", "") or ""
                    if not skills_dir:
                        # No operator-configured user dir → keep earned skills
                        # in a vault-local directory so they're still discovered.
                        skills_dir = str(_Path(getattr(vault, "root", ".")) / "skills" / "earned")
                    path = _ase.persist_skill_candidate(candidate, skills_dir=skills_dir)
                    if path:
                        logger.info("[L7] auto-extracted SKILL.md → %s", path)
            except Exception:
                logger.debug("[L7] auto-skill extraction swallowed error", exc_info=True)

        # ── (1b) corrective (anti-pattern) extraction — failure/partial ────
        # v0.9.7 (Phase 4.2): learn-from-failure. When a run with real activity
        # fails or only partially succeeds, extract an anti-pattern SKILL.md so
        # future runs are warned about what went wrong. Same config/threshold
        # guards as the success path; never raises.
        elif status in ("failure", "partial"):
            try:
                from systemu.runtime import auto_skill_extractor as _ase
                tools_called = list(res.get("tools_called") or [])
                n_tool_calls = int(res.get("tool_calls", len(tools_called)) or 0)
                n_rounds = int(res.get("rounds", res.get("total_events", 0)) or 0)
                if n_rounds >= 2 or n_tool_calls >= 2:
                    failure_reason = summary or res.get("error") or ""
                    candidate = _ase.extract_corrective_candidate(
                        intent=intent,
                        failure_reason=failure_reason,
                        n_rounds=n_rounds,
                        n_tool_calls=n_tool_calls,
                        tools_called=tools_called,
                        config=config,
                    )
                    if candidate:
                        skills_dir = getattr(config, "skills_user_dir", "") or ""
                        if not skills_dir:
                            skills_dir = str(_Path(getattr(vault, "root", ".")) / "skills" / "earned")
                        path = _ase.persist_skill_candidate(candidate, skills_dir=skills_dir)
                        if path:
                            logger.info("[L7] corrective (anti-pattern) SKILL.md → %s", path)
            except Exception:
                logger.debug("[L7] corrective skill extraction swallowed error", exc_info=True)

        # ── (2) memory consolidation — config-gated, fingerprint-cached ────
        try:
            from systemu.runtime.memory_consolidator import consolidate_run
            chat_history = [
                {"role": "user", "content": intent},
                {"role": "assistant", "content": summary},
            ]
            consolidate_run(
                chat_history=chat_history,
                config=config,
                cache_root=_Path(getattr(vault, "root", ".")),
            )
        except Exception:
            logger.debug("[L7] memory consolidation swallowed error", exc_info=True)
    except Exception:
        logger.debug("[L7] post-run hook swallowed error", exc_info=True)


def _wire_chat_history_completion(
    vault: Vault,
    chat_ts: str,
    activity_id: str,
    submission_id: str,
    *,
    timeout_s: float = 1800.0,
) -> None:
    """Subscribe to the EventBus for *activity_id*'s terminal events and write
    the result back to the chat-history entry created during submission.

    Without this, a queued-mode submission shows ``status="queued"`` forever
    because the worker that actually runs the activity has no direct path back
    to the chat-history entry.

    The subscription self-unsubscribes after the first terminal event for the
    target activity, or after ``timeout_s`` seconds — preventing leaks if the
    activity vanishes (e.g. dead-letter without an explicit event we recognise).
    """
    try:
        from systemu.interface.event_bus import EventBus
        bus = EventBus.get()
    except Exception as exc:
        logger.debug("[DirectTask] EventBus unavailable, skipping completion wire: %s", exc)
        return

    state: Dict[str, Any] = {"unsub": None, "done": False}
    state_lock = threading.Lock()

    def _is_terminal_for_us(event: Dict[str, Any]) -> Optional[str]:
        ctx = event.get("context") or {}
        if ctx.get("activity_id") != activity_id:
            return None
        msg = (event.get("message") or "")
        if msg.startswith("✅ Completed"):
            return "success"
        if msg.startswith("💀 Dead-lettered"):
            return "failed"
        if msg.startswith("🚫 Shadow cancelled"):
            return "cancelled"
        return None

    def _on_event(event: Dict[str, Any]) -> None:
        with state_lock:
            if state["done"]:
                return
            terminal = _is_terminal_for_us(event)
            if not terminal:
                return
            state["done"] = True
            unsub = state.get("unsub")
        # Run the unsubscribe + vault write outside the lock.
        if unsub is not None:
            try:
                unsub()
            except Exception:
                pass
        ctx = event.get("context") or {}
        try:
            vault.update_chat_history_entry(chat_ts, {
                "status":       terminal,
                "submission_id": submission_id,
                "execution_id": (ctx.get("result") or {}).get("execution_id"),
                "error":        ctx.get("error") if terminal == "failed" else None,
                # W5.2: outcome summary for the Status dropdown (queued path).
                "summary":      (ctx.get("result") or {}).get("final_summary") or "",
            })
        except Exception as exc:
            logger.warning("[DirectTask] chat history update failed: %s", exc)

    state["unsub"] = bus.subscribe(_on_event, replay=False)

    # Safety net — drop the subscription after the timeout to avoid a slow leak
    # of subscriber callbacks if the activity is somehow lost.  Daemon=True so
    # the timer thread does not block process exit (otherwise pytest hangs at
    # the end of every test that creates a queued submission).
    def _expire() -> None:
        with state_lock:
            if state["done"]:
                return
            state["done"] = True
            unsub = state.get("unsub")
        if unsub is not None:
            try:
                unsub()
            except Exception:
                pass

    expiry_timer = threading.Timer(timeout_s, _expire)
    expiry_timer.daemon = True
    expiry_timer.start()


def run_direct_task(
    prompt: str,
    config: Config,
    vault:  Vault,
    *,
    route_through_supervisor: bool = False,
) -> Optional[Any]:
    """Run a free-text task through the full pipeline.

    Args:
        prompt: Raw user text (may start with '/continue').
        config: Config with API keys + model names.
        vault:  Vault instance.
        route_through_supervisor:
            False (default) — execute the assigned shadow synchronously in this
                thread.  Caller blocks until the activity finishes.  Suitable for
                local mode where the dashboard process IS the worker.
            True — submit the activity to the Supervisor task queue and return
                immediately.  Progress is published over the EventBus and shows
                up in Systemu Chat.  Suitable for docker-* modes where workers
                run in separate processes/containers.

    Returns:
        The Activity if execution was attempted (or queued, when
        route_through_supervisor=True), None on early pipeline failure.
    """
    from systemu.interface.notifications import set_vault
    from systemu.pipelines.activity_extractor import init_pipeline
    set_vault(vault)
    init_pipeline(config, vault)

    ts = utcnow().isoformat()

    # ── /continue detection ───────────────────────────────────────────────
    prior_task:  Optional[Dict[str, Any]] = None
    clean_prompt = prompt.strip()
    if clean_prompt.lower().startswith("/continue"):
        clean_prompt = clean_prompt[len("/continue"):].strip()
        prior_scroll = vault.get_latest_chat_scroll()
        if prior_scroll:
            prior_task = {
                "scroll_name": prior_scroll.name,
                "intent":      prior_scroll.intent,
                "objectives":  [obj.model_dump(mode="json") for obj in prior_scroll.objectives],
            }
            logger.info("[DirectTask] /continue: prior scroll '%s'", prior_scroll.name)
        else:
            logger.warning("[DirectTask] /continue with no prior chat scroll — fresh task")

    # ── Stage 1: Scroll ───────────────────────────────────────────────────
    from systemu.pipelines.scroll_refiner import refine_from_text
    try:
        scroll = refine_from_text(clean_prompt or prompt, vault, config, prior_task=prior_task)
    except Exception as exc:
        logger.error("[DirectTask] Scroll refinement failed: %s", exc)
        vault.append_chat_history({"ts": ts, "prompt": prompt, "status": "failed", "error": str(exc)})
        return None

    vault.append_chat_history({"ts": ts, "prompt": prompt, "scroll_id": scroll.id, "status": "running"})

    # ── Stage 2: Activity ─────────────────────────────────────────────────
    from systemu.pipelines.activity_extractor import extract_and_process
    try:
        activity = extract_and_process(scroll, config, vault, skip_shadow_decision=True)
    except Exception as exc:
        logger.error("[DirectTask] Activity extraction failed: %s", exc)
        vault.update_chat_history_entry(ts, {"status": "failed", "error": str(exc)})
        _maybe_trigger_fact_extraction(vault, config, ts)
        return None

    if activity is None:
        vault.update_chat_history_entry(ts, {
            "status": "failed", "error": "extraction returned no activity",
        })
        _maybe_trigger_fact_extraction(vault, config, ts)
        return None

    # v0.8.16: this is a chat-originated task — stamp the trigger origin so
    # every downstream event (sync execute + queued worker) partitions into
    # the Supervisor (chat) pane, not Manual Logs.
    try:
        activity.origin = "chat"
        vault.save_activity(activity)
    except Exception:
        logger.debug("[DirectTask] could not stamp chat origin on activity", exc_info=True)

    # ── Stage 3: Shadow assignment ────────────────────────────────────────
    from systemu.pipelines.shadow_decision import decide_shadow
    try:
        # skip_supervisor=True: direct_task owns the execution (Stage 4 below).
        # Without this, decide_shadow() also submits to Supervisor, causing double execution.
        shadow = decide_shadow(activity, config, vault, skip_supervisor=True)
    except Exception as exc:
        logger.error("[DirectTask] Shadow decision failed: %s", exc)
        vault.update_chat_history_entry(ts, {"status": "failed", "error": str(exc)})
        _maybe_trigger_fact_extraction(vault, config, ts)
        return activity

    if shadow is None:
        logger.info("[DirectTask] No shadow assigned — user skipped or none available")
        vault.update_chat_history_entry(ts, {"status": "skipped_no_shadow"})
        return activity

    # ── Stage 3.5: readiness gate (v0.8.13 RC#3) ──────────────────────────
    # If the activity needs tools that are not runtime-ready (freshly PROPOSED /
    # FORGED, deps pending approval), do NOT execute tool-less and report failure.
    # Park it as waiting_on_tools; recovery Pass 2 auto-runs it once tools deploy.
    from systemu.runtime.shadow_runtime import tool_is_runtime_ready
    not_ready = []
    not_ready_tools = []
    for tid in activity.required_tool_ids:
        try:
            t = vault.get_tool(tid)
        except KeyError:
            not_ready.append(tid)
            continue
        # v0.8.22.1 (Fix 1b): a disabled tool can't be invoked (Gate 3) — treat it
        # as not-ready so the activity parks instead of burning iterations failing
        # GATE_3_DISABLED at invocation.
        if not tool_is_runtime_ready(t.status) or not getattr(t, "enabled", False):
            not_ready.append(t.name)
            not_ready_tools.append(t)
    if not_ready:
        msg = _waiting_on_tools_message(not_ready, not_ready_tools)
        # v0.8.13 Fix 6a: re-mark the activity PARTIAL so the heal sweeps
        # (tool_service._heal_partial_activities + recovery Pass 2) always cover
        # it — even when a reused tool left it UNASSIGNED.
        from systemu.core.models import ActivityStatus
        try:
            activity.status = ActivityStatus.PARTIAL
            activity.missing_tools = not_ready
            vault.save_activity(activity)
        except Exception:
            logger.debug("[DirectTask] could not re-mark activity PARTIAL", exc_info=True)
        vault.update_chat_history_entry(ts, {
            "status":       "waiting_on_tools",
            "shadow_id":    shadow.id,
            "activity_id":  activity.id,
            "missing_tools": not_ready,
            "error":        msg,
        })
        # W1.2: make the park ACTIONABLE — post the unified Inbox gate naming
        # the blocking tools ("Enable & run" → Gate-3 enable → heal sweep
        # re-runs the task). Best-effort: a gate failure must not break the park.
        try:
            from systemu.interface.readiness_gate import ensure_tools_blocked_gate
            ensure_tools_blocked_gate(vault, activity, not_ready_tools)
        except Exception:
            logger.debug("[DirectTask] could not enqueue tools_blocked gate",
                         exc_info=True)
        logger.info("[DirectTask] Parked '%s' as waiting_on_tools — not ready: %s",
                    activity.id, not_ready)
        return activity

    # ── Stage 4: Execute ──────────────────────────────────────────────────
    if route_through_supervisor:
        # Submit to the Supervisor queue and return immediately.  Workers will
        # pick up the activity; progress events flow over the EventBus.
        try:
            from systemu.runtime.supervisor import Supervisor
            try:
                supervisor = Supervisor.get()
            except RuntimeError as exc:
                # Supervisor was never .init()ed in this process — most likely
                # the user invoked this from a CLI or test where only the
                # daemon would have started it.
                friendly = (
                    "Supervisor is not running in this process. "
                    "Start the daemon (./start.sh) before submitting queued tasks, "
                    "or run with route_through_supervisor=False to execute "
                    "synchronously."
                )
                logger.error("[DirectTask] %s — underlying: %s", friendly, exc)
                vault.update_chat_history_entry(ts, {
                    "status": "failed", "error": friendly, "shadow_id": shadow.id,
                })
                return activity

            sub_id = supervisor.submit(
                activity.id, shadow.id,
                priority=2, reason="chat", origin="chat",
                chat_submission_id=ts,  # v0.8.22.1 (Fix 2)
            )
            vault.update_chat_history_entry(ts, {
                "status": "queued",
                "shadow_id": shadow.id,
                "submission_id": sub_id,
            })
            _wire_chat_history_completion(vault, ts, activity.id, sub_id)
            logger.info(
                "[DirectTask] Queued via Supervisor — activity=%s shadow=%s sub=%s",
                activity.id, shadow.id, sub_id,
            )
        except Exception as exc:
            logger.error("[DirectTask] Supervisor.submit failed: %s", exc)
            vault.update_chat_history_entry(ts, {
                "status": "failed", "error": str(exc), "shadow_id": shadow.id,
            })
        return activity

    from systemu.runtime.shadow_runtime import ShadowRuntime
    runtime = ShadowRuntime(config, vault)
    try:
        result = _run_coroutine(runtime.execute(shadow, activity,
                                                 origin="chat",
                                                 chat_submission_id=ts))
    except PendingOperatorDecision as pd:
        # v0.8.22 (C): the run parked itself waiting on an operator decision.
        # Surface the parked state on the chat history entry so the chat UI can
        # render an inline card (matched against the live OperatorDecisionQueue
        # by decision_id) — NOT as a generic "failed".
        _handle_pending_decision_in_chat(
            vault, ts,
            decision_id=pd.decision_id,
            dedup_key=pd.dedup_key,
            options=pd.options,
        )
        logger.info("[DirectTask] chat task %r parked on decision %s — surfaced inline",
                    ts, pd.decision_id)
        _maybe_trigger_fact_extraction(vault, config, ts)
        return activity
    except Exception as exc:
        logger.error("[DirectTask] Execution failed: %s", exc)
        vault.update_chat_history_entry(ts, {
            "status": "failed", "error": str(exc), "shadow_id": shadow.id,
        })
        _maybe_trigger_fact_extraction(vault, config, ts)
        return activity

    vault.update_chat_history_entry(ts, {
        "status":       result.get("status", "unknown"),
        "shadow_id":    shadow.id,
        "execution_id": result.get("execution_id"),
        # W5.2: persist the run's outcome so the Status dropdown (and any
        # other task-list surface) can show WHAT happened, not just a badge.
        "summary":      result.get("final_summary") or "",
    })

    # W5.3: stream the outcome to the live panes — the operator should see
    # "what happened" in the right-rail Live feed without hunting. The queued
    # path already emits the worker's "✅ Completed…" event; this covers the
    # sync path. details carries the expand-arrow payload (Outcome/Artifacts).
    try:
        from systemu.interface.notifications import log_event as _log_event
        _status = result.get("status", "unknown")
        _level = {"success": "SUCCESS", "partial": "WARNING"}.get(_status, "ERROR")
        _summary = result.get("final_summary") or ""
        _log_event(
            _level, "task_outcome",
            f"Task {_status}: {prompt[:80]}",
            {"origin": "chat", "scroll_id": scroll.id,
             "activity_id": activity.id,
             "execution_id": result.get("execution_id")},
            details={"summary": _summary,
                     "output_dir": getattr(config, "output_dir", "") or ""},
        )
    except Exception:
        logger.debug("[DirectTask] outcome event publish failed", exc_info=True)

    # Wave 1.4: persist the terminal activity state on the SYNC path too.
    # Previously only the Supervisor's queued path flipped the activity to
    # COMPLETED — a sync-executed task stayed "assigned" in the vault forever,
    # so the dashboard showed finished work as never-finished.
    if result.get("status") == "success":
        from systemu.runtime.activity_completion import mark_activity_completed
        mark_activity_completed(vault, activity.id)

    _maybe_trigger_fact_extraction(vault, config, ts)

    # v0.9.2: episodic capture — best-effort hook at chat-resolve
    try:
        from systemu.runtime.shadow_runtime import _trigger_episodic_capture
        _trigger_episodic_capture(
            vault=vault,
            config=config,
            session_id=ts,
            intent=getattr(scroll, "intent", ""),
            chat_result=result.get("final_summary"),
            files_produced=[],
            status=result.get("status", "unknown"),
            execution_id=result.get("execution_id"),
            raw_chat_id=ts,
        )
    except Exception:
        logger.debug("[DirectTask] episodic capture hook swallowed error", exc_info=True)

    # v0.9.6 (Layer 7): auto-skill extraction + memory consolidation.
    # Best-effort, never raises. This is where the skill library EARNS new
    # SKILL.md recipes from successful multi-step runs (Odysseus pattern).
    _maybe_extract_skill_and_consolidate(
        vault=vault, config=config, scroll=scroll, shadow=shadow, result=result,
    )

    # ── Stage 5: Wild Card reflection (best-effort) ───────────────────────
    if shadow.name == "Wild Card":
        try:
            from systemu.pipelines.evolution_engine import reflect_on_wild_card
            reflect_on_wild_card(shadow, activity, result, vault, config)
        except Exception as exc:
            logger.warning("[DirectTask] Wild Card reflection failed (non-fatal): %s", exc)

    return activity
