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
import types
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from systemu.approval.exceptions import PendingOperatorDecision

logger = logging.getLogger(__name__)

# ── Harness suspend contract (v0.9.7) ─────────────────────────────────────────
HARNESS_SUSPEND_CONTRACT = """
Harness Escalation Suspend Contract — v0.9.7
============================================

When shadow_runtime receives a HARNESS ESCALATE (or ASK_OPERATOR) verdict from the
Governor it MUST do the following before returning control to the Supervisor:

1. SNAPSHOT — write an ExecutionSnapshot via ``execution_snapshot.write_snapshot``:

   Required fields in the snapshot
   --------------------------------
   execution_id           str   The current run's execution_id (already known).
   shadow_id              str   The running shadow's id.
   scroll_id              str   The scroll under execution.
   activity_id            str   The activity id (from the payload / context).
   iteration              int   The iteration index at suspension point.
   current_action_block   int   The action-block pointer at suspension point.
   completed_objective_ids list  All objectives completed SO FAR (do NOT re-do on resume).
   recent_history_slice   list  Last ≤12 events (tool_call/observation/thought) for LLM
                                 continuity — use ``capture_from_context`` or
                                 ``_build_history_slice`` (same helper used by recalibrate).
   sticky_notes           list  Existing sticky notes at suspension time.
   original_tool_id       str | None  Set only when the escalation is for a specific tool.
                                      None for ASK_OPERATOR / INPUT escalations.
   pending_harness_request dict  The serialised HarnessRequest that triggered the ESCALATE,
                                 stored as an ADDITIONAL sticky note with the key pattern:
                                     ``__HARNESS_PENDING__::<execution_id>::<json>``
                                 This lets resume_after_grant verify the request kind before
                                 injecting the grant payload.

2. SURFACE — post an operator-visible record so the dashboard can display a
   "Grant / Deny" decision card.  This is the controller's responsibility (not
   Supervisor's), but the snapshot must be written BEFORE the operator card is
   surfaced to avoid a race where the operator resolves faster than the snapshot
   lands.

3. SUSPEND — return a structured ``{"status": "suspended_harness_escalation",
   "execution_id": <id>, "activity_id": <id>, "shadow_id": <id>}`` result dict
   from ``runtime.execute()``.  The Supervisor's ``_handle_result`` will see
   ``status != "success"`` and NOT mark the activity COMPLETED — the activity
   stays ASSIGNED, waiting for ``resume_after_grant`` to re-queue it.

What resume_after_grant hands back
-----------------------------------
After the operator (or auto-grant logic) resolves the escalation,
``Supervisor.resume_after_grant(...)`` is called.  It:

  a. Reads the snapshot from disk.
  b. Appends a sticky note: ``__HARNESS_GRANT__::<execution_id>::<json>``
     where <json> is the ``grant_payload`` dict, e.g.:
       - TOOL grant:    ``{"granted_tool": "<name>", "tool_id": "<id>"}``
       - INPUT answer:  ``{"operator_answer": "<free text>"}``
       - DENY:          ``{"denied": true, "rationale": "<reason>"}``
  c. Re-writes the snapshot.
  d. Calls ``supervisor.submit(..., resume_from_execution_id=<execution_id>)``
     with ``priority=1`` so the resumed run jumps the queue.

On resume, shadow_runtime's existing ``resume_from_execution_id`` path applies
the snapshot (restored objectives, sticky notes, history slice) and then calls
``_apply_harness_grant`` to peel the ``__HARNESS_GRANT__`` sticky note and inject
the granted capability or operator answer into the live context before the LLM
iteration continues.

What shadow_runtime must NOT do
---------------------------------
- Do NOT re-raise a Python exception to terminate the thread — return a structured
  result dict so the Supervisor handles the suspension gracefully.
- Do NOT mark the activity COMPLETED or FAILED — leave status as ASSIGNED.
- Do NOT write the snapshot AFTER surfacing the operator card — write it FIRST
  (snapshot → surface → suspend, in that order).

Idempotency
-----------
``resume_after_grant`` checks for an existing ``__HARNESS_GRANT__`` sticky note on
the snapshot before re-submitting.  Double-call returns a sentinel string and does
NOT double-queue.  The snapshot write is atomic (temp-file swap via ``os.replace``).
"""

# ── Constants ─────────────────────────────────────────────────────────────────
MAX_CONCURRENT_SHADOWS  = 3      # max parallel shadow executions (default)


