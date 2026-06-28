"""Full Inbox page (Phase 3 Batch 3 / Task 17) — the one decisions surface.

Renders every pending gate as the UNIFIED card (spec §4.3):
  * risk badge (status_pill)
  * inline Inspect (descriptor.inspect)
  * an explicit "what Approve does" line (descriptor.what_approve_does)
  * the safe-default option highlighted
  * destructive treatment (high-risk / Deny) made visually distinct
  * the dedup id shown (operator can correlate the gate to its source)

Pending (InboxQueue.list_descriptors) = the Triage section; resolved gate rows
(vault.load_index("decisions") with status=="resolved" and context.kind=="gate")
= the History section. Approve/Deny EXECUTE via the proven order
queue.resolve(id, choice) -> resolve_gate(resolved, vault=vault).

The card-builder logic is factored into the pure, NiceGUI-free
``_inbox_card_model`` (+ the ``_resolved_gate_rows`` splitter) so it is
import-light testable, mirroring recovery_panel / test_recovery_panel.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from nicegui import ui

logger = logging.getLogger(__name__)

# Risk bands that get the distinct destructive treatment on the card.
_DESTRUCTIVE_RISKS = frozenset({"high"})


def _inbox_card_model(descriptor) -> Dict[str, Any]:
    """Pure model for one unified Inbox card (spec §4.3).

    Decides the destructive treatment + surfaces the explicit
    what-Approve-does text and the highlighted safe-default. Kept UI-free so it
    is unit-testable without a NiceGUI runtime.
    """
    options = list(getattr(descriptor, "options", []) or [])
    safe_default = getattr(descriptor, "safe_default", "") or (
        options[0] if options else "")
    # The affirmative (Approve-equivalent) option is the LAST option.
    affirmative = options[-1] if options else ""
    risk = getattr(descriptor, "risk", "low")
    return {
        "title": getattr(descriptor, "title", ""),
        "risk": risk,
        "inspect": getattr(descriptor, "inspect", ""),
        "what_approve_does": getattr(descriptor, "what_approve_does", ""),
        "options": options,
        "safe_default": safe_default,
        "affirmative": affirmative,
        "dedup": getattr(descriptor, "dedup", ""),
        # High-risk gates get a visually-distinct (danger-bordered) card and
        # the affirmative option styled as a danger button.
        "destructive": risk in _DESTRUCTIVE_RISKS,
    }


def _resolved_gate_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Pure filter: keep only resolved decision rows that the Inbox owns
    (status=="resolved" AND context.kind=="gate"). UI-free / testable."""
    out: List[Dict[str, Any]] = []
    for r in rows:
        if r.get("status") != "resolved":
            continue
        if (r.get("context") or {}).get("kind") != "gate":
            continue
        out.append(r)
    return out


# W5.1: the kinds the Inbox History section shows. Gates plus the non-gate
# asks (stuck-run structured questions, credential requests) that Triage now
# surfaces — a resolved ask must not vanish without a trace.
_INBOX_HISTORY_KINDS = frozenset({"gate", "structured_question", "credential"})

# History hydration cap — the decisions index can grow unbounded; only the
# newest N resolved rows are worth a per-decision read.
_HISTORY_LIMIT = 50


def _resolved_inbox_rows(rows: List[Dict[str, Any]], get_decision) -> List[Dict[str, Any]]:
    """Resolved decision rows the Inbox owns — gates AND asks.

    W5.1 root-cause note: the decisions *index* stores slim headers WITHOUT
    ``context`` (vault.save_decision), so the old filter
    ``context.kind == "gate"`` over ``load_index("decisions")`` matched
    nothing — the History section has been silently empty since it shipped.
    This hydrates the newest resolved rows via ``get_decision`` (per-decision
    JSON carries context/choice/resolved_at) and filters by kind there.
    """
    resolved = [r for r in rows if r.get("status") == "resolved"]
    resolved.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    out: List[Dict[str, Any]] = []
    for r in resolved[:_HISTORY_LIMIT]:
        ctx = r.get("context") or {}
        if not ctx:
            try:
                d = get_decision(r["id"])
                ctx = getattr(d, "context", None) or {}
                resolved_at = getattr(d, "resolved_at", None)
                r = {**r, "context": ctx,
                     "choice": getattr(d, "choice", None),
                     "resolved_at": (resolved_at.isoformat()
                                     if hasattr(resolved_at, "isoformat")
                                     else resolved_at)}
            except Exception:
                continue
        if ctx.get("kind") not in _INBOX_HISTORY_KINDS:
            continue
        out.append(r)
    return out


