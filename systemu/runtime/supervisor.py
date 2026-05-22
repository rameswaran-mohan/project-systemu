"""Systemu Supervisor — orchestrates shadow execution with full operational controls.

Provides:
  • Priority Activity Queue   — thread-safe, crash-persistent (JSON on disk)
  • Concurrency Gate          — semaphore caps simultaneous shadow runs
  • API Rate Limiter          — token-bucket prevents OpenRouter 429 bursts
  • Heartbeat Watchdog        — detects & re-queues stuck shadows
  • Retry + Dead Letter Queue — configurable max retries, permanent-failure log
  • Graceful Shutdown         — queue persisted to disk, restored on restart
  • LLM Failure Analysis      — Tier-1 diagnosis of failed/stuck shadows
  • Approval Gate             — request user confirmation via EventBus/UI
  • Cancellation Events       — watchdog can signal a shadow to exit cleanly

Threading model:
  _dispatcher_loop  — dedicated daemon thread, drains the priority queue
  Each shadow run   — one daemon thread (creates its own asyncio event loop)
  _heartbeat_loop   — dedicated daemon thread, scans for stuck runs
  All shared state  — protected by threading.Lock

Integration points:
  Supervisor.get()          → singleton (set by __init__ and by init())
  Supervisor.init(...)      → create + start idempotently (used by dashboard)
  Supervisor.submit(...)    → queue an activity for execution
  Supervisor.get_status()   → queue depth, running count, dead letters, etc.
  Supervisor.shutdown()     → graceful shutdown
  EventBus.publish_*()      → all events flow to the UI Systemu Chat page

Bug-fix changelog (v2):
  [FIX-1] __init__ now sets _instance immediately — shadow_runtime heartbeat calls
          work even when Supervisor is created via direct instantiation (eval scripts).
  [FIX-2] STUCK_THRESHOLD_S raised 180 → 300 — prevents false watchdog fires on
          shadows making slow-but-valid LLM inference calls (30-130 s each).
  [FIX-3] Watchdog pops the key from _running immediately and releases the semaphore
          itself — eliminates cascading re-fires at each 20 s heartbeat interval.
  [FIX-4] _run_shadow_guarded only releases semaphore/handles result when it owns the
          slot (entry != None); if watchdog already cleaned up, zombie thread exits
          silently — no double-release and no spurious "✅ Completed" events.
  [FIX-5] Retry counter derived from the running entry's payload, not the original
          submission — watchdog re-queues always carry the correct incremented count.
  [FIX-6] Cancellation threading.Event stored in each running entry and passed to the
          shadow runtime — watchdog sets it so the shadow exits at the next iteration
          boundary instead of continuing to consume API tokens as a zombie.
  [FIX-7] _check_stuck_shadows skips entries whose status is already "starting" for
          the first STUCK_THRESHOLD_S seconds — prevents spurious fires on cold starts.
  [A.3]  Heartbeat watchdog switched to time.monotonic() so laptop sleep/wake events
         don't cause false-positive "stuck" detections.  A gap > 3× HEARTBEAT_INTERVAL_S
         between watchdog ticks indicates a host sleep; all running-shadow heartbeats
         are reset to now and the watchdog check is skipped for that cycle.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
MAX_CONCURRENT_SHADOWS  = 3      # max parallel shadow executions
MAX_RETRIES             = 2      # how many times to retry a failed activity
STUCK_THRESHOLD_S       = 300    # [FIX-2] raised from 180s — LLM calls can take 130s each
HEARTBEAT_INTERVAL_S    = 20     # how often the watchdog checks
SLEEP_JUMP_THRESHOLD_S  = HEARTBEAT_INTERVAL_S * 3   # gap > 60s → host likely slept
QUEUE_PERSIST_FILE      = "supervisor_queue.json"   # inside vault dir
TOKEN_BUCKET_PER_MINUTE = 80_000  # OpenRouter token budget / minute

# ── Token-bucket rate limiter ─────────────────────────────────────────────────

class _RateLimiter:
    """Simple token-bucket rate limiter (thread-safe)."""

    def __init__(self, tokens_per_minute: int) -> None:
        self._tpm      = tokens_per_minute
        self._tokens   = float(tokens_per_minute)
        self._lock     = threading.Lock()
        self._last_ts  = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_ts
        self._tokens = min(
            float(self._tpm),
            self._tokens + elapsed * (self._tpm / 60.0),
        )
        self._last_ts = now

    def acquire(self, estimated_tokens: int = 2048) -> None:
        """Block until *estimated_tokens* are available."""
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= estimated_tokens:
                    self._tokens -= estimated_tokens
                    return
            time.sleep(1)


# ── Supervisor singleton ──────────────────────────────────────────────────────

class Supervisor:
    """Orchestrates all shadow execution with queuing, rate limiting, and diagnostics."""

    _instance: Optional["Supervisor"] = None
    _init_lock: threading.RLock = threading.RLock()  # RLock: reentrant — init() calls __init__ under same lock

    # ── Construction / singleton ──────────────────────────────────────────────

    def __init__(self, config: Any, vault: Any, task_queue: Any = None) -> None:
        self.config = config
        self.vault  = vault

        # [A.2] Optional durable queue — when present, every submit/complete/fail
        # is written through to SQLite so the queue survives hard crashes.
        # When None, falls back to pure in-memory mode (original behaviour).
        self._task_queue = task_queue
        self._worker_id  = f"proc-{os.getpid()}-{uuid.uuid4().hex[:6]}"

        # Priority queue: items are (priority, enqueued_at, payload_dict)
        # Lower priority number = higher urgency (standard Python heapq order)
        self._queue: queue.PriorityQueue[Tuple[int, float, Dict[str, Any]]] = queue.PriorityQueue()

        # Concurrency: semaphore limits parallel shadow threads
        self._semaphore = threading.Semaphore(MAX_CONCURRENT_SHADOWS)

        # Running shadows: execution_key → {thread, cancel_event, last_heartbeat_at, meta}
        # [FIX-3/4/5/6] cancel_event lets watchdog signal clean exit; popping key from
        # _running in the watchdog prevents cascading re-fires and double semaphore releases.
        self._running: Dict[str, Dict[str, Any]] = {}
        self._running_lock = threading.Lock()

        # Pending activity IDs: set of activity_ids currently sitting in the queue
        # (not yet dispatched). Populated by submit() and _restore_queue(); cleared
        # by the dispatcher when an item is dequeued. Used by the dedup guard in
        # submit() to prevent double-queueing across queue+running (e.g. when both
        # startup recovery and fresh assignment fire for the same activity_id).
        self._pending_activity_ids: set[str] = set()
        self._pending_lock = threading.Lock()

        # Dead letter: activities that exhausted all retries
        self._dead_letters: List[Dict[str, Any]] = []
        self._dl_lock = threading.Lock()

        # API rate limiter (shared across all shadow threads)
        self._rate_limiter = _RateLimiter(TOKEN_BUCKET_PER_MINUTE)

        # Shutdown coordination
        self._shutdown_event = threading.Event()

        # Background threads (started by .start())
        self._dispatcher_thread: Optional[threading.Thread] = None
        self._heartbeat_thread:  Optional[threading.Thread] = None

        # Queue persistence path
        vault_path = Path(getattr(vault, "root", str(config.vault_dir)))
        self._queue_persist_path = vault_path / QUEUE_PERSIST_FILE

        # [FIX-1] Register as the global singleton immediately so shadow threads can
        # find us via Supervisor.get() even when created via direct instantiation.
        with Supervisor._init_lock:
            Supervisor._instance = self

        # Restore queue from previous run.
        # [A.2] When a durable queue is attached, migrate any legacy JSON file first,
        # then recover orphaned DB rows into the in-memory queue.
        # Without a durable queue, fall back to the original JSON-restore path.
        if self._task_queue is not None:
            self._migrate_json_queue()
            self._recover_orphans()
        else:
            self._restore_queue()

    @classmethod
    def get(cls) -> "Supervisor":
        if cls._instance is None:
            raise RuntimeError(
                "Supervisor not initialised. Call Supervisor.init(config, vault) first."
            )
        return cls._instance

    @classmethod
    def init(cls, config: Any, vault: Any, task_queue: Any = None) -> "Supervisor":
        """Create and start the supervisor (idempotent — safe to call multiple times).

        Uses RLock so __init__'s inner _init_lock acquisition doesn't deadlock.

        When ``task_queue`` is None, the durable queue adapter is selected from
        the environment via :func:`systemu.queue.protocol.build_task_queue`
        (sqlite for local + docker-local; redis for docker-enterprise; None for
        the file backend, in which case the supervisor stays purely in-memory).
        """
        with cls._init_lock:
            if cls._instance is None:
                if task_queue is None:
                    # build_task_queue itself raises in strict mode
                    # (docker-enterprise) — let that propagate so the
                    # operator sees a hard failure at boot instead of a
                    # silent degradation to in-memory.  Only the lenient
                    # path (warnings → None) is caught here.
                    from systemu.queue.protocol import build_task_queue
                    strict = (os.environ.get("SYSTEMU_MODE", "").lower()
                              == "docker-enterprise")
                    try:
                        task_queue = build_task_queue(config)
                    except Exception as exc:
                        if strict:
                            # Re-raise — docker-enterprise users would
                            # rather see crash-loops than silent data loss.
                            raise
                        logger.warning(
                            "[Supervisor] build_task_queue failed (%s) — running in-memory",
                            exc,
                        )
                        task_queue = None
                sup = cls(config, vault, task_queue=task_queue)
                sup.start()
                # _instance already set by __init__
        return cls._instance

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the dispatcher and heartbeat background threads."""
        self._dispatcher_thread = threading.Thread(
            target=self._dispatcher_loop,
            daemon=True,
            name="supervisor-dispatcher",
        )
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            daemon=True,
            name="supervisor-heartbeat",
        )
        self._dispatcher_thread.start()
        self._heartbeat_thread.start()

        self._publish("🚀 Supervisor started", level="SUCCESS", context={
            "max_concurrent": MAX_CONCURRENT_SHADOWS,
            "max_retries": MAX_RETRIES,
            "stuck_threshold_s": STUCK_THRESHOLD_S,
            "token_bucket_tpm": TOKEN_BUCKET_PER_MINUTE,
        })
        logger.info("[Supervisor] Started — max_concurrent=%d stuck_threshold=%ds",
                    MAX_CONCURRENT_SHADOWS, STUCK_THRESHOLD_S)

    def shutdown(self) -> None:
        """Graceful shutdown: persist pending queue items, signal threads to stop."""
        self._publish("🛑 Supervisor shutting down — persisting queue…", level="WARNING")
        self._persist_queue()
        self._shutdown_event.set()
        logger.info("[Supervisor] Shutdown initiated.")

    # ── Public API ────────────────────────────────────────────────────────────

    def submit(
        self,
        activity_id: str,
        shadow_id: str,
        *,
        priority: int = 5,
        reason: str = "manual",
        retry_count: int = 0,
        exclude_shadow_id: Optional[str] = None,
        scroll_id: Optional[str] = None,
        consult_affinity_log: bool = True,
        resume_from_execution_id: Optional[str] = None,
    ) -> str:
        """Queue an activity for execution.

        Args:
            activity_id:  ID of the Activity to run.
            shadow_id:    ID of the Shadow to run it with.
            priority:     1=urgent, 5=normal, 10=background (lower = sooner).
            reason:       Human-readable source of this submission.
            retry_count:  Internal — used when re-queuing after failure.
            exclude_shadow_id: When provided AND it matches ``shadow_id``, find
                          an alternative shadow before queueing.  Used by the
                          "Retry with different shadow" operator
                          action — caller passes the shadow that just gave up.
            scroll_id:    Scroll id (used by the affinity-log consultation to
                          compute intent_hash; falls back to vault lookup when
                          omitted).
            consult_affinity_log: When True (default), check the v0.4.1-b
                          affinity log: if this (intent_hash, shadow_id) has
                          a recent TERMINATE, pick a different shadow before
                          queueing.  Set to False to skip the check (e.g. for
                          tests or operator-forced submissions).

        Returns:
            A submission_id string for tracking.
        """
        # Affinity-aware routing.  Two reasons to swap the assigned
        # shadow before queueing:
        #   1. Caller explicitly excluded this shadow (Retry-different-shadow).
        #   2. The affinity log has a recent TERMINATE for (intent_hash, shadow).
        # In either case, look up an alternative shadow whose skill_ids overlap
        # with the activity's required_skill_ids.  When no alternative exists,
        # we log loudly and fall back to the original assignment — better to
        # surface a noisy failure than silently sit on a queueable activity.
        original_shadow_id = shadow_id
        if consult_affinity_log or exclude_shadow_id:
            try:
                shadow_id = self._resolve_shadow_with_affinity(
                    activity_id=activity_id,
                    shadow_id=shadow_id,
                    exclude_shadow_id=exclude_shadow_id,
                    scroll_id=scroll_id,
                )
            except Exception:
                logger.exception(
                    "[Supervisor] affinity resolution failed — using original assignment"
                )
                shadow_id = original_shadow_id
        # Idempotency guard: skip if this activity is already in the pending queue OR
        # currently running in a shadow thread. Prevents double-execution when both
        # startup recovery and fresh assignment fire for the same activity_id (e.g.
        # after a crash mid-execution that left an item in the persisted queue file).
        # Retries carry retry_count > 0 and are always allowed through.
        if retry_count == 0:
            with self._pending_lock:
                if activity_id in self._pending_activity_ids:
                    logger.info(
                        "[Supervisor] Skipping duplicate submit for %s — already pending in queue",
                        activity_id,
                    )
                    return f"sub_skipped_{activity_id[:8]}"
            with self._running_lock:
                for entry in self._running.values():
                    if entry["payload"].get("activity_id") == activity_id:
                        logger.info(
                            "[Supervisor] Skipping duplicate submit for %s — already running",
                            activity_id,
                        )
                        return f"sub_skipped_{activity_id[:8]}"

        submission_id = f"sub_{uuid.uuid4().hex[:8]}"
        payload = {
            "submission_id": submission_id,
            "activity_id":   activity_id,
            "shadow_id":     shadow_id,
            "priority":      priority,
            "retry_count":   retry_count,
            "reason":        reason,
            "enqueued_at":   time.time(),
            # resume hint carried in payload so the shadow runtime
            # can rebuild its context from the snapshot persisted by
            # RECALIBRATE_TOOL.  When None, runtime starts fresh.
            "resume_from_execution_id": resume_from_execution_id,
        }
        # [A.2] Persist to SQLite BEFORE the in-memory put.  If the process crashes
        # between these two writes, the DB row stays in 'queued' state and
        # _recover_orphans() restores it into memory on the next startup.
        if self._task_queue is not None:
            try:
                self._task_queue.enqueue(
                    activity_id, shadow_id,
                    priority=priority, reason=reason, retry_count=retry_count,
                    submission_id=submission_id,
                )
            except Exception as exc:
                logger.warning("[Supervisor] SQLite queue enqueue failed (falling back to memory): %s", exc)
        with self._pending_lock:
            self._pending_activity_ids.add(activity_id)
        self._queue.put((priority, time.time(), payload))
        self._publish(
            f"📥 Activity queued: {activity_id} (priority={priority}, reason={reason}, retry={retry_count})",
            context={"submission_id": submission_id, "activity_id": activity_id,
                     "shadow_id": shadow_id, "priority": priority, "retry_count": retry_count},
        )
        logger.info("[Supervisor] Queued %s / %s (submission=%s retry=%d)",
                    activity_id, shadow_id, submission_id, retry_count)
        return submission_id

    def _resolve_shadow_with_affinity(
        self,
        *,
        activity_id: str,
        shadow_id: str,
        exclude_shadow_id: Optional[str],
        scroll_id: Optional[str],
    ) -> str:
        """Return the shadow we should actually queue this activity for.

        Consults the v0.4.1-b affinity log: if the (intent_hash, shadow_id)
        pair has a recent TERMINATE, OR ``exclude_shadow_id`` matches the
        candidate, scan the shadow_army for an alternative whose skill_ids
        overlap with the activity's required_skill_ids.  Picks the first
        match deterministically (sorted by id).

        When no alternative exists OR the activity / scroll can't be
        resolved, returns the original ``shadow_id`` — affinity routing is
        a *signal*, never a hard ban.
        """
        # 1. Are we even being asked to swap?  No affinity hit + no explicit
        #    exclusion → return immediately.
        try:
            activity = self.vault.get_activity(activity_id)
        except Exception:
            return shadow_id

        scroll_id = scroll_id or getattr(activity, "scroll_id", None)
        intent_hash = ""
        try:
            scroll = self.vault.get_scroll(scroll_id) if scroll_id else None
            if scroll is not None:
                from systemu.runtime.affinity_log import compute_intent_hash
                intent_hash = compute_intent_hash(
                    intent=getattr(scroll, "intent", "") or "",
                    objectives=getattr(scroll, "objectives", None),
                )
        except Exception:
            logger.debug(
                "[Supervisor] could not compute intent_hash for %s — affinity check by shadow id only",
                activity_id, exc_info=True,
            )

        excluded_by_affinity = False
        if intent_hash:
            try:
                from systemu.runtime.affinity_log import get_affinity_log
                excluded_by_affinity = get_affinity_log().is_excluded(
                    intent_hash=intent_hash, shadow_id=shadow_id,
                )
            except Exception:
                logger.debug(
                    "[Supervisor] affinity-log lookup failed", exc_info=True,
                )

        excluded_by_caller = bool(
            exclude_shadow_id and exclude_shadow_id == shadow_id
        )

        if not excluded_by_affinity and not excluded_by_caller:
            return shadow_id

        # 2. Look for an alternative shadow whose skills overlap with the
        #    activity's required_skill_ids.  Deterministic by sorted id.
        required_skills = set(getattr(activity, "required_skill_ids", []) or [])
        try:
            candidates = self.vault.list_shadows() or []
        except Exception:
            return shadow_id

        # look up the originating shadow's specialty so we can
        # prefer candidates that share it.  Empty specialty on either side
        # → specialty bonus is 0 (no preference signal).
        origin_specialty = ""
        try:
            origin_shadow = self.vault.get_shadow(shadow_id)
            origin_specialty = (
                str(getattr(origin_shadow, "specialty", "") or "").strip().lower()
            )
        except Exception:
            pass

        # Score tuple: (-skill_overlap, -specialty_match, -success_rate, shadow_id).
        # Lower sorts first, so: higher overlap → preferred, then matching
        # specialty → preferred, then higher success_rate → preferred,
        # then deterministic lexical id.
        # ShadowMetrics second-tier ranking — when two shadows
        # both have skill_overlap=2, the one with a better track record on
        # this intent_hash wins.
        # specialty tag breaks ties between metric-equivalent
        # candidates by preferring those with matching specialty.
        scored: List[tuple] = []
        # Cache affinity-log excludes per shadow so we don't double-check.
        from systemu.runtime.affinity_log import get_affinity_log
        from systemu.runtime.shadow_metrics import get_shadow_metrics
        affinity = get_affinity_log() if intent_hash else None
        metrics = get_shadow_metrics()
        for c in candidates:
            cid = c.get("id") if isinstance(c, dict) else getattr(c, "id", None)
            if not cid or cid == shadow_id:
                continue
            if exclude_shadow_id and cid == exclude_shadow_id:
                continue
            if affinity is not None and affinity.is_excluded(
                intent_hash=intent_hash, shadow_id=cid,
            ):
                continue
            shadow_skills = set(
                (c.get("skill_ids") if isinstance(c, dict)
                 else getattr(c, "skill_ids", [])) or []
            )
            if required_skills:
                overlap = len(required_skills & shadow_skills)
                if overlap == 0:
                    continue
            else:
                overlap = 0
            # specialty match (1 when both have the same non-empty
            # specialty, 0 otherwise).  Inserted between overlap and metrics
            # so two equal-skill candidates split on specialty first.
            cand_specialty = (
                (c.get("specialty") if isinstance(c, dict)
                 else getattr(c, "specialty", "")) or ""
            )
            cand_specialty = str(cand_specialty).strip().lower()
            specialty_match = (
                1 if origin_specialty
                and cand_specialty
                and origin_specialty == cand_specialty
                else 0
            )
            # success_rate is 0.5 when this shadow has no history on this
            # intent_hash — neutral, neither rewarding nor penalising the
            # cold-start case.
            success_rate = 0.5
            if intent_hash:
                try:
                    success_rate = metrics.get(
                        shadow_id=cid, intent_hash=intent_hash,
                    ).success_rate
                except Exception:
                    success_rate = 0.5
            scored.append((-overlap, -specialty_match, -success_rate, cid))

        if not scored:
            logger.warning(
                "[Supervisor] No alternative shadow found for activity %s "
                "(excluded=%s, intent_hash=%s) — keeping original %s",
                activity_id, shadow_id, intent_hash, shadow_id,
            )
            return shadow_id

        scored.sort()
        alt = scored[0][3]
        logger.info(
            "[Supervisor] Affinity routing: activity %s swapped %s → %s "
            "(reason=%s, alt_skill_overlap=%d, specialty_match=%d, alt_success_rate=%.2f)",
            activity_id, shadow_id, alt,
            "affinity_log" if excluded_by_affinity else "operator_exclusion",
            -scored[0][0], -scored[0][1], -scored[0][2],
        )
        return alt

    def resume_after_recalibration(
        self,
        *,
        execution_id: str,
        original_tool_id: str,
        new_tool_id: str,
        mode: str,                    # "bump_version" | "fork_new_tool"
        original_shadow_id: str,
        scroll_id: Optional[str] = None,
    ) -> str:
        """operator approved a recalibration — re-queue activity.

        Looks up the originating activity (from the audit / vault), updates
        the shadow's ``available_tool_ids`` if forking (so the shadow uses
        the new specialised tool), and re-submits the activity with a
        sticky-note context handoff.

        Returns the submission_id of the re-queued activity.  When the
        activity can't be found, returns a "no-op" id and logs.
        """
        # 1. Find the activity for this shadow + scroll.  The execution_id
        #    doesn't directly index back to an activity_id, so we search.
        activity_id = None
        try:
            for ah in (self.vault.list_activities() or []):
                if not isinstance(ah, dict):
                    continue
                if ah.get("scroll_id") == scroll_id and ah.get("assigned_shadow_id") == original_shadow_id:
                    activity_id = ah.get("id")
                    break
        except Exception:
            logger.exception("[Supervisor] resume_after_recalibration: activity lookup failed")
        if not activity_id:
            logger.warning(
                "[Supervisor] resume_after_recalibration: no activity found for "
                "scroll=%s shadow=%s — caller must re-submit manually",
                scroll_id, original_shadow_id,
            )
            return f"sub_no_activity_{original_tool_id[:8]}"

        # 2. For fork mode, update the shadow's available_tool_ids to swap
        #    old → new.  Other shadows are unaffected; the old tool record
        #    stays in the vault for them.
        if mode == "fork_new_tool" and new_tool_id != original_tool_id:
            try:
                sh = self.vault.get_shadow(original_shadow_id)
                tools = list(sh.available_tool_ids or [])
                if original_tool_id in tools:
                    tools = [t for t in tools if t != original_tool_id]
                if new_tool_id not in tools:
                    tools.append(new_tool_id)
                sh.available_tool_ids = tools
                self.vault.save_shadow(sh)
                logger.info(
                    "[Supervisor] resume_after_recalibration: swapped %s → %s "
                    "in shadow %s available_tool_ids",
                    original_tool_id, new_tool_id, original_shadow_id,
                )
            except Exception:
                logger.exception(
                    "[Supervisor] resume_after_recalibration: tool swap failed",
                )

        # 3. Re-queue with elevated priority and a sticky-note hint that
        #    will land in the new execution's context.  Affinity log is
        #    intentionally NOT consulted — we want the same shadow back.
        # also pass the prior execution_id so the new run can
        #    load + apply the snapshot RECALIBRATE_TOOL persisted (true
        #    resume — preserves completed objectives + recent context).
        #    When the snapshot is missing the runtime falls back to the
        #    fresh-restart-with-sticky behaviour.
        return self.submit(
            activity_id=activity_id,
            shadow_id=original_shadow_id,
            priority=2,
            reason=(
                f"operator_approved_recalibration:{mode}:{new_tool_id}"
            )[:120],
            retry_count=1,
            consult_affinity_log=False,
            resume_from_execution_id=execution_id,
        )

    def get_status(self) -> Dict[str, Any]:
        """Return a snapshot of supervisor state for the UI."""
        with self._running_lock:
            running_list = [
                {
                    "key":             k,
                    "activity_id":     v["payload"]["activity_id"],
                    "shadow_id":       v["payload"]["shadow_id"],
                    "retry_count":     v["payload"].get("retry_count", 0),
                    "started_at":      v["started_at"],
                    "last_heartbeat":  v["last_heartbeat_at"],
                    "status":          v.get("status", "running"),
                }
                for k, v in self._running.items()
            ]

        with self._dl_lock:
            dl_list = list(self._dead_letters[-20:])   # last 20

        return {
            "queue_depth":       self._queue.qsize(),
            "running_count":     len(running_list),
            "running":           running_list,
            "dead_letters":      dl_list,
            "dead_letter_count": len(self._dead_letters),
            "max_concurrent":    MAX_CONCURRENT_SHADOWS,
            "stuck_threshold_s": STUCK_THRESHOLD_S,
        }

    # ── Dispatcher loop ───────────────────────────────────────────────────────

    def _dispatcher_loop(self) -> None:
        """Background thread: drain the queue, respect concurrency semaphore."""
        while not self._shutdown_event.is_set():
            try:
                try:
                    _priority, _ts, payload = self._queue.get(timeout=2.0)
                except queue.Empty:
                    continue

                # Clear pending-set entry now that this item is being dispatched.
                # Must happen before the semaphore acquire so a concurrent submit()
                # can re-queue the same activity_id if this dispatch ultimately fails.
                with self._pending_lock:
                    self._pending_activity_ids.discard(payload.get("activity_id", ""))

                # [A.2] Mark the DB row as running (durable state transition).
                if self._task_queue is not None:
                    try:
                        self._task_queue.mark_running(payload["submission_id"])
                    except Exception as exc:
                        logger.warning("[Supervisor] SQLite queue mark_running failed: %s", exc)

                # Check for starvation (item waiting > 10 minutes)
                wait_s = time.time() - payload["enqueued_at"]
                if wait_s > 600:
                    self._publish(
                        f"⚠️ Activity {payload['activity_id']} waited {wait_s:.0f}s in queue",
                        level="WARNING",
                        context=payload,
                    )

                # Acquire slot (blocks if all slots busy, respects shutdown)
                while not self._semaphore.acquire(timeout=2.0):
                    if self._shutdown_event.is_set():
                        return

                # Create a cancellation event for this execution slot [FIX-6]
                cancel_event = threading.Event()

                # Launch shadow in a worker thread
                key = f"{payload['activity_id']}_{payload['submission_id']}"
                t = threading.Thread(
                    target=self._run_shadow_guarded,
                    args=(key, payload, cancel_event),
                    daemon=True,
                    name=f"shadow-{payload['activity_id'][:12]}",
                )
                with self._running_lock:
                    self._running[key] = {
                        "thread":                  t,
                        "cancel_event":            cancel_event,   # [FIX-6]
                        "payload":                 payload,
                        "started_at":              time.time(),
                        "last_heartbeat_at":       time.time(),       # wall-clock (display only)
                        "last_heartbeat_at_mono":  time.monotonic(),  # [A.3] monotonic (watchdog comparison)
                        "status":                  "starting",
                    }
                t.start()

            except Exception as exc:
                logger.exception("[Supervisor] Dispatcher error: %s", exc)
                time.sleep(1)

    # ── Shadow execution ──────────────────────────────────────────────────────

    def _run_shadow_guarded(
        self,
        key: str,
        payload: Dict[str, Any],
        cancel_event: threading.Event,
    ) -> None:
        """Execute a shadow in its own asyncio event loop (runs in a daemon thread).

        [FIX-4] Semaphore is only released AND _handle_result only called when this
        thread owns the running slot.  If the watchdog already popped *key* from
        _running and released the semaphore, `entry` will be None on the pop below,
        and we skip both — preventing double-release and spurious result events.
        """
        activity_id = payload["activity_id"]
        shadow_id   = payload["shadow_id"]
        retry_count = payload.get("retry_count", 0)

        self._update_heartbeat(key, "running")
        self._publish(
            f"▶️ Executing: {activity_id} (retry={retry_count})",
            context={"activity_id": activity_id, "shadow_id": shadow_id,
                     "retry_count": retry_count, "key": key},
        )

        result: Dict[str, Any] = {}
        try:
            # Rate limiter — prevent API burst across parallel shadows
            self._rate_limiter.acquire(2048)

            # Lazy import to avoid circular imports at module level
            from systemu.runtime.shadow_runtime import ShadowRuntime

            runtime  = ShadowRuntime(self.config, self.vault)
            shadow   = self.vault.get_shadow(shadow_id)
            activity = self.vault.get_activity(activity_id)

            self._update_heartbeat(key, "running")

            # thread the resume hint from the queue payload through
            # to runtime.execute().  When set, the runtime loads the snapshot
            # written by RECALIBRATE_TOOL and rebuilds context (true resume).
            resume_from = payload.get("resume_from_execution_id")

            # Create a fresh event loop for this thread
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                result = loop.run_until_complete(
                    runtime.execute(
                        shadow, activity,
                        cancel_event=cancel_event,
                        resume_from_execution_id=resume_from,
                    )
                )
            finally:
                loop.close()
                asyncio.set_event_loop(None)

        except Exception as exc:
            logger.exception("[Supervisor] Shadow thread error: %s", exc)
            result = {
                "status": "failure",
                "error":  str(exc),
                "final_summary": f"Supervisor thread error: {exc}",
            }

        finally:
            # [FIX-3/4] Pop our key from _running under the lock.
            # If the watchdog already popped it (and released the semaphore),
            # entry will be None — we must NOT release the semaphore again,
            # and we must NOT call _handle_result (retry was already submitted).
            with self._running_lock:
                entry = self._running.pop(key, None)

            if entry is not None:
                # We own the slot — release it and process the result
                self._semaphore.release()
                self._handle_result(payload, result)
            else:
                # Watchdog already cleaned up this slot; we are a zombie.
                # Log but do not re-trigger any result handling.
                logger.info(
                    "[Supervisor] Zombie thread finished for key=%s (watchdog already handled) "
                    "— result suppressed. status=%s",
                    key, result.get("status", "?"),
                )

    def _handle_result(self, payload: Dict[str, Any], result: Dict[str, Any]) -> None:
        """Post-execution: log result, decide retry vs dead-letter, trigger LLM analysis."""
        activity_id = payload["activity_id"]
        shadow_id   = payload["shadow_id"]
        retry_count = payload.get("retry_count", 0)
        status      = result.get("status", "unknown")
        error       = result.get("error") or result.get("final_summary", "")

        sub_id = payload.get("submission_id", "")

        if status == "success":
            # Persist the terminal COMPLETED state so recovery sweeps and the
            # hourly sweep never re-queue an already-finished activity.
            try:
                from systemu.core.models import ActivityStatus
                activity = self.vault.get_activity(activity_id)
                activity.status = ActivityStatus.COMPLETED
                activity.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
                self.vault.save_activity(activity)
                logger.info("[Supervisor] Activity %s marked COMPLETED", activity_id)
            except Exception as exc:
                logger.warning(
                    "[Supervisor] Could not mark activity %s COMPLETED: %s", activity_id, exc
                )
            # [A.2] Mark DB row completed
            if self._task_queue is not None and sub_id:
                try:
                    self._task_queue.mark_completed(sub_id, result)
                except Exception as exc:
                    logger.warning("[Supervisor] SQLite queue mark_completed failed: %s", exc)
            self._publish(
                f"✅ Completed: {activity_id}",
                level="SUCCESS",
                context={"activity_id": activity_id, "shadow_id": shadow_id,
                         "result": result},
            )
            return

        # Cancelled by watchdog — no retry needed (watchdog already re-queued)
        if status == "cancelled":
            # [A.2] DB row was already marked by the watchdog path; nothing to do here.
            self._publish(
                f"🚫 Shadow cancelled (watchdog-requested): {activity_id}",
                level="WARNING",
                context={"activity_id": activity_id},
            )
            return

        # Partial or failure — decide retry
        if status in ("failure", "partial") and retry_count < MAX_RETRIES:
            wait_s = 5 * (retry_count + 1)   # back-off: 5s, 10s
            # [A.2] Mark this DB row as failed; the retry submit() creates a new row.
            if self._task_queue is not None and sub_id:
                try:
                    self._task_queue.mark_failed(sub_id, error[:500])
                except Exception as exc:
                    logger.warning("[Supervisor] SQLite queue mark_failed failed: %s", exc)
            self._publish(
                f"🔄 Scheduling retry {retry_count + 1}/{MAX_RETRIES} for {activity_id} "
                f"(in {wait_s}s) — reason: {error[:120]}",
                level="WARNING",
                context={"activity_id": activity_id, "retry_count": retry_count + 1},
            )
            threading.Timer(
                wait_s,
                self.submit,
                kwargs={
                    "activity_id": activity_id,
                    "shadow_id":   shadow_id,
                    "priority":    payload.get("priority", 5),
                    "reason":      f"retry-{retry_count + 1}",
                    "retry_count": retry_count + 1,
                },
            ).start()
        else:
            # Dead letter
            dl_entry = {
                "activity_id": activity_id,
                "shadow_id":   shadow_id,
                "status":      status,
                "error":       error,
                "retries":     retry_count,
                "failed_at":   datetime.now(timezone.utc).isoformat(),
            }
            with self._dl_lock:
                self._dead_letters.append(dl_entry)
            # [A.2] Mark DB row as dead_letter
            if self._task_queue is not None and sub_id:
                try:
                    self._task_queue.mark_dead_letter(sub_id, error[:500])
                except Exception as exc:
                    logger.warning("[Supervisor] SQLite queue mark_dead_letter failed: %s", exc)
            self._publish(
                f"💀 Dead-lettered: {activity_id} (exhausted {retry_count} retries) — {error[:200]}",
                level="ERROR",
                context=dl_entry,
            )

            # Trigger LLM diagnosis in background
            threading.Thread(
                target=self._analyze_failure,
                args=(payload, result),
                daemon=True,
                name="supervisor-diagnosis",
            ).start()

    # ── LLM Failure Diagnosis ─────────────────────────────────────────────────

    def _analyze_failure(self, payload: Dict[str, Any], result: Dict[str, Any]) -> None:
        """Call Tier-1 LLM to diagnose why a shadow failed and suggest fixes.
        Posts the analysis back to the Systemu Chat via EventBus.
        """
        activity_id = payload["activity_id"]
        shadow_id   = payload["shadow_id"]

        self._publish(
            f"🔍 Analyzing failure for {activity_id}…",
            context={"activity_id": activity_id},
        )

        recent_events = self._read_recent_events(limit=30)

        system_prompt = """You are the Systemu Supervisor — an intelligent orchestration layer
that monitors agentic shadow execution. Your job is to diagnose why a shadow failed
and recommend precise, actionable fixes.

Respond with a JSON object exactly matching this schema:
{
  "root_cause": "<2–3 sentence diagnosis of what went wrong>",
  "failure_category": "<one of: tool_missing | tool_error | scroll_flaw | think_storm | context_overflow | api_error | unknown>",
  "immediate_fix": "<specific action to take right now — file to edit, field to add, etc.>",
  "retry_recommended": <true|false>,
  "prevention": "<how to prevent this class of failure in future scrolls/shadows>"
}"""

        user_payload = {
            "shadow_id":     shadow_id,
            "activity_id":   activity_id,
            "status":        result.get("status"),
            "error":         result.get("error") or result.get("final_summary", ""),
            "recent_events": recent_events,
        }

        try:
            from systemu.core.llm_router import llm_call_json
            analysis = llm_call_json(
                tier=1,
                system=system_prompt,
                user=json.dumps(user_payload),
                config=self.config,
                temperature=0.2,
                max_tokens=512,
            )

            if isinstance(analysis, dict) and "root_cause" in analysis:
                self._publish(
                    f"🧠 Diagnosis for {activity_id}:\n"
                    f"• Cause: {analysis.get('root_cause', '?')}\n"
                    f"• Fix: {analysis.get('immediate_fix', '?')}\n"
                    f"• Retry: {'✅' if analysis.get('retry_recommended') else '❌'}",
                    level="INFO",
                    context={
                        "activity_id": activity_id,
                        "analysis":    analysis,
                        "type":        "failure_analysis",
                    },
                )
                # also mirror to failure_telemetry.jsonl so a single
                # file holds the full failure-mode history for histogram analysis.
                try:
                    from systemu.runtime.failure_telemetry import record_supervisor_diagnosis
                    record_supervisor_diagnosis(
                        shadow_id=shadow_id,
                        activity_id=activity_id,
                        diagnosis=analysis,
                    )
                except Exception:
                    logger.debug("[Supervisor] diagnosis telemetry skipped", exc_info=True)
                # also write a structured failure_patterns entry to
                # the shadow's memory_buffer so the next execution of this
                # shadow can recall the diagnosis.  Today the diagnosis only
                # reaches the dashboard event log — without this write the
                # learning loop ignores everything the post-mortem found.
                try:
                    from systemu.core.memory_types import pattern_signature
                    sig = pattern_signature(
                        error_type=analysis.get("failure_category"),
                        tool_name=None,
                        error_message=analysis.get("root_cause") or "",
                    )
                    lesson_text = (
                        (analysis.get("root_cause") or "").strip()[:300]
                        + " — Prevention: "
                        + (analysis.get("prevention") or "(none)").strip()[:200]
                    )
                    entry = {
                        "category":                 "failure_patterns",
                        "lesson":                   lesson_text[:500],
                        "evidence_action_blocks":   [],
                        "_source":                  "supervisor_diagnosis",
                        "_pattern_signature":       sig,
                        "_origin_activity_id":      activity_id,
                        "_failure_category":        analysis.get("failure_category"),
                        "_retry_recommended":       analysis.get("retry_recommended"),
                    }
                    if shadow_id:
                        self.vault.append_shadow_memory_buffer(
                            shadow_id, entry, source="supervisor_diagnosis",
                        )
                except Exception:
                    logger.debug(
                        "[Supervisor] diagnosis→memory write skipped", exc_info=True,
                    )
            else:
                self._publish(
                    f"🧠 Diagnosis complete for {activity_id} (raw): {str(analysis)[:300]}",
                    context={"activity_id": activity_id, "raw_analysis": str(analysis)},
                )
        except Exception as exc:
            logger.warning("[Supervisor] LLM diagnosis failed: %s", exc)
            self._publish(
                f"⚠️ Could not generate diagnosis for {activity_id}: {exc}",
                level="WARNING",
                context={"activity_id": activity_id},
            )

    # ── Heartbeat watchdog ────────────────────────────────────────────────────

    def _heartbeat_loop(self) -> None:
        """Background thread: detect stuck shadows and cancel + re-queue them."""
        loop_last_run_mono = time.monotonic()  # [A.3] track inter-tick gap for sleep detection
        while not self._shutdown_event.is_set():
            try:
                self._shutdown_event.wait(timeout=HEARTBEAT_INTERVAL_S)
                if self._shutdown_event.is_set():
                    break

                now_mono  = time.monotonic()
                gap_s     = now_mono - loop_last_run_mono
                loop_last_run_mono = now_mono

                # [A.3] A gap far larger than the normal interval means the host slept.
                # Firing the watchdog after a resume would produce false-positive "stuck"
                # detections for shadows that were simply paused with the host.
                # Reset all running-shadow heartbeat timestamps and skip this cycle.
                if gap_s > SLEEP_JUMP_THRESHOLD_S:
                    with self._running_lock:
                        for meta in self._running.values():
                            meta["last_heartbeat_at_mono"] = now_mono
                    self._publish(
                        f"😴 Host sleep detected ({gap_s:.0f}s gap) — "
                        f"resetting shadow heartbeats, skipping watchdog this cycle.",
                        level="INFO",
                        context={"gap_s": round(gap_s, 1)},
                    )
                    continue

                self._check_stuck_shadows()
            except Exception as exc:
                logger.exception("[Supervisor] Heartbeat error: %s", exc)

    def _check_stuck_shadows(self) -> None:
        """Detect shadows that have been silent for STUCK_THRESHOLD_S seconds.

        [FIX-3] We pop the stuck entry from _running immediately under the lock,
        then release the semaphore outside the lock.  This means:
          - The next watchdog tick won't see the same key → no cascading fires.
          - _run_shadow_guarded's finally will pop None → won't double-release.
          - [FIX-5] retry_count is taken from the running entry's payload, not the
            original submission payload, so it increments correctly.
          - [FIX-6] We set the cancel_event so the shadow exits at the next iteration
            boundary rather than burning API tokens as a zombie.
        """
        now      = time.time()       # wall-clock — for display and dead-letter timestamps only
        now_mono = time.monotonic()  # [A.3] monotonic — authoritative silence measurement

        with self._running_lock:
            stuck_entries = []
            for k, v in list(self._running.items()):
                # [A.3] Use monotonic clock; fall back to wall-clock for entries created
                # before this version (last_heartbeat_at_mono may not exist on old entries).
                mono_ref  = v.get("last_heartbeat_at_mono", v["last_heartbeat_at"])
                silence_s = now_mono - mono_ref if "last_heartbeat_at_mono" in v else now - v["last_heartbeat_at"]
                status    = v.get("status", "running")

                # Grace period for "starting" state (vault load / loop init can take time)
                if status == "starting" and silence_s < STUCK_THRESHOLD_S:
                    continue

                if silence_s > STUCK_THRESHOLD_S:
                    stuck_entries.append((k, v))

            # [FIX-3] Pop stuck entries from _running NOW, before releasing the lock.
            # This prevents any subsequent heartbeat tick from seeing these keys again.
            for k, _ in stuck_entries:
                self._running.pop(k, None)

        # Now handle the popped entries outside the lock
        for key, meta in stuck_entries:
            payload      = meta["payload"]
            cancel_event = meta.get("cancel_event")   # [FIX-6]
            # [A.3] Use monotonic reference for silence; fall back to wall-clock if absent
            if "last_heartbeat_at_mono" in meta:
                silence_s = now_mono - meta["last_heartbeat_at_mono"]
            else:
                silence_s = now - meta["last_heartbeat_at"]

            # [FIX-5] Correct retry count from the running entry's payload
            retry_count = payload.get("retry_count", 0) + 1

            self._publish(
                f"⚠️ Shadow stuck — {payload['activity_id']} silent for "
                f"{silence_s:.0f}s (retry_count was {retry_count - 1}) — cancelling",
                level="WARNING",
                context={"key": key, "payload": payload,
                         "silence_s": silence_s, "new_retry_count": retry_count},
            )

            # [FIX-6] Signal the shadow to exit cleanly at the next iteration boundary
            if cancel_event is not None:
                cancel_event.set()

            # [FIX-3] Release the semaphore slot we're reclaiming from the zombie thread
            self._semaphore.release()

            old_sub_id = payload.get("submission_id", "")

            # Re-queue or dead-letter
            if retry_count <= MAX_RETRIES:
                # [A.2] Mark the old DB row as failed; submit() creates a new row.
                if self._task_queue is not None and old_sub_id:
                    try:
                        self._task_queue.mark_failed(old_sub_id, f"watchdog-stuck after {silence_s:.0f}s")
                    except Exception as exc:
                        logger.warning("[Supervisor] SQLite queue watchdog mark_failed: %s", exc)
                self.submit(
                    payload["activity_id"],
                    payload["shadow_id"],
                    priority=payload.get("priority", 5),
                    reason="stuck-watchdog-retry",
                    retry_count=retry_count,   # [FIX-5] correct count
                )
            else:
                dl_entry = {
                    "activity_id": payload["activity_id"],
                    "shadow_id":   payload["shadow_id"],
                    "status":      "stuck",
                    "error":       f"Shadow stuck for >{STUCK_THRESHOLD_S}s, exhausted retries",
                    "retries":     retry_count,
                    "failed_at":   datetime.now(timezone.utc).isoformat(),
                }
                with self._dl_lock:
                    self._dead_letters.append(dl_entry)
                # [A.2] Mark the old DB row as dead_letter
                if self._task_queue is not None and old_sub_id:
                    try:
                        self._task_queue.mark_dead_letter(old_sub_id, dl_entry["error"])
                    except Exception as exc:
                        logger.warning("[Supervisor] SQLite queue watchdog dead_letter: %s", exc)
                self._publish(
                    f"💀 Dead-lettered (stuck + max retries): {payload['activity_id']}",
                    level="ERROR",
                    context=dl_entry,
                )
                threading.Thread(
                    target=self._analyze_failure,
                    args=(payload, {"status": "stuck", "error": dl_entry["error"]}),
                    daemon=True,
                    name="supervisor-diagnosis",
                ).start()

    def update_heartbeat(self, activity_id: str) -> None:
        """Called by the shadow runtime on each iteration to signal liveness.

        Updates the in-process ``_running`` dict (used by the local watchdog)
        AND the durable task queue's heartbeat record (used by remote
        recover_orphans in docker-enterprise mode).  Without the queue write,
        a worker on a separate host has no way to refresh its lease.
        """
        now_wall = time.time()
        now_mono = time.monotonic()  # [A.3]
        sub_id_to_refresh: Optional[str] = None
        with self._running_lock:
            for key, meta in self._running.items():
                if meta["payload"]["activity_id"] == activity_id:
                    meta["last_heartbeat_at"]      = now_wall
                    meta["last_heartbeat_at_mono"] = now_mono  # [A.3]
                    if meta.get("status") == "starting":
                        meta["status"] = "running"
                    sub_id_to_refresh = meta["payload"].get("submission_id")
                    break

        # Cross-host heartbeat refresh — best-effort.  If the queue isn't
        # configured (file backend / pure local-mem) this is a no-op.
        if sub_id_to_refresh and self._task_queue is not None:
            try:
                self._task_queue.update_heartbeat(sub_id_to_refresh)
            except Exception as exc:
                logger.debug("[Supervisor] queue update_heartbeat failed: %s", exc)

    # ── Queue persistence ─────────────────────────────────────────────────────

    def _persist_queue(self) -> None:
        """Save pending queue items to disk for crash recovery."""
        items: List[Dict[str, Any]] = []
        temp: List[Tuple[int, float, Dict[str, Any]]] = []

        while not self._queue.empty():
            try:
                item = self._queue.get_nowait()
                temp.append(item)
                items.append({"priority": item[0], "enqueued_at": item[1], "payload": item[2]})
            except queue.Empty:
                break

        # Put items back
        for item in temp:
            self._queue.put(item)

        try:
            self._queue_persist_path.write_text(json.dumps(items, indent=2))
            logger.info("[Supervisor] Persisted %d queue items to %s", len(items), self._queue_persist_path)
        except Exception as exc:
            logger.warning("[Supervisor] Could not persist queue: %s", exc)

    def _restore_queue(self) -> None:
        """Restore queue items from disk (called at startup).

        Also seeds _pending_activity_ids so submit()'s dedup guard correctly
        skips any activity that is already in the restored queue — preventing
        _resubmit_unexecuted_assigned() from adding a second copy.
        """
        if not self._queue_persist_path.exists():
            return
        try:
            items = json.loads(self._queue_persist_path.read_text())
            with self._pending_lock:
                for item in items:
                    payload = item.get("payload", {})
                    payload["reason"] = "restart-restore"
                    self._queue.put((item["priority"], item.get("enqueued_at", time.time()), payload))
                    aid = payload.get("activity_id")
                    if aid:
                        self._pending_activity_ids.add(aid)
            self._queue_persist_path.unlink()   # consumed
            logger.info("[Supervisor] Restored %d queue items from disk", len(items))
        except Exception as exc:
            logger.warning("[Supervisor] Could not restore queue: %s", exc)

    # ── A.2 — Durable queue startup helpers ──────────────────────────────────

    def _migrate_json_queue(self) -> None:
        """One-shot migration: import supervisor_queue.json into the SQLite queue.

        Runs only when task_queue is attached.  Renames the JSON file to .bak
        after import so it is never re-imported on subsequent startups.
        """
        if not self._queue_persist_path.exists():
            return
        try:
            items = json.loads(self._queue_persist_path.read_text())
            if items:
                count = self._task_queue.import_from_json(items)
                logger.info(
                    "[Supervisor] Migrated %d item(s) from supervisor_queue.json to SQLite queue",
                    count,
                )
            bak = self._queue_persist_path.with_suffix(".json.bak")
            self._queue_persist_path.rename(bak)
            logger.info("[Supervisor] Renamed supervisor_queue.json → %s", bak.name)
        except Exception as exc:
            logger.warning("[Supervisor] JSON queue migration failed: %s", exc)

    def _recover_orphans(self) -> None:
        """Load queued + orphaned DB rows into the in-memory queue at startup.

        Runs only when task_queue is attached.  Two passes:
          1. find 'queued' rows (not yet claimed) → put into in-memory queue
          2. find 'running' rows from other worker IDs past the lease window
             → requeue or dead_letter via task_queue.recover_orphans()
        """
        # Pass 1: restore unclaimed queued rows
        try:
            queued_rows = self._task_queue.list_queued()
            with self._pending_lock:
                for row in queued_rows:
                    aid = row["activity_id"]
                    if aid not in self._pending_activity_ids:
                        payload = {
                            "submission_id": row["submission_id"],
                            "activity_id":   aid,
                            "shadow_id":     row["shadow_id"],
                            "priority":      row["priority"],
                            "retry_count":   row["retry_count"],
                            "reason":        "db-restore",
                            "enqueued_at":   row["enqueued_at"],
                        }
                        self._queue.put((row["priority"], row["enqueued_at"], payload))
                        self._pending_activity_ids.add(aid)
            if queued_rows:
                logger.info(
                    "[Supervisor] Restored %d queued item(s) from SQLite queue into memory",
                    len(queued_rows),
                )
        except Exception as exc:
            logger.warning("[Supervisor] Could not restore queued rows from SQLite: %s", exc)

        # Pass 2: recover orphaned running rows
        try:
            recovered = self._task_queue.recover_orphans()
            for item in recovered:
                if item["action"] == "requeued":
                    # The DB row was re-queued; add back into in-memory queue too
                    aid = item["activity_id"]
                    with self._pending_lock:
                        if aid not in self._pending_activity_ids:
                            payload = {
                                "submission_id": item["submission_id"],
                                "activity_id":   aid,
                                "shadow_id":     item.get("shadow_id", ""),
                                "priority":      5,
                                "retry_count":   item["new_retry"],
                                "reason":        "crash-recovery",
                                "enqueued_at":   time.time(),
                            }
                            self._queue.put((5, time.time(), payload))
                            self._pending_activity_ids.add(aid)
            if recovered:
                logger.info(
                    "[Supervisor] Crash-recovered %d orphaned row(s) from SQLite queue",
                    len(recovered),
                )
        except Exception as exc:
            logger.warning("[Supervisor] Could not recover orphaned rows from SQLite: %s", exc)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _update_heartbeat(self, key: str, status: str) -> None:
        with self._running_lock:
            if key in self._running:
                self._running[key]["last_heartbeat_at"]      = time.time()
                self._running[key]["last_heartbeat_at_mono"] = time.monotonic()  # [A.3]
                self._running[key]["status"] = status

    def _publish(
        self,
        message: str,
        level: str = "INFO",
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Publish a supervisor event to the EventBus (non-blocking)."""
        try:
            from systemu.interface.event_bus import EventBus
            EventBus.get().publish_supervisor(message, level=level, context=context or {})
        except Exception as exc:
            logger.debug("[Supervisor] EventBus publish error: %s", exc)

    def _read_recent_events(self, limit: int = 30) -> List[Dict[str, Any]]:
        """Read recent events from the event log for LLM diagnosis context."""
        try:
            from systemu.interface.event_bus import EventBus
            buf = EventBus.get().get_buffer()
            return buf[-limit:]
        except Exception:
            return []
