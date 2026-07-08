"""Default command handlers for messaging gateways.

Each handler is a pure function from an ``InboundCommand`` to a reply
string.  Handlers reach into the vault and the supervisor via the
``AppState`` singleton — the same surface the dashboard pages use.

To customise: build your own mapping ``{cmd_name: handler}`` and pass
it to ``TelegramGateway(command_handlers=...)``.
"""

from __future__ import annotations

import logging
from typing import Callable, Dict

from .gateway import InboundCommand
from .decision_bridge import resolve_from_channel

logger = logging.getLogger(__name__)

# Recognized choice keys for /answer <tag> <choice>. A bare 1..4 maps to a1..a4.
_ANSWER_CHOICE_KEYS = {"a1", "a2", "a3", "a4"}
_ANSWER_USAGE = (
    "Usage: /answer <tag> <a1..a4>\n"
    "The <tag> is the short code on the decision message; the choice is the "
    "option to pick (a1, a2, a3, a4 — or just 1, 2, 3, 4)."
)


# ─────────────────────────────────────────────────────────────────────────────
#  Handlers
# ─────────────────────────────────────────────────────────────────────────────

def _state():
    """Lazy AppState access — handlers are constructed at import time, but
    the AppState singleton isn't ready until the daemon's run_dashboard()
    has finished its bootstrap."""
    from systemu.interface.dashboard_state import AppState
    return AppState.get()


def handle_help(_: InboundCommand) -> str:
    return (
        "Available commands:\n"
        "/chat <prompt>        — submit a task (defaults to Queue mode)\n"
        "/status               — running tasks + queue depth\n"
        "/scrolls              — list recent scrolls\n"
        "/activities           — list recent activities\n"
        "/shadows              — list shadows\n"
        "/approve <scroll_id>  — approve a pending scroll\n"
        "/reject  <scroll_id>  — reject a pending scroll\n"
        "/answer <tag> <a1-a4> — resolve a parked decision (or tap its button)\n"
        "/help                 — this message\n"
        "\nPlain text without a leading / is treated as /chat."
    )


def handle_chat(cmd: InboundCommand) -> str:
    """Submit a chat task — routes through direct_task with route_through_supervisor=True."""
    if not cmd.args:
        return "Usage: /chat <prompt>"
    try:
        from systemu.pipelines.direct_task import submit_chat_task
        submission_id = submit_chat_task(
            prompt=cmd.args,
            state=_state(),
            route_through_supervisor=True,
            source=f"telegram:{cmd.user_id}",
        )
        return (
            f"✓ Submitted (id: {submission_id}).  "
            f"Use /status to check progress."
        )
    except ImportError:
        # submit_chat_task is an aspirational helper; fall back to a direct
        # queue submission so the gateway at least confirms receipt.
        return f"Queued: {cmd.args[:120]}.  (No chat pipeline available in this build.)"
    except Exception as exc:
        logger.exception("[Handler] /chat failed")
        return f"Sorry — /chat failed: {exc}"


def build_status_handler(vault) -> Callable[[InboundCommand], str]:
    """W10.1 — vault-scoped /status: pending-attention count (gates AND
    asks, the W5.1 accounting) + recent tasks with outcomes. Richer than the
    queue-only handle_status and testable without AppState. Never raises —
    the gateway must always be able to reply."""
    def _status(_cmd) -> str:
        try:
            from systemu.interface.components.attention import needs_you_total
            from systemu.interface.components.status_menu import build_status_rows
            pending = needs_you_total(vault)
            rows = build_status_rows(vault, limit=5)
            lines = [f"Needs you: {pending} item(s) pending."]
            if rows:
                lines.append("Recent tasks:")
                for row in rows:
                    lines.append(f"• [{row['status']}] {row['name']}")
            else:
                lines.append("No tasks yet.")
            lines.append("Act on items from the dashboard Inbox.")
            return "\n".join(lines)
        except Exception:
            logger.debug("[Handler] vault /status failed", exc_info=True)
            return "Status unavailable right now — check the dashboard."
    return _status


def handle_status(_: InboundCommand) -> str:
    try:
        state = _state()
        status = state.queue.get_status()
        depth   = status.get("queue_depth", 0)
        running = status.get("running", [])
        if not running and depth == 0:
            return "Idle — no running or queued tasks."
        lines = [f"Queue depth: {depth}", f"Running: {len(running)}"]
        for r in running[:5]:
            lines.append(f"  • {r.get('submission_id', '?')} — {r.get('shadow_id', '?')}")
        return "\n".join(lines)
    except Exception as exc:
        return f"Could not read queue status: {exc}"