# ─── rendering ────────────────────────────────────────────────────────────────

def _resolve_and_execute_gate(dec_id: str, choice: str, vault):
    """The blocking half of a gate resolution — runs OFF the UI event loop.

    resolve_gate EXECUTES the approved action (scroll → LLM activity
    extraction, dep → pip install + dry-run, tools_blocked → Gate-3 enables,
    forge → LLM code generation). Running it inside the NiceGUI click handler
    starved the websocket for seconds — the operator saw a connection-lost
    overlay and a frozen screen on every approve. Pure: no UI calls here.
    """
    from systemu.interface.command.inbox import InboxQueue, resolve_gate
    queue = InboxQueue(vault)._queue
    resolved = queue.resolve(dec_id, choice=choice)
    return resolve_gate(resolved, vault=vault)


def _render_unified_card(dec_id: str, descriptor, *, vault, on_resolved) -> None:
    """Render one pending gate as the unified triage card with Approve/Deny."""
    from systemu.interface.design.primitives import status_pill, button

    model = _inbox_card_model(descriptor)
    card_classes = "s-card s-card--danger" if model["destructive"] else "s-card"

    with ui.element("div").classes(card_classes).style(
        "margin-bottom: 12px; display: flex; flex-direction: column; gap: 10px;"
    ):
        # Header: risk badge + title.
        with ui.row().style("align-items: center; gap: 10px;"):
            status_pill(model["risk"])
            ui.label(model["title"]).classes("s-cell s-cell--bold").style(
                "font-size: 15px;"
            )

        # Inline Inspect.
        if model["inspect"]:
            with ui.column().style("gap: 2px;"):
                ui.label("INSPECT").classes("s-field-label")
                ui.label(model["inspect"]).classes("s-cell").style(
                    "white-space: pre-wrap; font-size: 13px;"
                )

        # Explicit "what Approve does".
        if model["what_approve_does"]:
            with ui.column().style("gap: 2px;"):
                ui.label("WHAT APPROVE DOES").classes("s-field-label")
                ui.label(model["what_approve_does"]).classes("s-cell").style(
                    "white-space: pre-wrap; font-size: 13px;"
                )

        # Highlighted safe-default.
        if model["safe_default"]:
            ui.html(
                f'<span class="s-safe-default">Safe default: '
                f'{model["safe_default"]}</span>'
            )

        # dedup id (operator correlates the gate to its source).
        if model["dedup"]:
            ui.label(f"dedup: {model['dedup']}").classes("s-mono")

        def _resolve_with(choice: str):
            # W7.1: async handler + to_thread — the resolve chain executes the
            # approved action (LLM/pip/dry-run) and must never run on the UI
            # event loop (it froze the dashboard + dropped the websocket).
            async def _click(_=None):
                import asyncio
                # Capture the client before the await — the Triage section
                # refreshes on a 5s timer + on resolve, disposing this slot
                # while the executor runs; post-await UI ops re-enter it.
                try:
                    client = ui.context.client
                except Exception:
                    client = None
                ui.notify(f"Working on it: {choice}…", type="info")
                try:
                    result = await asyncio.to_thread(
                        _resolve_and_execute_gate, dec_id, choice, vault)
                    msg = getattr(result, "summary", None) or f"Resolved: {choice}"
                    typ = "positive"
                except Exception as exc:
                    logger.exception("[Inbox] resolve failed for %s", dec_id)
                    msg, typ = f"Resolve failed: {exc}", "negative"
                if client is not None:
                    try:
                        with client:
                            ui.notify(msg, type=typ)
                            on_resolved()
                    except Exception:
                        pass
            return _click

        # Forge gates route to the RICH human-code-review dialog (Slice 3e), NOT
        # the generic Approve→resolve_gate chain. resolve_gate's forge branch is
        # the DEGRADED one-shot that re-runs forge_tool_from_spec over the
        # UNEDITED spec with no human code review — it owns AUTO-PROPOSED tools
        # only. An operator who finds a forge gate in the Inbox gets the SAME
        # two-gate spec→code review as the registry "Review & Forge" button by
        # navigating to the canonical /tools?forge=<id> deep-link (build_tools_page
        # auto-opens _show_spec_review_dialog for it). The card therefore renders
        # ONE "Review & Forge" button and NEVER calls resolve_gate for forge, so
        # it cannot double-forge: the rich dialog's save_approved_code +
        # _resolve_forge_gate_silently is the single forge executor for this path.
        dedup = model["dedup"]
        if dedup.startswith("forge:"):
            forge_tool_id = dedup.partition(":")[2]

            def _open_review(_=None, tid: str = forge_tool_id) -> None:
                ui.navigate.to(f"/tools?forge={tid}")

            with ui.row().style("gap: 8px;"):
                button("Review & Forge", variant="primary", on_click=_open_review)
            return

        # Action buttons: the affirmative (destructive→danger) + the safe-default
        # / Deny rendered as a ghost so the destructive choice is visually
        # distinct from the safe one.
        with ui.row().style("gap: 8px;"):
            for opt in model["options"]:
                if opt == model["affirmative"]:
                    variant = "danger" if model["destructive"] else "primary"
                else:
                    variant = "ghost"
                button(opt, variant=variant, on_click=_resolve_with(opt))


