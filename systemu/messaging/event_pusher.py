"""EventPusher — bridges the EventBus to a messaging gateway.

Subscribes to the EventBus and translates a curated subset of events
into :class:`OutboundMessage` pushes via the configured gateway.
Without this, the Telegram bot is submit-only — the operator can ask
"what's happening" but the system can't proactively tell them.

Filter philosophy
    Push only events an operator would care about *while away from the
    dashboard*:

    * approval requests (operator must respond)
    * shadow completion / failure
    * watchdog fires (shadow stuck, re-queued)
    * tool-forge proposals (when auto-forge is off)

    Per-iteration heartbeats, tool-call observations, and verbose log
    messages are explicitly NOT pushed — they'd spam the chat and the
    operator can drill in via the dashboard.

Rate limiting
    A sliding window per category keeps a runaway Shadow from filling
    the chat.  When the per-category budget is exceeded, additional
    pushes are dropped with a debug log; the next push that arrives
    outside the window goes through normally.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque
from typing import Any, Callable, Deque, Dict, Optional

from .gateway import OutboundMessage

logger = logging.getLogger(__name__)


# Per-category rate limits.  (max_pushes, window_seconds).  Approval
# requests are deliberately *not* limited — they're operator-driven and
# already infrequent.
_RATE_LIMITS: Dict[str, tuple[int, int]] = {
    "shadow":     (10, 60),    # 10 pushes per minute per category
    "supervisor": (5,  60),
    "approval":   (0,  0),     # 0 = no limit
}


class EventPusher:
    """Subscribe to an EventBus and push relevant events to a Gateway."""

    def __init__(
        self,
        gateway: Any,                          # systemu.messaging.gateway.Gateway
        *,
        translator: Optional[Callable[[Dict[str, Any]], Optional[OutboundMessage]]] = None,
        rate_limits: Optional[Dict[str, tuple[int, int]]] = None,
    ) -> None:
        self.gateway     = gateway
        self.translator  = translator or translate_event
        self.rate_limits = rate_limits or _RATE_LIMITS
        # Per-category sliding-window timestamp deques.
        self._window: Dict[str, Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()
        self._unsubscribe: Optional[Callable[[], None]] = None

    # ── Lifecycle ──────────────────────────────────────────────────

    def subscribe(self, bus: Any) -> None:
        """Attach this pusher to *bus* (EventBus instance)."""
        if self._unsubscribe is not None:
            return  # already subscribed — idempotent
        self._unsubscribe = bus.subscribe(self._handle, replay=False)
        logger.info("[EventPusher] subscribed to EventBus")

    def shutdown(self) -> None:
        if self._unsubscribe is not None:
            try:
                self._unsubscribe()
            except Exception:
                pass
            self._unsubscribe = None

    # ── Event handling ─────────────────────────────────────────────

    def _handle(self, event: Dict[str, Any]) -> None:
        """EventBus callback.  Must not block."""
        try:
            message = self.translator(event)
        except Exception as exc:
            logger.debug("[EventPusher] translator error: %s", exc)
            return
        if message is None:
            return

        category = event.get("category", "unknown")
        if not self._allow(category):
            logger.debug(
                "[EventPusher] rate-limited push for category=%s", category,
            )
            return

        try:
            self.gateway.push(message)
        except Exception as exc:
            logger.warning("[EventPusher] gateway push failed: %s", exc)

    def _allow(self, category: str) -> bool:
        """Sliding-window rate limit check.  Returns True if push allowed."""
        limit = self.rate_limits.get(category)
        if not limit:
            return True
        max_pushes, window_s = limit
        if max_pushes <= 0:
            return True
        now = time.time()
        with self._lock:
            q = self._window[category]
            # Evict expired entries
            cutoff = now - window_s
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) >= max_pushes:
                return False
            q.append(now)
            return True


# ─────────────────────────────────────────────────────────────────────────────
#  Default event-to-message translator (pure function — easy to test)
# ─────────────────────────────────────────────────────────────────────────────

def translate_event(event: Dict[str, Any]) -> Optional[OutboundMessage]:
    """Map an EventBus event dict to an OutboundMessage, or None to skip.

    Pure function — no side effects, no state.  Returning None means
    "don't push this event".

    The translation is deliberately conservative.  Adding a new push
    category should be a small, reviewable diff here.
    """
    category = (event.get("category") or "").lower()
    level    = (event.get("level") or "INFO").upper()
    message  = event.get("message") or ""
    ctx      = event.get("context") or {}

    # 0a) W10.1: pending operator decisions (the modern needs-you flow —
    # W5.3 made these events self-describing). Unlimited bucket: the
    # operator MUST hear about these while away. Resolutions are noise.
    if category == "operator_decision_posted":
        title = ctx.get("title") or message
        return OutboundMessage(
            text=(f"🔔 Needs you: {title}\n\n"
                  f"Open the dashboard Inbox to answer."),
            category="approval",
        )
    if category in {"operator_decision_resolved", "operator_decision_expired"}:
        return None

    # 0b) W10.1: task outcomes (W8's terminal events — quick lane + sync
    # workflow runs). Push the result with the summary head; per-iteration
    # quick_task events fall through to the default drop.
    if category == "task_outcome":
        details = event.get("details") or {}
        summary = str(details.get("summary") or "")[:300]
        icon = "✅" if level == "SUCCESS" else "⚠️"
        text = f"{icon} {message}"
        if summary:
            text += f"\n\n{summary}"
        files = details.get("files") or []
        if files:
            text += f"\n({len(files)} file(s) produced)"
        return OutboundMessage(text=text, category="execution")

    # 1) Approval requests — always push, regardless of level.
    if category == "approval":
        options = ctx.get("options") or ["Approve", "Reject"]
        return OutboundMessage(
            text=(
                f"🔔 Approval needed: {message}\n\n"
                f"{ctx.get('approval_message', '')}\n\n"
                f"Reply with /approve <scroll_id> or /reject <scroll_id>"
            ).strip(),
            category="approval",
        )

    # 2) Shadow completion or failure — push only terminal events.
    if category == "shadow":
        status = (ctx.get("status") or "").lower()
        if status in {"completed", "success", "done"}:
            return OutboundMessage(
                text=f"✅ Shadow finished: {message}",
                category="execution",
            )
        if status == "failed":
            return OutboundMessage(
                text=f"⚠️ Shadow failed: {message}",
                category="execution",
            )
        if level == "ERROR":
            return OutboundMessage(
                text=f"⚠️ Shadow error: {message}",
                category="execution",
            )
        return None  # skip per-iteration noise

    # 3) Supervisor watchdog + queue-state events.
    if category == "supervisor":
        if any(marker in message.lower() for marker in (
            "stuck", "re-queued", "requeued", "watchdog",
            "dead letter", "orphan",
        )):
            return OutboundMessage(
                text=f"⏰ Supervisor: {message}",
                category="watchdog",
            )
        return None

    # 4) Tool-forge proposals awaiting human review.
    if category in {"tool_forge", "tool"} and "proposed" in message.lower():
        return OutboundMessage(
            text=f"🔧 Tool proposed for review: {message}",
            category="info",
        )

    # Everything else — drop.
    return None
