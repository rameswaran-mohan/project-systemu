"""EventBus — thread-safe publish/subscribe for real-time systemu events.

Published to by:  log_event(), ShadowRuntime (heartbeat), Supervisor
Consumed by:      Systemu Chat UI (deque + ui.timer drain pattern)

Design:
  • Thread-safe subscriber list via threading.RLock (re-entrant)
  • Publish is non-blocking: subscriber callbacks run synchronously on the
    publishing thread — callbacks MUST NOT block (UI uses deque + timer)
  • In-memory ring buffer (MAX_BUFFER=500) — late-joining pages see history
  • Singleton via EventBus.get()
  • Approval request/response: blocking threading.Event pattern so the
    shadow thread can pause awaiting user confirmation from the UI
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

MAX_BUFFER = 500   # ring-buffer size — late-joining subscribers see this much history


# ─────────────────────────────────────────────────────────────────────────────

class EventBus:
    """Singleton pub/sub event bus.  Thread-safe; callbacks must be non-blocking."""

    _instance: Optional["EventBus"] = None
    _init_lock: threading.Lock = threading.Lock()

    def __init__(self) -> None:
        self._subscribers: List[Callable[[Dict[str, Any]], None]] = []
        self._sub_lock = threading.RLock()
        self._buffer: deque[Dict[str, Any]] = deque(maxlen=MAX_BUFFER)

        # Approval gate: request_id → {event: threading.Event, choice: str | None}
        self._approval_requests: Dict[str, Dict[str, Any]] = {}
        self._approval_lock = threading.Lock()

    # ── Singleton ─────────────────────────────────────────────────────────────

    @classmethod
    def get(cls) -> "EventBus":
        """Return (or lazily create) the process-wide singleton."""
        if cls._instance is None:
            with cls._init_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    # ── Subscribe / unsubscribe ───────────────────────────────────────────────

    def subscribe(
        self,
        callback: Callable[[Dict[str, Any]], None],
        *,
        replay: bool = True,
    ) -> Callable[[], None]:
        """Register *callback* to receive all future published events.

        Args:
            callback: Called with each event dict.  **Must not block.**
            replay:   When True, immediately replay the ring buffer so the
                      subscriber catches up to the current state.

        Returns:
            An unsubscribe callable — invoke it to stop receiving events.
        """
        with self._sub_lock:
            if callback not in self._subscribers:
                self._subscribers.append(callback)
            if replay:
                snapshot = list(self._buffer)   # safe copy under lock

        if replay:
            for event in snapshot:
                try:
                    callback(event)
                except Exception as exc:
                    logger.debug("[EventBus] Replay error: %s", exc)

        def _unsubscribe() -> None:
            with self._sub_lock:
                try:
                    self._subscribers.remove(callback)
                except ValueError:
                    pass

        return _unsubscribe

    # ── Publish ───────────────────────────────────────────────────────────────

    def publish(self, event: Dict[str, Any]) -> None:
        """Publish *event* to all subscribers and append to the ring buffer.

        Non-blocking: all callbacks run synchronously on the caller's thread.
        Callbacks that raise are caught and logged — they never abort the publish.
        """
        # Stamp a monotonic sequence number so UI can detect missed events
        event.setdefault("ts", datetime.now(timezone.utc).isoformat())

        self._buffer.append(event)

        with self._sub_lock:
            subs = list(self._subscribers)  # snapshot under lock

        for sub in subs:
            try:
                sub(event)
            except Exception as exc:
                logger.debug("[EventBus] Subscriber error: %s", exc)

    def get_buffer(self) -> List[Dict[str, Any]]:
        """Thread-safe snapshot of the ring buffer (for late-joining pages)."""
        with self._sub_lock:  # RLock allows this even if called during replay
            return list(self._buffer)

    # ── Convenience publishers ────────────────────────────────────────────────

    def publish_supervisor(
        self,
        message: str,
        *,
        level: str = "INFO",
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Publish a supervisor-sourced event (category='supervisor')."""
        self.publish({
            "ts":       datetime.now(timezone.utc).isoformat(),
            "level":    level.upper(),
            "category": "supervisor",
            "message":  message,
            "context":  context or {},
        })

    def publish_shadow(
        self,
        message: str,
        shadow_id: str,
        execution_id: str,
        *,
        level: str = "INFO",
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Publish a shadow-execution event (category='shadow')."""
        ctx = context or {}
        ctx.update({"shadow_id": shadow_id, "execution_id": execution_id})
        self.publish({
            "ts":       datetime.now(timezone.utc).isoformat(),
            "level":    level.upper(),
            "category": "shadow",
            "message":  message,
            "context":  ctx,
        })

    # ── Approval gate ─────────────────────────────────────────────────────────

    def request_approval(
        self,
        request_id: str,
        title: str,
        message: str,
        options: List[str],
        *,
        context: Optional[Dict[str, Any]] = None,
        timeout_s: float = 120.0,
        default: str = "",
    ) -> str:
        """Block the caller's thread until the UI (or timeout) resolves the request.

        Publishes an 'approval_request' event; the UI shows a dialog and calls
        resolve_approval(request_id, choice).  Returns the chosen option string,
        or *default* on timeout.

        Args:
            request_id: Unique id (uuid4 recommended).
            title:      Short heading for the dialog.
            message:    Body text / question.
            options:    List of button labels, e.g. ["Approve", "Reject"].
            context:    Extra data attached to the event (e.g. shadow_id).
            timeout_s:  Seconds to wait before auto-selecting *default*.
            default:    Auto-selected on timeout; falls back to options[0].

        Returns:
            The chosen option string.
        """
        if not default and options:
            default = options[0]

        gate: Dict[str, Any] = {
            "event":  threading.Event(),
            "choice": None,
        }

        with self._approval_lock:
            self._approval_requests[request_id] = gate

        self.publish({
            "ts":       datetime.now(timezone.utc).isoformat(),
            "level":    "WARNING",
            "category": "approval",
            "message":  title,
            "context": {
                **(context or {}),
                "request_id": request_id,
                "approval_message": message,
                "options": options,
                "default": default,
            },
        })

        resolved = gate["event"].wait(timeout=timeout_s)

        with self._approval_lock:
            self._approval_requests.pop(request_id, None)

        choice = gate.get("choice") or default
        if not resolved:
            logger.warning(
                "[EventBus] Approval '%s' timed out after %.0fs — using default '%s'",
                request_id, timeout_s, default,
            )
            self.publish_supervisor(
                f"⏰ Approval timed out — auto-selected '{default}'",
                level="WARNING",
                context={"request_id": request_id},
            )

        return choice or default

    def resolve_approval(self, request_id: str, choice: str) -> bool:
        """Called by the UI to resolve a pending approval request.

        Returns True if the request was found and resolved, False otherwise.
        """
        with self._approval_lock:
            gate = self._approval_requests.get(request_id)

        if gate is None:
            logger.debug("[EventBus] resolve_approval: unknown request_id %s", request_id)
            return False

        gate["choice"] = choice
        gate["event"].set()
        logger.info("[EventBus] Approval '%s' resolved: %s", request_id, choice)
        return True

    def has_pending_approval(self, request_id: str) -> bool:
        """True if the given approval is still awaiting resolution."""
        with self._approval_lock:
            return request_id in self._approval_requests

    def list_pending_approvals(self) -> List[str]:
        """Return all currently pending approval request_ids."""
        with self._approval_lock:
            return list(self._approval_requests.keys())

    # ── Non-blocking out-of-band approvals (v0.3.6) ──────────────────────────
    #
    # Some approvals — notably tool pip-dependency approvals — are resolved
    # asynchronously on a dedicated UI surface (the Tools page) rather than
    # by clicking inline in the chat feed.  For these we publish a card with
    # ``redirect_to`` set; the chat renderer shows a one-click navigation
    # button instead of resolve actions, and the caller continues without
    # waiting for the operator.
    #
    # In-memory dedup map: package → last_published_count.  Keeps a single
    # card in the feed even when dozens of shadows independently hit the
    # same missing dep — only the first occurrence (and meaningful count
    # bumps) re-publish.

    def __dep_publish_state(self) -> Dict[str, int]:
        # Lazily attach so existing instances still work.
        if not hasattr(self, "_dep_publish_counts"):
            self._dep_publish_counts = {}                 # type: ignore[attr-defined]
        return self._dep_publish_counts                   # type: ignore[attr-defined]

    def publish_dep_approval_request(
        self,
        package: str,
        *,
        tool_name: str,
        tool_id: Optional[str] = None,
        request_count: int = 1,
        pending_total: Optional[int] = None,
        republish_thresholds: tuple = (1, 5, 25, 100),
    ) -> bool:
        """Flash a non-blocking approval card in the Systemu Chat feed.

        Args:
            package:        pip name awaiting approval.
            tool_name:      Tool that first encountered the missing dep.
            tool_id:        Vault id of that tool (for audit).
            request_count:  How many times this dep has been requested
                            across processes (operator allow-list tracks this).
            pending_total:  Total pending approvals when this fires (badge hint).
            republish_thresholds: Counts at which to re-publish (so a long-
                            running operator absence is noticed).  Default
                            republishes at first request and at 5/25/100×.

        Returns:
            True when a card was published, False when it was deduped.
        """
        counts = self.__dep_publish_state()
        previous = counts.get(package, 0)
        threshold_hit = any(
            previous < t <= request_count for t in republish_thresholds
        )
        if previous and not threshold_hit:
            return False
        counts[package] = request_count

        msg_lines = [
            f"Tool '{tool_name}' needs to install Python package '{package}'.",
            "",
            "Review and approve on the Tools page so the runtime may install it.",
        ]
        if request_count > 1:
            msg_lines.append(f"Requests seen so far: {request_count}")
        if pending_total and pending_total > 1:
            msg_lines.append(f"({pending_total} package(s) currently pending in total.)")

        self.publish({
            "ts":       datetime.now(timezone.utc).isoformat(),
            "level":    "WARNING",
            "category": "approval",
            "message":  f"📦 Tool dependency awaits approval: {package}",
            "context": {
                "approval_message": "\n".join(msg_lines),
                "options":          [],          # signals "no inline resolve"
                "redirect_to":      "/tools",
                "dedup_key":        f"dep-install:{package}",
                "package":          package,
                "tool_name":        tool_name,
                "tool_id":          tool_id,
                "request_count":    request_count,
                "pending_total":    pending_total,
            },
        })
        return True

    def publish_dep_approval_dismissed(
        self,
        package: str,
        *,
        outcome: str = "approved",
    ) -> None:
        """Close any open dep-approval card for ``package`` in the chat feed.

        ``outcome`` is folded into the dismissal message ("approved" /
        "revoked" / "expired") so the renderer can pick an appropriate
        glyph.  Dedup state for the package is reset so a future
        BLOCKED_PENDING_APPROVAL would re-publish.
        """
        counts = self.__dep_publish_state()
        counts.pop(package, None)
        self.publish({
            "ts":       datetime.now(timezone.utc).isoformat(),
            "level":    "SUCCESS" if outcome == "approved" else "INFO",
            "category": "approval_dismissed",
            "message":  f"📦 Dep '{package}' {outcome}",
            "context": {
                "dedup_key": f"dep-install:{package}",
                "package":   package,
                "outcome":   outcome,
            },
        })

    def reset_dep_publish_state_for_tests(self) -> None:
        """Clear the dep-card dedup map.  ONLY for tests."""
        self.__dep_publish_state().clear()

    # ── Supervisor strategy-stream (v0.4.1-d) ────────────────────────────
    # Lightweight publisher for the Intelligent Supervisor's per-decision
    # ticks.  The chat feed renders these as compact inline cards under a
    # "Supervisor" filter so operators can watch the supervisor reason in
    # real time without diving into the audit JSONL file on disk.
    #
    # Categorically distinct from approval cards (which require operator
    # action): supervisor_action events are informational + auto-grouped
    # by execution_id.

    def publish_supervisor_action(
        self,
        *,
        execution_id: str,
        action:       str,
        rationale:    str = "",
        tier_used:    Optional[str] = None,
        classifier:   Optional[str] = None,
        consec_failures: int = 0,
        iteration:    int = 0,
        shadow_id:    Optional[str] = None,
        pattern_signature: Optional[str] = None,
    ) -> None:
        """Emit a strategy-stream tick to the chat feed.

        Always informational — no operator response expected.  The chat
        renderer groups these by execution_id and shows a compact action
        glyph plus a collapsible rationale.
        """
        self.publish({
            "ts":       datetime.now(timezone.utc).isoformat(),
            "level":    "INFO",
            "category": "supervisor_action",
            "message":  f"🧠 Supervisor → {action}",
            "context": {
                "execution_id":      execution_id,
                "supervisor_action": action,
                "rationale":         (rationale or "")[:400],
                "tier_used":         tier_used,
                "classifier":        classifier,
                "consec_failures":   consec_failures,
                "iteration":         iteration,
                "shadow_id":         shadow_id,
                "pattern_signature": pattern_signature,
            },
        })