def _render_history_card(row: Dict[str, Any]) -> None:
    """Render one resolved gate row (read-only) in the History section."""
    from systemu.interface.command.gate import GateDescriptor
    from systemu.interface.design.primitives import status_pill

    ctx = row.get("context") or {}
    descriptor = GateDescriptor.from_decision_context(
        ctx, title=row.get("title", ""), options=row.get("options", []),
        dedup=row.get("dedup_key", ""))
    with ui.element("div").classes("s-card").style(
        "margin-bottom: 8px; display: flex; align-items: center; gap: 10px;"
    ):
        status_pill(descriptor.risk)
        ui.label(descriptor.title).classes("s-cell").style("flex: 1;")
        # W10.2: policy auto-grants are visibly marked — the audit trail
        # must distinguish "the operator approved" from "the dial approved".
        if (row.get("context") or {}).get("resolved_by") == "auto_policy":
            ui.html('<span class="s-pill s-pill--info">auto-policy</span>')
        choice = row.get("choice") or ""
        ui.html(f'<span class="s-pill s-pill--muted">{choice}</span>')


def _load_resolved_gate_rows(vault) -> List[Dict[str, Any]]:
    try:
        rows = vault.load_index("decisions")
    except Exception:
        return []
    return _resolved_inbox_rows(rows, vault.get_decision)


def render_inbox_ask_cards(vault, *, on_resolved) -> int:
    """W5.1: render the pending NON-gate decisions (stuck-run questions,
    credential asks, …) as full answerable cards via the proven
    ``render_decision_card`` path. Returns the count rendered.

    These used to be invisible on /inbox (Triage filtered kind=='gate') even
    though the page's contract is "every decision the agent needs from you —
    one card, one place"."""
    if vault is None:
        return 0
    from systemu.interface.components.attention import pending_ask_rows
    from systemu.approval.decision_queue import OperatorDecisionQueue
    from systemu.interface.pages.insights import render_decision_card

    asks = pending_ask_rows(vault)
    if not asks:
        return 0
    queue = OperatorDecisionQueue(vault)
    for ask in asks:
        try:
            render_decision_card(ask["decision"], queue, on_resolved)
        except Exception as exc:
            logger.exception("[Inbox] ask card failed for %s", ask["id"])
            ui.label(f"Could not render decision {ask['id']}: {exc}").classes(
                "s-text-danger")
    return len(asks)


def render_inbox_gate_cards(vault, *, on_resolved, empty_label: str = "") -> int:
    """Render the pending unified gate cards (kind=="gate" rows) for ``vault``.

    Reusable across the full Inbox page AND the subsumed legacy surfaces
    (notifications pending tab, /insights actions tab) so the unified card is
    rendered in EXACTLY one place — no split-brain. Returns the count of gate
    cards rendered so a caller can decide whether to also draw its own
    "nothing here" copy. When ``empty_label`` is given and there are no gates,
    renders that label.
    """
    if vault is None:
        return 0
    from systemu.interface.command.inbox import InboxQueue
    try:
        descriptors = InboxQueue(vault).list_descriptors()
    except Exception as exc:
        ui.label(f"Failed to load pending gates: {exc}").classes(
            "s-text-danger").style("padding: 12px;")
        return 0
    if not descriptors:
        if empty_label:
            ui.label(empty_label).classes("s-muted").style("padding: 12px;")
        return 0
    from systemu.approval.decision_queue import OperatorDecisionQueue
    from systemu.interface.pages.insights import render_decision_card
    _queue = OperatorDecisionQueue(vault)
    for dec_id, descriptor in descriptors:
        # Amend-then-approve: a harness CAPABILITY gate (not INPUT) renders via the
        # full render_decision_card so the operator gets Deny / Approve / Edit (the
        # JSON spec editor) here on the primary surface. Every other gate type keeps
        # the unified triage card.
        _routed = False
        try:
            _dec = vault.get_decision(dec_id)
            _ctx = (_dec.context or {}) if _dec else {}
            if (_ctx.get("gate_type") == "harness"
                    and str(_ctx.get("harness_kind") or "").lower() not in ("", "input")):
                render_decision_card(_dec.to_dict(), _queue, on_resolved)
                _routed = True
        except Exception:
            logger.exception("[Inbox] harness gate card routing failed for %s", dec_id)
        if not _routed:
            _render_unified_card(dec_id, descriptor, vault=vault, on_resolved=on_resolved)
    return len(descriptors)


