"""Operator-attention accounting (W5.1) — ONE definition of "needs you".

The header badge, the right-rail "Needs you" section, and the /inbox Triage
all used to count ``InboxQueue.list_descriptors()`` — which keeps only
decisions posted with ``context.kind == "gate"``. Every other pending
operator decision (``structured_question`` stuck-run asks, ``credential``
requests, …) was invisible to the whole shell: badge said 0, rail said
"Nothing waiting on you", while two runs sat parked.

This module owns the complete accounting:

  * :func:`pending_ask_rows` — pending decisions that are NOT inbox gates.
  * :func:`needs_you_total` — gates + asks (what the badge shows).
  * :func:`open_answer_dialog` — the one inline answer affordance, reusing
    the proven ``render_decision_card`` resolve→dispatch path (structured
    questions get their option pickers + free text; resolution publishes
    ``operator_decision_resolved``, which the daemon's resume_on_decision
    subscriber/reconciler uses to re-submit a parked run).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def pending_ask_rows(vault) -> List[Dict[str, Any]]:
    """Pending operator decisions that the gate-only surfaces drop.

    Returns row dicts (newest intent first is NOT guaranteed — vault order):
    ``{"id", "title", "kind", "options", "decision"}`` where ``decision`` is
    the full ``OperatorDecision.to_dict()`` shape ``render_decision_card``
    consumes. Defensive: any failure yields ``[]`` (the shell must render).
    """
    try:
        from systemu.approval.decision_queue import OperatorDecisionQueue
        pending = OperatorDecisionQueue(vault).list_pending()
    except Exception:
        logger.debug("[Attention] could not list pending decisions", exc_info=True)
        return []
    rows: List[Dict[str, Any]] = []
    for d in pending:
        ctx = getattr(d, "context", None) or {}
        if ctx.get("kind") == "gate":
            continue  # gates are owned by InboxQueue.list_descriptors()
        try:
            decision_dict = d.to_dict()
        except Exception:
            decision_dict = {
                "id": d.id, "title": getattr(d, "title", ""),
                "body": getattr(d, "body", ""),
                "options": list(getattr(d, "options", []) or []),
                "context": ctx, "dedup_key": getattr(d, "dedup_key", ""),
            }
        rows.append({
            "id": d.id,
            "title": getattr(d, "title", ""),
            "kind": ctx.get("kind") or "question",
            "options": list(getattr(d, "options", []) or []),
            "decision": decision_dict,
        })
    return rows


def table_suggestion_count(vault) -> int:
    """R-B4 — how many `suggested` table items are waiting in the tray (§5.10.c).

    Reads the PROJECTED snapshot (`items.json`) rather than re-projecting: the
    reconciler is that file's sole writer (DEC-10) and this is a badge poll running
    every 2s on every page, so re-deriving here would both duplicate the projection
    and race it. The cost is that a suggestion is visible in the count within one
    reconcile tick (≤60s) rather than instantly — the same freshness the /table
    board itself has.

    Defensive: any failure ⇒ 0. A badge must never break the shell, and 0 is the
    non-alarming direction for a count whose only job is to point at a page the
    operator can also reach directly.
    """
    try:
        from systemu.runtime.table_store import load_items
        return sum(1 for it in load_items(vault)
                   if (getattr(it, "status", "") or "") == "suggested")
    except Exception:
        logger.debug("[Attention] could not count table suggestions", exc_info=True)
        return 0


def needs_you_breakdown(vault) -> Dict[str, Any]:
    """The complete pending-attention accounting, BY SURFACE.

    A single total was enough while everything waiting lived on /inbox. R-B4 adds a
    second place work can wait — the /table tray — and a badge that counts it but
    always links to /inbox would send the operator to an empty page and tell them
    nothing needs them. So the breakdown carries the target: /inbox while anything
    is queued there, /table when the tray is the ONLY thing waiting.
    """
    gates = 0
    try:
        from systemu.interface.command.inbox import InboxQueue
        gates = len(InboxQueue(vault).list_descriptors())
    except Exception:
        gates = 0
    asks = len(pending_ask_rows(vault))
    suggestions = table_suggestion_count(vault)
    return {
        "gates": gates,
        "asks": asks,
        "table_suggestions": suggestions,
        "total": gates + asks + suggestions,
        "target": "/inbox" if (gates + asks) else ("/table" if suggestions else "/inbox"),
    }


def needs_you_total(vault) -> int:
    """Gates + non-gate asks + tray suggestions — the complete pending count."""
    return needs_you_breakdown(vault)["total"]


def make_answer_host():
    """Create the answer dialog's STABLE-SLOT host.

    Must be called OUTSIDE any timer-refreshed ``@ui.refreshable`` pane: a
    handler that creates ``ui.dialog()`` from inside one lands in a slot the
    next timer tick may already have disposed — NiceGUI then raises 'parent
    slot of the element has been deleted' (which the W3.1 log filter drops,
    so the dialog just silently never opens). Pre-creating the dialog in the
    section's own slot and reusing it sidesteps the disposal race.

    Returns ``(dialog, body)`` for :func:`open_answer_dialog`'s ``host=``.
    """
    from nicegui import ui
    from systemu.interface.design import card

    with ui.dialog() as dlg:
        body = card(classes="s-dialog q-pa-lg")
    return dlg, body


def open_answer_dialog(decision_id: str, vault, *, on_resolved=None,
                       host=None) -> None:
    """Open the inline answer dialog for one pending decision.

    Loads the decision fresh (it may have been resolved elsewhere), then
    renders the proven ``render_decision_card`` inside a dialog. Resolution
    goes through ``OperatorDecisionQueue.resolve`` → the
    ``operator_decision_resolved`` event → resume_on_decision, so answering
    here unsticks a parked run exactly like answering from /chat.

    ``host``: the ``(dialog, body)`` pair from :func:`make_answer_host`.
    REQUIRED when the caller's button lives in a timer-refreshed refreshable
    (right-rail panes) — see make_answer_host's slot-disposal note. Callers
    in stable slots may omit it (a fresh dialog is created).
    """
    from nicegui import ui

    try:
        decision = vault.get_decision(decision_id)
    except Exception:
        ui.notify("Decision not found — it may have been resolved.", type="warning")
        return
    if getattr(decision, "status", "") != "pending":
        ui.notify("Already resolved elsewhere.", type="info")
        if on_resolved:
            on_resolved()
        return

    from systemu.approval.decision_queue import OperatorDecisionQueue
    from systemu.interface.pages.insights import render_decision_card
    from systemu.interface.design import card

    queue = OperatorDecisionQueue(vault)

    if host is not None:
        dlg, body = host
        body.clear()
    else:
        with ui.dialog() as dlg:
            body = card(classes="s-dialog q-pa-lg")

    def _done() -> None:
        dlg.close()
        if on_resolved:
            on_resolved()

    with body:
        try:
            render_decision_card(decision.to_dict(), queue, _done)
        except Exception as exc:
            logger.exception("[Attention] answer card failed for %s", decision_id)
            ui.label(f"Could not render this decision: {exc}").classes("s-text-danger")
        ui.button("Close", on_click=dlg.close).classes("s-btn s-btn--ghost q-mt-md")
    dlg.open()