def handle_scrolls(_: InboundCommand) -> str:
    try:
        scrolls = _state().vault.load_index("scrolls") or []
        if not scrolls:
            return "No scrolls."
        lines = ["Recent scrolls:"]
        for s in scrolls[-10:]:
            lines.append(
                f"  • {s.get('id', '?')} — {s.get('status', '?')} — "
                f"{(s.get('name') or '')[:60]}"
            )
        return "\n".join(lines)
    except Exception as exc:
        return f"Could not load scrolls: {exc}"


def handle_activities(_: InboundCommand) -> str:
    try:
        items = _state().vault.load_index("activities") or []
        if not items:
            return "No activities."
        lines = ["Recent activities:"]
        for a in items[-10:]:
            lines.append(
                f"  • {a.get('id', '?')} — {a.get('status', '?')}"
            )
        return "\n".join(lines)
    except Exception as exc:
        return f"Could not load activities: {exc}"


def handle_shadows(_: InboundCommand) -> str:
    try:
        items = _state().vault.load_index("shadow_army") or []
        if not items:
            return "No shadows."
        lines = ["Shadows:"]
        for sh in items[:20]:
            lines.append(
                f"  • {sh.get('id', '?')} — {sh.get('name', '?')} — "
                f"{sh.get('status', '?')}"
            )
        return "\n".join(lines)
    except Exception as exc:
        return f"Could not load shadows: {exc}"


def handle_approve(cmd: InboundCommand) -> str:
    """Approve a pending scroll.  Equivalent of clicking ✓ APPROVE in the UI."""
    scroll_id = cmd.args.strip()
    if not scroll_id:
        return "Usage: /approve <scroll_id>"
    try:
        # Reuse the same approval gate the dashboard uses.
        from systemu.pipelines.refinery import approve_scroll
        approve_scroll(scroll_id, vault=_state().vault, config=_state().config)
        return f"✓ Approved {scroll_id}.  Pipeline advancing."
    except ImportError:
        return f"Approval pipeline not available in this build for {scroll_id}."
    except Exception as exc:
        logger.exception("[Handler] /approve failed")
        return f"Could not approve {scroll_id}: {exc}"


def handle_reject(cmd: InboundCommand) -> str:
    scroll_id = cmd.args.strip()
    if not scroll_id:
        return "Usage: /reject <scroll_id>"
    try:
        from systemu.pipelines.refinery import reject_scroll
        reject_scroll(scroll_id, vault=_state().vault, config=_state().config)
        return f"✓ Rejected {scroll_id}."
    except ImportError:
        return f"Reject pipeline not available in this build for {scroll_id}."
    except Exception as exc:
        return f"Could not reject {scroll_id}: {exc}"


def _normalize_choice(raw: str) -> str | None:
    """Map a user-typed choice to a canonical ``a1..a4`` key, or None if invalid.

    Accepts an explicit ``a1..a4`` (case-insensitive) OR a bare ``1..4``. Anything
    else (out-of-range number, a word, an ``a5``) is rejected so the caller can
    show a usage hint rather than guess.
    """
    token = (raw or "").strip().lower()
    if token in _ANSWER_CHOICE_KEYS:
        return token
    if token.isdigit():
        n = int(token)
        if 1 <= n <= 4:
            return f"a{n}"
    return None


def handle_answer(cmd: InboundCommand) -> str:
    """Resolve a parked decision from chat: ``/answer <tag> <a1..a4>``.

    The typed sibling of tapping an inline button — both feed the SAME
    server-side resolver (``resolve_from_channel``), gated by the persisted
    SEC-1 ``resolution_class`` bit and the sender allowlist. We only parse the
    tag + choice here; every safety check lives in the resolver.
    """
    parts = (cmd.args or "").split()
    if len(parts) < 2:
        return _ANSWER_USAGE
    tag, raw_choice = parts[0], parts[1]
    choice = _normalize_choice(raw_choice)
    if choice is None:
        return _ANSWER_USAGE
    try:
        _outcome, message = resolve_from_channel(
            tag, choice, sender_id=cmd.user_id or "", channel="telegram",
        )
        return message
    except Exception as exc:
        logger.exception("[Handler] /answer failed")
        return f"Sorry — /answer failed: {exc}"


# ─────────────────────────────────────────────────────────────────────────────
#  Default handler bundle
# ─────────────────────────────────────────────────────────────────────────────

def default_handlers() -> Dict[str, Callable[[InboundCommand], str]]:
    """Return the canonical command-name → handler mapping."""
    return {
        "help":       handle_help,
        "chat":       handle_chat,
        "status":     handle_status,
        "scrolls":    handle_scrolls,
        "activities": handle_activities,
        "shadows":    handle_shadows,
        "approve":    handle_approve,
        "reject":     handle_reject,
        "answer":     handle_answer,
    }