def build_inbox_page() -> None:
    """Render the full Inbox: a Triage section (pending unified cards) above a
    History section (resolved gate rows)."""
    from systemu.interface.dashboard_state import AppState

    state = AppState.get()
    vault = state.vault

    from systemu.interface.design.glossary import lore_sublabel
    ui.label(lore_sublabel("inbox")).classes("s-muted")
    ui.label(
        "Every decision the agent needs from you — one card, one place. "
        "Approve executes the authorized action."
    ).classes("s-muted").style("font-size: 13px; margin-bottom: 8px;")

    # v0.9.50 (item 4): one-click path back to the Chat page where the operator
    # added the task that produced these decisions. Outlined + primary so it's
    # clearly visible (a muted flat button was too easy to miss).
    ui.button("← Back to Chat",
              on_click=lambda: ui.navigate.to("/chat?tab=compose")).props(
        "outline dense color=primary").style("margin-bottom: 12px;")

    # W2.4: never silent when the gate policy pierces the safety floor
    # (no_floor / override→allow on dep|recovery) — uses gate_mode.floor_pierces.
    from systemu.interface.ui_helpers import render_floor_pierce_banner
    render_floor_pierce_banner()

    # ── Triage (pending) ──────────────────────────────────────────────────────
    ui.label("Triage").classes("s-section-head").style("margin-top: 8px;")

    @ui.refreshable
    def _triage() -> None:
        if vault is None:
            ui.label("Vault unavailable.").classes("s-muted").style("padding: 12px;")
            return
        # W5.1: gates + non-gate asks are BOTH Triage; the empty copy only
        # renders when neither has anything pending.
        n_gates = render_inbox_gate_cards(vault, on_resolved=_triage.refresh)
        n_asks = render_inbox_ask_cards(vault, on_resolved=_triage.refresh)
        if n_gates == 0 and n_asks == 0:
            ui.label("Nothing waiting on you. You're all caught up.").classes(
                "s-muted").style("padding: 12px;")

    _triage()

    # ── History (resolved gates) ──────────────────────────────────────────────
    ui.separator().classes("s-sep").style("margin: 20px 0 8px 0;")
    ui.label("History").classes("s-section-head")

    @ui.refreshable
    def _history() -> None:
        if vault is None:
            return
        rows = _load_resolved_gate_rows(vault)
        if not rows:
            ui.label("No resolved gates yet.").classes("s-muted").style(
                "padding: 12px;")
            return
        # Newest-first by resolved_at when present.
        rows = sorted(
            rows, key=lambda r: r.get("resolved_at") or "", reverse=True)
        for row in rows:
            _render_history_card(row)

    _history()

    # Refresh both sections periodically (file-backed queue → polling is enough).
    # W12 (ship-blocker class): change-gated — unconditional repaints destroyed
    # and rebuilt the Approve/Answer buttons, silently eating racing clicks.
    import json as _json

    from systemu.interface.ui_helpers import gated_refresh, safe_timer

    def _refresh_all():
        _triage.refresh()
        _history.refresh()

    def _fingerprint():
        from systemu.interface.command.inbox import InboxQueue
        from systemu.interface.components.attention import pending_ask_rows
        pending = [(d.gate_id, d.title) for d in
                   InboxQueue(vault).list_descriptors()] if vault else []
        asks = pending_ask_rows(vault) if vault else []
        resolved = [(r.get("id"), r.get("resolved_at"))
                    for r in _load_resolved_gate_rows(vault)] if vault else []
        return _json.dumps([pending, asks, resolved], default=str)

    safe_timer(5.0, gated_refresh(_fingerprint, _refresh_all))
