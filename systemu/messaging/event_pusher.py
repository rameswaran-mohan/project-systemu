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
import os
import threading
import time
from collections import defaultdict, deque
from typing import Any, Callable, Deque, Dict, List, Optional

from .gateway import InlineButton, OutboundMessage, mask_outbound

logger = logging.getLogger(__name__)


def _decision_resolution_on() -> bool:
    """R-P1 master switch: is Telegram-driven decision resolution enabled?

    Reads the env directly (mirrors ``Config.messaging_decision_resolution``'s
    ``default_factory``) so this pure translator needs no Config instance. ON
    unless the var is off/false/0.
    """
    return os.getenv("SHARING_ON_MESSAGING_DECISION_RESOLUTION", "on").lower() \
        not in ("off", "false", "0")


def _push_detail() -> str:
    """R-P1: how much context to include in a decision push (summary|full)."""
    return os.getenv("SHARING_ON_MESSAGING_PUSH_DETAIL", "summary").lower()


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
    #
    # R-P1: extend the push with a short [tag] + inline option buttons (or a
    # /answer reply hint, or a "needs the dashboard" note), keyed on the parked
    # decision's PERSISTED resolution_class + shape. Buttons are opt-in per
    # recognized shape, never the default. Every span here is MASK-redacted (the
    # gateway masks again — belt and suspenders).
    if category == "operator_decision_posted":
        return _render_decision_push(ctx, fallback_title=message)
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


# ─────────────────────────────────────────────────────────────────────────────
#  R-P1 decision push rendering
# ─────────────────────────────────────────────────────────────────────────────

class _DecShim:
    """Minimal decision-like object (``.options`` + ``.context``) built from the
    enriched ``operator_decision_posted`` event so ``render_options`` — which
    reads only those two attributes — can run without a vault lookup."""

    __slots__ = ("options", "context")

    def __init__(self, options: List[str], context: Dict[str, Any]) -> None:
        self.options = list(options or [])
        self.context = dict(context or {})


_DASHBOARD_HINT = "Open the dashboard Inbox to answer."


def _render_decision_push(
    ctx: Dict[str, Any], *, fallback_title: str
) -> Optional[OutboundMessage]:
    """Build the OutboundMessage for a posted operator decision (R-P1).

    ``ctx`` is the enriched event context: ``title``, ``options``, ``tag``, and
    ``decision_context`` (the persisted ``OperatorDecision.context``, carrying
    ``resolution_class`` / ``gate_type`` / ``requested_schema`` / ``body``).

    Honours ``messaging_decision_resolution`` (off ⇒ old dashboard text, no
    buttons) and ``messaging_push_detail`` (summary|full). MASK-redacts the
    detail (the gateway masks again).
    """
    from .decision_bridge import (
        callback_token,
        render_options,
        SURFACE_BUTTONS,
        SURFACE_REPLY,
    )

    title = ctx.get("title") or fallback_title
    tag = ctx.get("tag") or ""
    options = ctx.get("options") or []
    decision_context = ctx.get("decision_context") or {}

    headline = mask_outbound(f"[{tag}] {title}" if tag else f"Needs you: {title}")

    # Master switch off, or no tag to reference — fall back to the old
    # dashboard-only push (no buttons, no remote-resolution affordance).
    if not _decision_resolution_on() or not tag:
        return OutboundMessage(
            text=f"🔔 {headline}\n\n{_DASHBOARD_HINT}",
            category="approval",
        )

    decision = _DecShim(options, decision_context)
    surface_hint, rendered = render_options(decision, tag=tag)

    # Optional detail body (full mode only), MASK-redacted.
    detail = ""
    if _push_detail() == "full":
        body = str(decision_context.get("body") or "").strip()
        if body:
            detail = "\n\n" + mask_outbound(body)

    if surface_hint == SURFACE_BUTTONS and rendered:
        buttons = [
            InlineButton(label=mask_outbound(label), callback=callback_token(tag, key))
            for key, label in rendered
        ]
        return OutboundMessage(
            text=f"🔔 {headline}{detail}",
            inline_buttons=buttons,
            category="approval",
        )

    if surface_hint == SURFACE_REPLY:
        return OutboundMessage(
            text=(f"🔔 {headline}{detail}\n\n"
                  f"Reply with /answer {tag} <value>."),
            category="approval",
        )

    # dashboard_only (floor / absent class / multi-field / 5+ / unknown shape).
    return OutboundMessage(
        text=f"🔔 {headline}{detail}\n\n{_DASHBOARD_HINT}",
        category="approval",
    )
