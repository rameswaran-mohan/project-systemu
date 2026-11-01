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

from .gateway import InboundCommand, mask_outbound
from .decision_bridge import (
    ANSWER_COMMAND,
    answer_example,
    parse_answer_args,
    resolve_from_channel,
)

logger = logging.getLogger(__name__)

# Button keys a typed value may also name (a bare 1..4 maps to a1..a4). Passed
# to the resolver ALONGSIDE the typed value: an option card or an enum question
# may use the key, a free-text question uses the value itself.
_ANSWER_CHOICE_KEYS = {"a1", "a2", "a3", "a4"}
_ANSWER_USAGE = (
    f"Usage: {answer_example('<tag>')}\n"
    "The <tag> is the short code on the decision message. The <value> is your "
    "answer: free text for a question, or an option's label or number "
    "(1, 2, 3 ...; a1..a4 also work) when there are options."
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
        f"{answer_example('<tag>')} — answer a parked decision (or tap its button)\n"
        "/why <tag>            — why a parked decision is asking (read-only)\n"
        "/help                 — this message\n"
        "\nPlain text without a leading / is treated as /chat."
    )


def handle_chat(cmd: InboundCommand) -> str:
    """Submit a chat task — routes through direct_task with route_through_supervisor=True.

    R-UTL1: ``submit_chat_task`` did not exist until this release, so every
    ``/chat`` raised ImportError and answered "No chat pipeline available in
    this build" WITHOUT enqueuing anything — the command has been inert since it
    was written. It now shares one submission path with the U-1a HTTP API
    (RUL-5: no second executor).
    """
    if not cmd.args:
        return "Usage: /chat <prompt>"
    try:
        from systemu.pipelines.direct_task import submit_chat_task
        submission_id = submit_chat_task(
            cmd.args,
            state=_state(),
            route_through_supervisor=True,
            source=f"telegram:{cmd.user_id}",
        )
        return (
            f"✓ Submitted (id: {submission_id}).  "
            f"Use /status to check progress."
        )
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
                from systemu.runtime import verification_badge as vb
                lines.append("Recent tasks:")
                for row in rows:
                    # DEC-13 / PAR-2: the same minted badge the dashboard row shows.
                    v = vb.view(row.get("badge"))
                    tail = f" -- {v.label}" if v.show else ""
                    lines.append(f"• [{row['status']}] {row['name']}{tail}")
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
            # DEC-31: mask BEFORE the cut. A secret straddling the 60-char cap
            # would otherwise be split into a prefix no shape rule recognises
            # (a 40+ hex run cut to 14 chars) and the push's own mask, which
            # only sees the already-cut line, would let the piece through.
            name = mask_outbound(str(s.get('name') or ''))[:60]
            lines.append(
                f"  • {s.get('id', '?')} — {s.get('status', '?')} — {name}"
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
    """Map a typed value to a canonical ``a1..a4`` button key, or None.

    Accepts an explicit ``a1..a4`` (case-insensitive) OR a bare ``1..4``. Anything
    else is NOT a key - but it may still be a valid answer (free text, a label),
    so the caller passes the raw value on regardless and the resolver decides.
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
    """Resolve a parked decision from chat: ``/answer <tag> <value>``.

    The grammar is the one the push prints (``decision_bridge.answer_example``).
    The typed sibling of tapping an inline button — both feed the SAME
    server-side resolver (``resolve_from_channel``), gated by the persisted
    SEC-1 ``resolution_class`` bit and the sender allowlist. We only split the
    tag from the value here: whether the value is a valid answer depends on the
    decision's kind, which only the resolver may look up (after its allowlist
    check), so every value reaches it.
    """
    parsed = parse_answer_args(cmd.args)
    if parsed is None:
        return _ANSWER_USAGE
    tag, value = parsed
    try:
        _outcome, message = resolve_from_channel(
            tag, _normalize_choice(value), sender_id=cmd.user_id or "",
            channel="telegram", value=value,
        )
        return message
    except Exception as exc:
        logger.exception("[Handler] /answer failed")
        return f"Sorry — /answer failed: {exc}"


_WHY_USAGE = (
    "Usage: /why <tag>\n"
    "The <tag> is the short code on the decision message. /why only reads the "
    "stored record - it never re-scores or resolves anything."
)


def _why_lookup(tag: str):
    """Find the decision for ``tag`` and return its dict, or None.

    Rides R-P1's tag machinery (``_match_open_decision``) so /why and /answer
    address decisions identically -- including the 8-char disambiguation for
    colliding tags. READ-ONLY: it lists and matches, it never resolves.
    """
    from systemu.approval.decision_queue import OperatorDecisionQueue
    from .decision_bridge import _match_open_decision

    state = _state()
    vault = getattr(state, "vault", None)
    if vault is None:
        return None
    decision = _match_open_decision(OperatorDecisionQueue(vault), tag)
    if decision is None:
        return None
    try:
        return decision.to_dict()
    except Exception:
        logger.debug("[Handler] /why could not serialize decision", exc_info=True)
        return None


def handle_why(cmd: InboundCommand) -> str:
    """Explain a parked decision: ``/why <tag>`` (UX-14 Telegram parity).

    Returns byte-identical text to the dashboard's why panel -- one explanation
    builder, two surfaces (PAR-2). Strictly read-only.
    """
    tag = (cmd.args or "").split()[0] if (cmd.args or "").strip() else ""
    if not tag:
        return _WHY_USAGE
    try:
        decision = _why_lookup(tag)
    except Exception:
        logger.exception("[Handler] /why lookup failed")
        return f"Sorry - /why could not read the decision for tag {tag}."
    if not decision:
        return (f"No decision matches tag {tag}. Tags are shown on the "
                f"decision message; use /status to see what is waiting.")
    from systemu.interface.components.why_panel import explain
    return explain(decision).as_text()


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
        ANSWER_COMMAND: handle_answer,
        "why":        handle_why,
    }
