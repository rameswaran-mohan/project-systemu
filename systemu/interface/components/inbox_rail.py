"""Right-rail "Needs you" inbox section (Phase 3 Batch 3 / Task 16).

Mirrors ``right_rail.live_runs_pane``: the pure row-model
(``_inbox_rail_rows``) is UI-free so it is trivially unit-testable; the
NiceGUI wrapper (``build_inbox_rail_section``) is a thin shell that follows
the SAME liveness contract (``safe_timer`` refresh, unsubscribe on
disconnect).

It is wired into the persistent IA shell (``dashboard.render_right_rail`` →
``right_rail.build_inbox_rail_section``). It renders
``InboxQueue(vault).list_descriptors()`` as glance rows (risk badge + title +
a quick Approve button) so the operator can clear low-friction gates without
leaving the current page.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple


# v0.9.32 (D.4 review FIX-3) + v0.9.34 (P0 review HIGH-2): gate types whose
# affirmative option is DANGEROUS and must NEVER be a one-click rail quick-approve.
# A command gate's last option is "Always allow"; an MCP gate's is also "Always
# allow" (options: Deny / Approve once / Trust this tool for the session / Always
# allow). Letting one rail click pick options[-1] (with no Deny offered) would
# permanently allow a destructive command/MCP call, AND the rail's resolve path
# (resolve_gate) NOOPs for both gate types so it would not even persist. These are
# render-only in the rail: the card shows ("Resolve in Inbox →"), but the operator
# resolves the full choice via the /insights Inbox, which routes through
# decision_dispatcher.dispatch -> command_gate_handler / mcp_call_gate_handler.
_RAIL_RENDER_ONLY_DEDUP_PREFIXES = ("command:", "mcp:")


def _is_render_only_gate(dedup: str) -> bool:
    return any(dedup.startswith(p) for p in _RAIL_RENDER_ONLY_DEDUP_PREFIXES)


def _inbox_rail_rows(descriptors: List[Tuple[str, Any]]) -> List[Dict[str, Any]]:
    """Pure glance-row model: map ``(id, GateDescriptor)`` -> row dict.

    Each row carries the risk (drives the status_pill) and the affirmative
    option label (the LAST option, e.g. "Approve"/"Forge"/"Approve & Install")
    so the quick-Approve button knows exactly what Approve does. Kept pure so
    the mapping is unit-testable independently of NiceGUI.

    v0.9.32 (D.4 review FIX-3): render-only gates (command gates) carry an EMPTY
    ``approve_label`` so the rail renders the card WITHOUT a dangerous one-click
    quick-approve. ``render_only`` makes the intent explicit for callers/tests.
    """
    rows: List[Dict[str, Any]] = []
    for dec_id, d in descriptors:
        options = list(getattr(d, "options", []) or [])
        dedup = getattr(d, "dedup", "") or ""
        render_only = _is_render_only_gate(dedup)
        rows.append({
            "id": dec_id,
            "title": getattr(d, "title", ""),
            "risk": getattr(d, "risk", "low"),
            "render_only": render_only,
            # The affirmative option is the LAST option (the Inbox convention:
            # safe-default at index 0, affirmative last). Empty when no options
            # OR when the gate is render-only (no one-click quick-approve).
            "approve_label": "" if render_only else (options[-1] if options else ""),
        })
    return rows


def _approve_descriptor(dec_id: str, descriptor, *, vault) -> None:
    """Quick-Approve a single gate from the rail: resolve with the affirmative
    option, then run the authorized action (Approve EXECUTES, spec §4.3).

    Order mirrors the proven CLI path (cli_commands.decisions_resolve):
    ``queue.resolve(id, choice=...)`` returns the decision with ``.choice``
    set, so ``resolve_gate`` sees the operator's choice and executes.

    v0.9.32 (D.4 review FIX-3): render-only gates (command gates) are REFUSED
    here — they must be resolved via the full /insights three-way UI, never as a
    one-click rail Always-allow (which has no Deny and which resolve_gate NOOPs,
    so it would not even persist). Defense-in-depth: the rail no longer renders a
    quick-approve button for these, but this guard makes the contract explicit.
    """
    if _is_render_only_gate(getattr(descriptor, "dedup", "") or ""):
        raise ValueError(
            "Command and MCP gates are resolved in the /insights Inbox "
            "(Deny / Approve once / [Trust for session] / Always allow), not "
            "via a one-click rail approve.")
    from systemu.interface.command.inbox import InboxQueue, resolve_gate
    queue = InboxQueue(vault)._queue
    options = list(getattr(descriptor, "options", []) or [])
    if not options:
        return
    affirmative = options[-1]
    resolved = queue.resolve(dec_id, choice=affirmative)
    return resolve_gate(resolved, vault=vault)


def build_inbox_rail_section(vault, stream_ref: str = "") -> None:
    """Render the "Needs you" rail section: pending gate descriptors as glance
    rows with a quick-Approve button.

    Follows the ``live_runs_pane`` liveness contract:
      * a UI-thread ``safe_timer`` is the SOLE driver of ``_pane.refresh()``;
      * the section unsubscribes from the EventBus + cancels on disconnect.

    ``stream_ref`` is accepted for signature parity with the other rail panes
    (Phase 4 wires the rail to follow one run); it is unused here because the
    inbox follows the vault decision queue, not a single streamed run.
    """
    from nicegui import ui

    from systemu.interface.command.inbox import InboxQueue
    from systemu.interface.design.primitives import status_pill, button
    from systemu.interface.ui_helpers import safe_timer

    def _rows() -> List[Dict[str, Any]]:
        try:
            descriptors = InboxQueue(vault).list_descriptors()
        except Exception:
            return []
        return _inbox_rail_rows(descriptors)

    # Keep the raw descriptors keyed by id so the Approve handler can pass the
    # real GateDescriptor (with its options) to the executor.
    def _descriptor_map() -> Dict[str, Any]:
        try:
            return {dec_id: d for dec_id, d in InboxQueue(vault).list_descriptors()}
        except Exception:
            return {}

    ui.label("Needs you").classes("s-section-head").style("margin-bottom: 4px;")

    # W5.1: the answer dialog's host lives HERE (stable slot) — creating it
    # inside the timer-refreshed _pane would race slot disposal (the dialog
    # silently never opens; see attention.make_answer_host).
    from systemu.interface.components.attention import make_answer_host
    _answer_host = make_answer_host()

    @ui.refreshable
    def _pane() -> None:
        from systemu.interface.components.attention import (
            pending_ask_rows, open_answer_dialog)

        rows = _rows()
        asks = pending_ask_rows(vault)
        if not rows and not asks:
            ui.label("Nothing waiting on you.").classes("s-muted").style(
                "font-size: 12px;"
            )
            return

        # W5.1: non-gate asks (stuck-run questions, credential requests) used
        # to be invisible here — a parked run looked like "nothing waiting".
        # W7.3 layout: stacked card per item (pill on top, title wrapping to
        # two lines, action right-aligned below) — the one-line pill+truncated-
        # title+button cram read as clutter in the ~280px rail.
        for ask in asks:
            with ui.element("div").classes("s-row-box s-rail-item"):
                with ui.row().classes("w-full items-center justify-between"):
                    status_pill("question")
                ui.label(ask["title"]).classes("s-rail-title")

                def _on_answer(_=None, did=ask["id"]):
                    open_answer_dialog(did, vault, on_resolved=_pane.refresh,
                                       host=_answer_host)

                with ui.element("div").classes("s-rail-actions"):
                    button("Answer", variant="primary", on_click=_on_answer)

        dmap = _descriptor_map()
        for row in rows:
            with ui.element("div").classes("s-row-box s-rail-item"):
                with ui.row().classes("w-full items-center justify-between"):
                    status_pill(row["risk"])
                ui.label(row["title"]).classes("s-rail-title")
                approve_label = row["approve_label"]
                if row.get("render_only"):
                    # v0.9.32 (D.4 review FIX-3): command gates need the full
                    # three-way choice (Deny / Approve once / Always allow) and
                    # must NOT be one-click Always-allowed from the rail. Show a
                    # hint that points to the /insights Inbox (which routes
                    # through the dispatcher → command_gate_handler).
                    ui.label("Resolve in Inbox →").classes("s-muted").style(
                        "font-size: 11px;")
                elif approve_label:
                    # W7.1: async + to_thread — Approve EXECUTES the gate
                    # action (pip installs, dry-runs, LLM calls); on the UI
                    # loop it froze the dashboard and dropped the websocket.
                    async def _on_approve(_=None, rid=row["id"]):
                        import asyncio
                        descriptor = _descriptor_map().get(rid)
                        if descriptor is None:
                            ui.notify("Gate already resolved.", type="warning")
                            _pane.refresh()
                            return
                        # Capture the client BEFORE the await — the 2s pane
                        # timer may dispose this slot while the work runs, so
                        # post-await UI ops must re-enter the captured client
                        # (else 'parent slot deleted'). Mirrors tools._heal_async.
                        try:
                            client = ui.context.client
                        except Exception:
                            client = None
                        ui.notify(f"Working on it: {descriptor.title}", type="info")
                        try:
                            await asyncio.to_thread(
                                _approve_descriptor, rid, descriptor, vault=vault)
                            msg, typ = f"Approved: {descriptor.title}", "positive"
                        except Exception as exc:
                            msg, typ = f"Approve failed: {exc}", "negative"
                        if client is not None:
                            try:
                                with client:
                                    ui.notify(msg, type=typ)
                                    _pane.refresh()
                            except Exception:
                                pass

                    with ui.element("div").classes("s-rail-actions"):
                        button(approve_label, variant="primary",
                               on_click=_on_approve)

    _pane()

    # UI-thread timer is the SOLE driver of refresh (slot-error tolerant),
    # mirroring live_runs_pane — the queue is file-backed so a poll is enough.
    # W12 (ship-blocker class): change-gated — the unconditional 2s repaint
    # destroyed and rebuilt the Answer/Approve buttons, silently eating any
    # click that raced the tick.
    import json as _json

    from systemu.interface.ui_helpers import gated_refresh

    def _fingerprint():
        from systemu.interface.components.attention import pending_ask_rows
        return _json.dumps([_rows(), pending_ask_rows(vault)], default=str)

    safe_timer(2.0, gated_refresh(_fingerprint, _pane.refresh))