def _resolve_max_concurrent() -> int:
    """W7.4: the parallel-execution width, env-tunable.

    ``SYSTEMU_MAX_CONCURRENT_SHADOWS`` overrides the default (3). Invalid or
    non-positive values fall back to the default — the semaphore must never
    be created with 0 slots (the queue would deadlock).
    """
    import os as _os
    raw = _os.environ.get("SYSTEMU_MAX_CONCURRENT_SHADOWS", "")
    try:
        value = int(raw) if raw else MAX_CONCURRENT_SHADOWS
    except ValueError:
        return MAX_CONCURRENT_SHADOWS
    return value if value >= 1 else MAX_CONCURRENT_SHADOWS
MAX_RETRIES             = 2      # how many times to retry a failed activity


def _resolve_stuck_threshold() -> int:
    """W12 (audit F10): the no-heartbeat cancel window, env-tunable.

    Default 300s ([FIX-2] — LLM calls can take 130s each). Slow/preview
    models can stall one call past that, so operators running them widen
    the window via ``SYSTEMU_STUCK_THRESHOLD_S`` without forking code.
    Invalid or <60s values fall back to the default.
    """
    import os as _os
    try:
        value = int(_os.environ.get("SYSTEMU_STUCK_THRESHOLD_S", "") or 300)
    except ValueError:
        return 300
    return value if value >= 60 else 300


