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
from systemu.core.llm_router import _run_coroutine
from systemu.vault.vault import Vault

logger = logging.getLogger(__name__)


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
        return None

    if activity is None:
        vault.update_chat_history_entry(ts, {
            "status": "failed", "error": "extraction returned no activity",
        })
        return None

    # ── Stage 3: Shadow assignment ────────────────────────────────────────
    from systemu.pipelines.shadow_decision import decide_shadow
    try:
        # skip_supervisor=True: direct_task owns the execution (Stage 4 below).
        # Without this, decide_shadow() also submits to Supervisor, causing double execution.
        shadow = decide_shadow(activity, config, vault, skip_supervisor=True)
    except Exception as exc:
        logger.error("[DirectTask] Shadow decision failed: %s", exc)
        vault.update_chat_history_entry(ts, {"status": "failed", "error": str(exc)})
        return activity

    if shadow is None:
        logger.info("[DirectTask] No shadow assigned — user skipped or none available")
        vault.update_chat_history_entry(ts, {"status": "skipped_no_shadow"})
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
                priority=2, reason="chat",
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
        result = _run_coroutine(runtime.execute(shadow, activity))
    except Exception as exc:
        logger.error("[DirectTask] Execution failed: %s", exc)
        vault.update_chat_history_entry(ts, {
            "status": "failed", "error": str(exc), "shadow_id": shadow.id,
        })
        return activity

    vault.update_chat_history_entry(ts, {
        "status":       result.get("status", "unknown"),
        "shadow_id":    shadow.id,
        "execution_id": result.get("execution_id"),
    })

    # ── Stage 5: Wild Card reflection (best-effort) ───────────────────────
    if shadow.name == "Wild Card":
        try:
            from systemu.pipelines.evolution_engine import reflect_on_wild_card
            reflect_on_wild_card(shadow, activity, result, vault, config)
        except Exception as exc:
            logger.warning("[DirectTask] Wild Card reflection failed (non-fatal): %s", exc)

    return activity
