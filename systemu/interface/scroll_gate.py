"""Inspect-before-approve for scroll gates (Phase 5 Slice 2b).

The Work spine retires the blind ``✓ Approve``: a PENDING_APPROVAL scroll is
reviewed through the SAME unified gate card the Inbox renders (risk pill,
INSPECT, WHAT-APPROVE-DOES, highlighted safe-default) and resolved through
the SAME executor chain — ``queue.resolve(dec_id, choice)`` →
``resolve_gate`` → ``approve_pending_scroll`` — that Inbox / CLI / scheduler
already use.

The gate row does NOT reliably exist: the only producer
(``scroll_refiner._queue_ready_for_reapproval_notification``) runs on
RE-approvals after a tool unblock, so first-time PENDING_APPROVAL scrolls
have no row.  ``ensure_scroll_gate`` therefore enqueues ON DEMAND,
idempotently, and CRITICALLY with ``policy=None`` — passing
``load_default_policy()`` would AUTO-EXECUTE under Bypass mode via
``_synthetic_approved`` (inbox.py), skipping the inspection the operator
just asked for.

Shared by pages/work.py and pages/scrolls.py.  Import-light: NiceGUI is
imported only inside the dialog opener, so ``ensure_scroll_gate`` is
testable headless.
"""
from __future__ import annotations

from typing import Callable, Optional


def ensure_scroll_gate(vault, scroll) -> str:
    """Return the pending decision id for ``scroll``'s approval gate,
    enqueueing it on demand when missing.

    Idempotent twice over: we pre-check ``list_descriptors`` for the scroll's
    dedup key (``scroll:<id>``), and ``OperatorDecisionQueue.post`` itself
    dedups pending rows on dedup_key — double-clicks can never produce two
    gates.

    NEVER auto-executes: ``policy=None`` skips the gate-mode dial entirely,
    so even under ``SYSTEMU_GATE_MODE=bypass`` this POSTS a pending row for
    the operator to inspect (they explicitly asked to review, so Bypass's
    auto-grant must not apply here).
    """
    from systemu.interface.command.gate import GateDescriptor
    from systemu.interface.command.inbox import InboxQueue

    queue = InboxQueue(vault)
    scroll_id = getattr(scroll, "id", "") or ""
    dedup = f"scroll:{scroll_id}"
    for dec_id, descriptor in queue.list_descriptors():
        if getattr(descriptor, "dedup", "") == dedup:
            return dec_id

    # Non-empty inspect for first-time gates: the scroll's intent (the WHY),
    # falling back to the first 200 chars of the narrative.
    summary = (getattr(scroll, "intent", "") or
               (getattr(scroll, "narrative_md", "") or "")[:200])
    descriptor = GateDescriptor.from_scroll(scroll, summary=summary)
    return queue.enqueue(descriptor, gate_type="scroll", policy=None)


def open_scroll_review_dialog(
    scroll_id: str, *, on_resolved: Optional[Callable[[], None]] = None,
) -> None:
    """Open the inspect-before-approve dialog for a PENDING_APPROVAL scroll.

    Ensures the gate row exists (``ensure_scroll_gate``), then renders the
    EXISTING unified Inbox card (``inbox_page._render_unified_card``) inside
    a dialog — its buttons run the proven resolve chain.  ``on_resolved``
    runs after the operator picks an option (callers refresh their rows);
    the dialog closes on resolution.
    """
    from nicegui import ui

    from systemu.interface.command.inbox import InboxQueue
    from systemu.interface.dashboard_state import AppState
    from systemu.interface.design import card
    from systemu.interface.pages.inbox_page import _render_unified_card

    vault = AppState.get().vault
    try:
        scroll = vault.get_scroll(scroll_id)
    except KeyError:
        ui.notify("Scroll not found.", type="negative")
        return
    try:
        dec_id = ensure_scroll_gate(vault, scroll)
    except Exception as exc:
        ui.notify(f"Could not open the approval gate: {exc}", type="negative")
        return

    descriptor = next(
        (d for i, d in InboxQueue(vault).list_descriptors() if i == dec_id),
        None)
    if descriptor is None:
        # Race: resolved between ensure and render — nothing left to review.
        ui.notify("Gate already resolved — refreshing.", type="warning")
        if on_resolved is not None:
            on_resolved()
        return

    with ui.dialog() as dlg, card(classes="s-dialog q-pa-lg"):
        def _resolved() -> None:
            dlg.close()
            if on_resolved is not None:
                on_resolved()

        _render_unified_card(dec_id, descriptor, vault=vault,
                             on_resolved=_resolved)
        ui.button("Close", on_click=dlg.close).classes(
            "s-btn s-btn--ghost q-mt-md")
    dlg.open()