STUCK_THRESHOLD_S       = _resolve_stuck_threshold()
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
        # W7.4: width is env-tunable (SYSTEMU_MAX_CONCURRENT_SHADOWS, default 3).
        self._max_concurrent = _resolve_max_concurrent()
        self._semaphore = threading.Semaphore(self._max_concurrent)

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
            "max_concurrent": self._max_concurrent,
            "max_retries": MAX_RETRIES,
            "stuck_threshold_s": STUCK_THRESHOLD_S,
            "token_bucket_tpm": TOKEN_BUCKET_PER_MINUTE,
        })
        logger.info("[Supervisor] Started — max_concurrent=%d stuck_threshold=%ds",
                    self._max_concurrent, STUCK_THRESHOLD_S)

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
        origin: Optional[str] = None,
        chat_submission_id: Optional[str] = None,
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
                          v0.4.1-b "Retry with different shadow" operator
                          action — caller passes the shadow that just gave up.
            scroll_id:    Scroll id (used by the affinity-log consultation to
                          compute intent_hash; falls back to vault lookup when
                          omitted).
            consult_affinity_log: When True (default), check the v0.4.1-b
                          affinity log: if this (intent_hash, shadow_id) has
                          a recent TERMINATE, pick a different shadow before
                          queueing.  Set to False to skip the check (e.g. for
                          tests or operator-forced submissions).
            origin:       v0.8.16 — canonical trigger origin
                          ({chat,capture,manual,scheduled,system}).  When None,
                          it is derived from ``reason`` via ``coerce_origin``.
                          Stamped onto the queued event so the origin-partitioned
                          live panes can filter, and carried on the queue payload
                          so the worker can thread it into the runtime.

        Returns:
            A submission_id string for tracking.
        """
        # v0.8.16: resolve the canonical trigger origin once, up-front.  An
        # explicit `origin` wins; otherwise derive it from the submit `reason`.
        from systemu.core.models import coerce_origin
        resolved_origin = coerce_origin(origin or reason)
        # v0.4.2-a: Affinity-aware routing.  Two reasons to swap the assigned
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
            "origin":        resolved_origin,   # v0.8.16: trigger origin → worker → runtime
            "enqueued_at":   time.time(),
            # v0.5.1-e: resume hint carried in payload so the shadow runtime
            # can rebuild its context from the snapshot persisted by
            # RECALIBRATE_TOOL.  When None, runtime starts fresh.
            "resume_from_execution_id": resume_from_execution_id,
            # v0.8.22.1 (Fix 2): carry chat_submission_id so the worker can thread
            # it into runtime.execute — enables the inline decision card + resume
            # for queued chat tasks (not just the sync path).
            "chat_submission_id": chat_submission_id,
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
            f"📥 Activity queued: {self._aname(activity_id)} (priority={priority}, reason={reason}, retry={retry_count})",
            context={"submission_id": submission_id, "activity_id": activity_id,
                     "shadow_id": shadow_id, "priority": priority, "retry_count": retry_count,
                     "origin": resolved_origin},
            origin=resolved_origin,
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

        # v0.4.3-b: look up the originating shadow's specialty so we can
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
        # v0.4.3-a: ShadowMetrics second-tier ranking — when two shadows
        # both have skill_overlap=2, the one with a better track record on
        # this intent_hash wins.
        # v0.4.3-b: specialty tag breaks ties between metric-equivalent
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
            # v0.4.3-b: specialty match (1 when both have the same non-empty
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
        """v0.5.0-e: operator approved a recalibration — re-queue activity.

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
        # v0.5.1-e: also pass the prior execution_id so the new run can
        #    load + apply the snapshot RECALIBRATE_TOOL persisted (true
        #    resume — preserves completed objectives + recent context).
        #    When the snapshot is missing the runtime falls back to the
        #    v0.5.0 fresh-restart-with-sticky behaviour.
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

    # ── Harness-grant resume (v0.9.7) ─────────────────────────────────────────

    def resume_after_grant(
        self,
        *,
        execution_id: str,
        activity_id: str,
        shadow_id: str,
        grant_payload: Dict[str, Any],
        origin: Optional[str] = None,
        chat_submission_id: Optional[str] = None,
    ) -> str:
        """v0.9.7: operator (or auto-grant) resolved a harness ESCALATE — re-queue.

        This is the MECHANICAL re-dispatch path for ESCALATE (and ASK_OPERATOR)
        harness decisions.  It is intentionally symmetric with
        ``resume_after_recalibration`` and ``resume_on_decision._dispatch_resume``:
        all three ultimately call ``self.submit()`` with ``resume_from_execution_id``,
        so the existing snapshot → context-rebuild path in ``shadow_runtime.execute``
        applies unchanged.

        Parameters
        ----------
        execution_id:
            The ``execution_id`` of the suspended run whose snapshot was written
            by shadow_runtime when it raised the harness escalation.
        activity_id:
            The activity that was running.  Callers obtain this from the snapshot
            or from the persisted ``HarnessEscalation`` record.
        shadow_id:
            The shadow that was running.
        grant_payload:
            Operator answer or granted capability — written as a sticky note into
            the snapshot so the resumed run can recover it deterministically.
            For TOOL grants: ``{"granted_tool": "<tool_name>", "tool_id": "<id>"}``.
            For INPUT (ASK_OPERATOR): ``{"operator_answer": "<free text>"}``.
            For DENY/skip: ``{"denied": True, "rationale": "..."}``.
            The key ``__HARNESS_GRANT__::<execution_id>`` is used; shadow_runtime
            peels it off at resume-start via ``_apply_harness_grant``.
        origin:
            Trigger origin to carry through (defaults to ``"chat"``).
        chat_submission_id:
            Threading key for the chat resume card (same value the original run
            carried; lets the inline decision card close correctly).

        Returns
        -------
        str
            submission_id of the re-queued activity, or a ``"sub_no_dispatch_*"``
            sentinel when the call is a no-op (already dispatched or bad coords).

        Idempotency
        -----------
        The snapshot carries a ``__HARNESS_GRANT__`` sticky note stamped BEFORE
        the submit, keyed by ``execution_id``.  A double-call returns the sentinel
        ``"sub_already_dispatched_*"`` instead of double-queueing.

        Suspend contract (what shadow_runtime must do)
        -----------------------------------------------
        See the module-level ``HARNESS_SUSPEND_CONTRACT`` docstring in this file
        for the full specification of what shadow_runtime must write into the
        snapshot and what this method hands back.
        """
        # ── Idempotency guard — read snapshot; check for existing grant stamp ──
        try:
            from systemu.runtime.execution_snapshot import read_snapshot, write_snapshot
            from systemu.runtime.snapshot_migrations import SnapshotRefused
            snap = read_snapshot(execution_id)
            if snap is None:
                logger.warning(
                    "[Supervisor] resume_after_grant: no snapshot for execution_id=%s "
                    "— cannot resume (shadow_runtime must write snapshot before suspending)",
                    execution_id,
                )
                return f"sub_no_dispatch_{execution_id[:8]}"

            grant_key = f"__HARNESS_GRANT__::{execution_id}"
            already = any(n.startswith(grant_key) for n in snap.sticky_notes)
            if already:
                logger.info(
                    "[Supervisor] resume_after_grant: already dispatched for execution_id=%s — skipping",
                    execution_id,
                )
                return f"sub_already_dispatched_{execution_id[:8]}"

            # ── Stash the grant payload as a sticky note ───────────────────────
            # Encode as JSON so shadow_runtime can unpack it without parsing ambiguity.
            import json as _json
            snap.sticky_notes.append(
                f"{grant_key}::{_json.dumps(grant_payload, separators=(',', ':'))}"
            )
            write_snapshot(snap)
        except SnapshotRefused:
            # DEC-9: schema newer than this build supports. The re-submit below
            # re-reads the snapshot in shadow_runtime.execute, which refuses it at
            # the single fresh-vs-resume chokepoint — so the grant-resume fails
            # honestly there (terminal failure with a clear reason), NOT a fresh
            # start. Log loudly here instead of the vague "best-effort" line.
            logger.error(
                "[Supervisor] resume_after_grant: snapshot for %s is refused "
                "(schema newer than supported) — resume will be refused at "
                "execution; not re-executing fresh.", execution_id,
            )
        except Exception:
            logger.exception(
                "[Supervisor] resume_after_grant: snapshot read/write failed for %s "
                "— proceeding with re-submit anyway (best-effort)",
                execution_id,
            )

        # ── Re-submit with elevated priority — same shadow, skip affinity log ─
        resolved_origin = origin or "chat"
        reason = f"harness_grant:{execution_id}"[:120]
        sub_id = self.submit(
            activity_id=activity_id,
            shadow_id=shadow_id,
            priority=1,
            reason=reason,
            retry_count=0,            # Fix #7: a successful grant-resume is forward
            #                            progress, not a failure-retry — don't burn a slot.
            consult_affinity_log=False,
            resume_from_execution_id=execution_id,
            origin=resolved_origin,
            chat_submission_id=chat_submission_id,
        )
        logger.info(
            "[Supervisor] resume_after_grant: re-dispatched activity=%s shadow=%s "
            "execution_id=%s → submission=%s",
            activity_id, shadow_id, execution_id, sub_id,
        )
        self._publish(
            f"▶️ Harness grant resume: {self._aname(activity_id)} (exec={execution_id[:8]}…)",
            context={
                "activity_id":  activity_id,
                "shadow_id":    shadow_id,
                "execution_id": execution_id,
                "submission_id": sub_id,
            },
            origin=resolved_origin,
        )
        return sub_id

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
                    # v0.9.32 (review FIX 2): observability only — surfaces WHY a
                    # slot is cancelling ("operator"); no control flow reads it.
                    "cancel_reason":   v.get("cancel_reason"),
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
            "max_concurrent":    self._max_concurrent,
            "stuck_threshold_s": STUCK_THRESHOLD_S,
        }

    def request_cancel(self, key: str) -> bool:
        """Operator-requested cooperative cancel of one running shadow (D3.1).

        Sets ONLY the slot's ``cancel_event`` so the ReAct loop exits at its next
        iteration boundary (``shadow_runtime.py`` cancellation gate), and marks the
        entry ``cancelling`` (the watchdog skips ``cancelling`` slots so it never
        re-queues a run that is intentionally winding down).

        Operator vs. watchdog cancellation is distinguished STRUCTURALLY, not by
        reading ``cancel_reason``:
          - **Operator cancel** (here) leaves the key IN ``_running``. The worker
            runs to completion with status="cancelled" and reaches
            ``_handle_result``, which persists a terminal CANCELLED state and
            skips the post-mortem.
          - **Watchdog cancel** (``_check_stuck_shadows``) POPS the key from
            ``_running`` first, so that run's result never reaches
            ``_handle_result`` — it is re-queued or dead-lettered instead.
        ``cancel_reason="operator"`` is set purely as harmless observability (it
        is surfaced in ``get_status``); no control flow reads it.

        Critically does NOT pop the key or release the semaphore — the worker's
        ``finally`` (supervisor.py:1062) performs the single slot release; a
        second release here would let an extra shadow start. Returns True iff a
        running entry was found and signalled.
        """
        with self._running_lock:
            entry = self._running.get(key)
            if entry is None:
                return False
            ev = entry.get("cancel_event")
            if ev is not None:
                ev.set()
            entry["status"] = "cancelling"
            entry["cancel_reason"] = "operator"
        logger.info("[Supervisor] Operator cancel requested for key=%s", key)
        return True

    def request_cancel_by_activity(self, activity_id: str) -> bool:
        """Operator cancel keyed on activity_id — cancels every running slot whose
        payload matches (normally one). Returns True iff at least one was found."""
        keys = []
        with self._running_lock:
            for k, v in self._running.items():
                if v.get("payload", {}).get("activity_id") == activity_id:
                    keys.append(k)
        cancelled_any = False
        for k in keys:
            if self.request_cancel(k):
                cancelled_any = True
        return cancelled_any

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
                        origin=payload.get("origin"),
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
            f"▶️ Executing: {self._aname(activity_id)} (retry={retry_count})",
            context={"activity_id": activity_id, "shadow_id": shadow_id,
                     "retry_count": retry_count, "key": key},
            origin=payload.get("origin"),   # v0.8.16: lifecycle event partitions on origin
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

            # v0.5.1-e: thread the resume hint from the queue payload through
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
                        origin=payload.get("origin"),   # v0.8.16: thread trigger origin
                        chat_submission_id=payload.get("chat_submission_id"),  # v0.8.22.1 (Fix 2)
                    )
                )
            finally:
                loop.close()
                asyncio.set_event_loop(None)

        except PendingOperatorDecision as pd:
            # v0.9.32 (D.4 review FIX-1): a destructive shell command in this
            # queued/scheduled/background shadow hit the per-command approval
            # gate, which posted the operator card (in _maybe_gate_command)
            # BEFORE raising. The operator can still approve it from the Inbox.
            # We must STOP CLEANLY here — NOT fall into the generic `except`
            # below, which would mark status="failure" → retry-storm +
            # dead-letter + LLM post-mortem on something that is awaiting a
            # human, not broken. Clean fail-closed deny (Option 1): the run
            # did NOT execute the command; the operator re-runs the task after
            # approving. (NOT a resume — that's a deferred follow-up.)
            logger.info(
                "[Supervisor] Shadow %s blocked on command-approval gate "
                "(decision=%s, dedup=%s) — clean deny, no retry.",
                shadow_id, getattr(pd, "decision_id", "?"),
                getattr(pd, "dedup_key", "?"),
            )
            result = {
                "status": "command_gate_blocked",
                "error":  "command_gate",
                "final_summary": (
                    "Blocked: a shell command requires operator approval and "
                    "was NOT run. Approve it (Always allow) in the inbox, then "
                    "re-run the task."
                ),
            }

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

    @staticmethod
    def _should_retry(status: str, retry_count: int, structural: bool) -> bool:
        """Retry only a TRANSIENT partial/failure run that has retries left. A
        structural failure (a required tool persistently/structurally failed)
        won't be fixed by re-running, so it goes straight to terminal — no storm."""
        return (status in ("failure", "partial")
                and retry_count < MAX_RETRIES and not structural)

    def _arm_durable_retry(
        self,
        *,
        execution_id: Optional[str],
        activity_id: str,
        shadow_id: str,
        root_execution_id: Optional[str],
        scroll_id: str,
        delay_s: float,
        attempt: int,
        max_attempts: int,
        now: float,
    ) -> Optional[dict]:
        """R-A12a: arm a DURABLE retry wait on the run's ExecutionSnapshot.

        Replaces the in-process ``threading.Timer`` the retry path used to arm.
        A Timer is lost if the daemon restarts during the 5–10 s back-off window,
        so the transiently-failed activity is silently never retried. A
        ``pending_wait`` record persisted on the ExecutionSnapshot survives the
        restart; a separate reconciler fires it when ``fire_at`` is due and
        replays ``submit(activity_id, shadow_id, retry_count=attempt+1, …)`` — the
        record carries exactly those ids so the reconciler needs no other state.

        The gate is unchanged: this is only reached when ``_should_retry`` is True
        (i.e. ``attempt < max_attempts``), so the armed record is never exhausted.

        ``execution_id`` may be absent — the generic worker-thread exception path
        builds a result dict with no ``execution_id`` (and no ``root_execution_id``) —
        so we fall back to a synthetic key derived from the activity, the ``shadow_id``
        (this run's assigned shadow — run-stable but run-distinguishing), and the
        attempt. Folding ``shadow_id`` in makes the key RUN-UNIQUE: two DISTINCT runs
        of the same activity that both fail at the same attempt on this path get
        DISTINCT snapshot homes / ``wait_id``s, so a later run's retry is never deduped
        away against an earlier run's surviving (dropped-but-not-deleted) snapshot. It
        stays STABLE for the same run + attempt (no uuid4/now), so a re-arm of that
        same run + attempt is still idempotent — durability for exactly the recurring
        failures durability matters most for.

        Best-effort: a persistence failure is logged and must NOT crash
        result-handling — the daemon keeps running; the blast radius of a lost arm
        is the same one lost Timer as before. Returns the armed record, or None on
        failure. ``now`` (wall clock) is injected by the caller — it belongs at
        this arm site, never inside the pure ``pending_waits`` helpers.
        """
        from systemu.runtime import pending_waits as _pw
        from systemu.runtime.execution_snapshot import (
            ExecutionSnapshot, read_snapshot, write_snapshot,
        )

        # A missing execution_id (worker-thread exception path) still needs a durable
        # home + a stable dedupe key. Derive one from activity + shadow_id + attempt:
        # shadow_id makes the key RUN-UNIQUE (distinct runs → distinct keys) while
        # staying stable within a run+attempt (so a re-arm is still idempotent).
        eid = execution_id or f"retryarm-{activity_id}-{shadow_id}-{attempt}"
        rec = _pw.make_retry_wait(
            execution_id=eid,
            activity_id=activity_id,
            shadow_id=shadow_id,
            root_execution_id=root_execution_id or eid,
            delay_s=delay_s,
            attempt=attempt,
            max_attempts=max_attempts,
            now=now,
        )
        # Default None → the module's ./data dir, matching every other snapshot
        # writer (shadow_runtime + resume_after_grant) so the reconciler scans one
        # place. Tests set ``_snapshot_data_dir`` to isolate at a tmp dir.
        data_dir = getattr(self, "_snapshot_data_dir", None)
        try:
            snap = read_snapshot(eid, data_dir=data_dir)
            if snap is None:
                # No snapshot on disk (the common transient-failure case — the run
                # never suspended). Mint a minimal one whose sole job is to carry
                # the durable wait for the reconciler to find and fire.
                snap = ExecutionSnapshot(
                    execution_id=eid,
                    shadow_id=shadow_id,
                    scroll_id=scroll_id or "",
                    activity_id=activity_id,
                )
            # Dedupe by wait_id via arm_wait against a tiny shim exposing the same
            # ``_pending_waits`` attribute an ExecutionContext would — one source of
            # dedupe truth, shared with the shadow_runtime arm path.
            shim = types.SimpleNamespace(_pending_waits=list(snap.pending_waits or []))
            _pw.arm_wait(shim, rec)
            snap.pending_waits = shim._pending_waits
            write_snapshot(snap, data_dir=data_dir)
            return rec
        except Exception:
            logger.exception(
                "[Supervisor] durable retry-arm failed for %s (attempt %s) — "
                "the retry is NOT durable across a restart", activity_id, attempt,
            )
            return None

    def _expire_pending_waits_on_cancel(self, execution_id: Optional[str]) -> None:
        """R-A12a / IMPL-11: proactively expire a cancelled run's durable retry
        timers so none can ever resubmit a run the operator stopped.

        The cancelled result dict carries the run's ``execution_id`` (``build_result``
        stamps it), so we can locate the run's snapshot directly — read it, flag
        EVERY ``pending_wait`` ``dispatched`` (``expire_all``), and re-persist. This
        clears the timers PROMPTLY at cancel time rather than leaving them dangling
        until the ``external_wait_reconciler`` next skips / staleness-drops them (that
        per-tick ``_run_is_cancelled`` check remains the belt-and-braces guarantee —
        this is the proactive companion, not a replacement).

        CONC-MAP / DEC-10: supervisor.py is an allowed ``write_snapshot`` writer, and
        this runs on the cancelled run's OWN worker thread inside ``_handle_result``
        after the shadow loop has returned — the run's loop is no longer writing this
        snapshot, so there is no concurrent-writer race on the file.

        Best-effort: a missing execution_id (defensive), a snapshot that never
        existed, or any persistence hiccup must NEVER break the cancel finalizer —
        the terminal CANCELLED mark has already been written by the caller and the
        reconciler still bounds any surviving wait.
        """
        if not execution_id:
            return
        try:
            from systemu.runtime import pending_waits as _pw
            from systemu.runtime.execution_snapshot import read_snapshot, write_snapshot
            data_dir = getattr(self, "_snapshot_data_dir", None)
            snap = read_snapshot(execution_id, data_dir=data_dir)
            if snap is None or not snap.pending_waits:
                return
            snap.pending_waits = _pw.expire_all(snap.pending_waits)
            write_snapshot(snap, data_dir=data_dir)
        except Exception:
            logger.debug(
                "[Supervisor] expire-pending-waits-on-cancel failed for "
                "execution_id=%s — the reconciler's CANCELLED-check still bounds "
                "any surviving wait", execution_id, exc_info=True,
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
            # Wave 1.4: shared helper — the sync path (run_direct_task) uses
            # the same writer, so both execution modes agree on terminal state.
            from systemu.runtime.activity_completion import mark_activity_completed
            # getattr: the original inline code read self.vault INSIDE its
            # try/except, so a vault-less Supervisor (tests, partial init)
            # stayed best-effort — keep that exact tolerance.
            mark_activity_completed(getattr(self, "vault", None), activity_id)
            # [A.2] Mark DB row completed
            if self._task_queue is not None and sub_id:
                try:
                    self._task_queue.mark_completed(sub_id, result)
                except Exception as exc:
                    logger.warning("[Supervisor] SQLite queue mark_completed failed: %s", exc)
            self._publish(
                f"✅ Completed: {self._aname(activity_id)}",
                level="SUCCESS",
                context={"activity_id": activity_id, "shadow_id": shadow_id,
                         "result": result},
                origin=payload.get("origin"),   # v0.8.16
            )
            return

        # Cancelled — operator interrupt reaching the worker's _handle_result
        # (the watchdog path is zombie-suppressed before here, since the watchdog
        # popped the key from _running). Persist a terminal CANCELLED state (not a
        # failure) and skip the LLM post-mortem — an intentional stop is not a bug
        # to diagnose (D-6). No retry, no dead-letter.
        if status == "cancelled":
            try:
                from systemu.runtime.activity_completion import mark_activity_failed
                mark_activity_failed(getattr(self, "vault", None), activity_id,
                                     status="cancelled",
                                     summary="Cancelled by operator")
            except Exception:
                logger.warning("[Supervisor] mark_activity_failed(cancelled) failed for %s",
                               activity_id, exc_info=True)
            # R-A12a / IMPL-11: expire this run's durable pending_waits so a retry
            # timer can never resubmit a run the operator just cancelled. Best-effort
            # (own try/except inside) — a persistence hiccup must not break the cancel
            # finalizer, and the reconciler's per-tick CANCELLED-check still bounds any
            # surviving wait (defense in depth).
            self._expire_pending_waits_on_cancel(result.get("execution_id"))
            self._publish(
                f"🚫 Cancelled by operator: {self._aname(activity_id)}",
                level="WARNING",
                context={"activity_id": activity_id},
                origin=payload.get("origin"),   # v0.8.16
            )
            return

        # Blocked on the per-command approval gate (v0.9.32, D.4 review FIX-1).
        # A destructive shell command in this queued/scheduled/background run
        # raised PendingOperatorDecision; the gate ALREADY posted the operator
        # card before raising, so this is awaiting a human — NOT a bug. Persist
        # a terminal FAILED state (clean deny: the command did not run, the
        # operator re-runs the task after approving) and skip the LLM
        # post-mortem. Mirrors the cancelled branch: publish + early return; NO
        # retry, NO dead-letter, NO _analyze_failure (no storm).
        if status == "command_gate_blocked":
            summary = result.get("final_summary") or (
                "Blocked: a shell command requires operator approval and was "
                "NOT run. Approve it (Always allow) in the inbox, then re-run "
                "the task."
            )
            try:
                from systemu.runtime.activity_completion import mark_activity_failed
                mark_activity_failed(getattr(self, "vault", None), activity_id,
                                     status="failed", summary=summary)
            except Exception:
                logger.warning(
                    "[Supervisor] mark_activity_failed(command_gate_blocked) "
                    "failed for %s", activity_id, exc_info=True)
            self._publish(
                f"🔒 Needs approval (command gate): {self._aname(activity_id)} — {summary}",
                level="WARNING",
                context={"activity_id": activity_id, "shadow_id": shadow_id},
                origin=payload.get("origin"),   # v0.8.16
            )
            return

        # Parked on a blocking harness ESCALATE — the run snapshotted itself and
        # returned suspended_harness_escalation awaiting an operator harness
        # decision. Leave the activity ASSIGNED + its snapshot on disk; the
        # harness-grant reconciler resumes it via resume_after_grant once the
        # operator resolves the gate. NO retry, NO dead-letter — mirrors the
        # cancelled branch above (publish + early return; the running-set slot
        # and semaphore were already released by the caller's finally block).
        if status == "suspended_harness_escalation":
            logger.info(
                "[Supervisor] activity %s parked on harness escalation %s",
                activity_id, result.get("execution_id"),
            )
            self._publish(
                f"⏸️ Parked on harness escalation: {self._aname(activity_id)}",
                level="INFO",
                context={
                    "activity_id":  activity_id,
                    "shadow_id":    shadow_id,
                    "execution_id": result.get("execution_id"),
                },
                origin=payload.get("origin"),   # v0.8.16
            )
            return

        # Partial or failure — decide retry. A structural failure won't be fixed
        # by re-running the same activity (a required tool persistently failed),
        # so skip the retry storm and go straight to terminal.
        structural = bool(result.get("structural_failure"))
        if self._should_retry(status, retry_count, structural):
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
                origin=payload.get("origin"),   # v0.8.16
            )
            # R-A12a: arm a DURABLE retry wait on the run's ExecutionSnapshot
            # instead of an in-process ``threading.Timer``. A Timer is lost if the
            # daemon restarts during the 5–10 s back-off window → the activity is
            # silently never retried. The pending_wait record survives the restart;
            # a separate reconciler fires it when due and replays the essential
            # submit(...) kwargs it carries — activity_id, shadow_id, and
            # retry_count=attempt+1. Unlike the old Timer path (a FRESH submit with no
            # resume hint), the reconciler resubmits with
            # resume_from_execution_id=<the failed run's execution_id> — a RESUME from
            # the persisted snapshot/checkpoint, NOT a from-scratch re-run. This is
            # deliberately SAFER: the resumed run replays fewer effectful blocks (it
            # picks up after the last durable checkpoint rather than re-doing completed
            # work) and fail-closed-parks any external objective, so a transient
            # failure mid-run doesn't re-fire side effects on retry. priority/origin
            # are NOT carried by the record (the reconciler re-derives them, priority→5
            # / origin from reason).
            # The activity is NOT dead-lettered / marked terminally failed here — it
            # stays non-terminal (ASSIGNED) so the reconciler can resubmit it. (That
            # non-terminal ASSIGNED state is exactly what the reconciler's terminal-drop
            # check keys off: a COMPLETED/FAILED/CANCELLED activity is dropped, an
            # ASSIGNED retry-pending one still fires — see jobs._run_is_terminal.)
            self._arm_durable_retry(
                execution_id=result.get("execution_id"),
                activity_id=activity_id,
                shadow_id=shadow_id,
                root_execution_id=result.get("root_execution_id"),
                scroll_id=payload.get("scroll_id") or result.get("scroll_id") or "",
                delay_s=wait_s,
                attempt=retry_count,
                max_attempts=MAX_RETRIES,
                now=time.time(),
            )
        else:
            # Dead letter
            dl_entry = {
                "activity_id": activity_id,
                "shadow_id":   shadow_id,
                "status":      status,
                "error":       error,
                "retries":     retry_count,
                "structural":  structural,
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
            # Fix 2: terminal FAILED state so the activity isn't orphaned at
            # ASSIGNED (the recorded-task "zombie" RCA). Best-effort.
            try:
                from systemu.runtime.activity_completion import mark_activity_failed
                mark_activity_failed(getattr(self, "vault", None), activity_id,
                                     status=status, summary=error)
            except Exception:
                logger.warning("[Supervisor] mark_activity_failed failed for %s",
                               activity_id, exc_info=True)
            _why = ("structural blocker — not retried" if structural
                    else f"exhausted {retry_count} retries")
            self._publish(
                f"💀 Dead-lettered: {self._aname(activity_id)} ({_why}) — {error[:200]}",
                level="ERROR",
                context=dl_entry,
                origin=payload.get("origin"),   # v0.8.16
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
            origin=payload.get("origin"),
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
                    f"🧠 Diagnosis for {self._aname(activity_id)}:\n"
                    f"• Cause: {analysis.get('root_cause', '?')}\n"
                    f"• Fix: {analysis.get('immediate_fix', '?')}\n"
                    f"• Retry: {'✅' if analysis.get('retry_recommended') else '❌'}",
                    level="INFO",
                    context={
                        "activity_id": activity_id,
                        "analysis":    analysis,
                        "type":        "failure_analysis",
                    },
                    origin=payload.get("origin"),
                )
                # v0.4.0-0: also mirror to failure_telemetry.jsonl so a single
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
                # v0.4.0-c: also write a structured failure_patterns entry to
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
                    origin=payload.get("origin"),
                )
        except Exception as exc:
            logger.warning("[Supervisor] LLM diagnosis failed: %s", exc)
            self._publish(
                f"⚠️ Could not generate diagnosis for {activity_id}: {exc}",
                level="WARNING",
                context={"activity_id": activity_id},
                origin=payload.get("origin"),
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

                # v0.9.32 (review FIX 3): an operator-cancelled slot is winding down,
                # not stuck. Skip it — otherwise its silence (the ReAct loop has
                # stopped emitting heartbeats) trips the watchdog, which pops and
                # re-submits the very run the operator asked to stop.
                if status == "cancelling":
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
                origin=payload.get("origin"),   # v0.8.16
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
                    origin=payload.get("origin"),   # v0.8.16: preserve origin across retries
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
                    f"💀 Dead-lettered (stuck + max retries): {self._aname(payload['activity_id'])}",
                    level="ERROR",
                    context=dl_entry,
                    origin=payload.get("origin"),   # v0.8.16
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

    def _aname(self, activity_id: str) -> str:
        """Resolve an activity id to its name for operator-facing event text."""
        try:
            from systemu.interface.name_resolver import resolve_name
            return resolve_name(activity_id, self.vault)
        except Exception:
            return activity_id

    def _publish(
        self,
        message: str,
        level: str = "INFO",
        context: Optional[Dict[str, Any]] = None,
        origin: Optional[str] = None,
    ) -> None:
        """Publish a supervisor event to the EventBus (non-blocking).

        v0.8.16: when *origin* is supplied, the event is published with a
        top-level ``origin`` key so the origin-partitioned live panes can
        filter on it; otherwise we use the plain ``publish_supervisor`` path.
        """
        try:
            from systemu.interface.event_bus import EventBus
            if origin is not None:
                from datetime import datetime, timezone
                EventBus.get().publish({
                    "ts":       datetime.now(timezone.utc).isoformat(),
                    "level":    level.upper(),
                    "category": "supervisor",
                    "message":  message,
                    "context":  context or {},
                    "origin":   origin,
                })
            else:
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
