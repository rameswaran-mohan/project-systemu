"""Right-rail "Needs you" inbox section (Phase 3 Batch 3 / Task 16).

Mirrors ``right_rail.live_runs_pane``: the pure row-model
(``_inbox_rail_rows``) is UI-free so it is trivially unit-testable; the
NiceGUI wrapper (``build_inbox_rail_section``) is a thin shell that follows
the SAME liveness contract (``safe_timer`` refresh, unsubscribe on
disconnect).

This component is deliberately NOT wired into the persistent IA shell /
``dashboard._build_layout`` — that integration lands in Phase 4. It renders
``InboxQueue(vault).list_descriptors()`` as glance rows (risk badge + title +
a quick Approve button) so the operator can clear low-friction gates without
leaving the current page.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple


def _inbox_rail_rows(descriptors: List[Tuple[str, Any]]) -> List[Dict[str, Any]]:
    """Pure glance-row model: map ``(id, GateDescriptor)`` -> row dict.

    Each row carries the risk (drives the status_pill) and the affirmative
    option label (the LAST option, e.g. "Approve"/"Forge"/"Approve & Install")
    so the quick-Approve button knows exactly what Approve does. Kept pure so
    the mapping is unit-testable independently of NiceGUI.
    """
    rows: List[Dict[str, Any]] = []
    for dec_id, d in descriptors:
        options = list(getattr(d, "options", []) or [])
        rows.append({
            "id": dec_id,
            "title": getattr(d, "title", ""),
            "risk": getattr(d, "risk", "low"),
            # The affirmative option is the LAST option (the Inbox convention:
            # safe-default at index 0, affirmative last). Empty when no options.
            "approve_label": options[-1] if options else "",
        })
    return rows


def _approve_descriptor(dec_id: str, descriptor, *, vault) -> None:
    """Quick-Approve a single gate from the rail: resolve with the affirmative
    option, then run the authorized action (Approve EXECUTES, spec §4.3).

    Order mirrors the proven CLI path (cli_commands.decisions_resolve):
    ``queue.resolve(id, choice=...)`` returns the decision with ``.choice``
    set, so ``resolve_gate`` sees the operator's choice and executes.
    """
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
    from nicegui import ui, app

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

    @ui.refreshable
    def _pane() -> None:
        rows = _rows()
        if not rows:
            ui.label("Nothing waiting on you.").classes("s-muted").style(
                "font-size: 12px;"
            )
            return
        dmap = _descriptor_map()
        for row in rows:
            with ui.element("div").classes("s-row-box").style(
                "display: flex; align-items: center; gap: 8px; margin-bottom: 6px;"
            ):
                status_pill(row["risk"])
                ui.label(row["title"]).classes("s-cell").style(
                    "flex: 1; overflow: hidden; text-overflow: ellipsis; "
                    "white-space: nowrap;"
                )
                approve_label = row["approve_label"]
                if approve_label:
                    def _on_approve(_=None, rid=row["id"]):
                        descriptor = _descriptor_map().get(rid)
                        if descriptor is None:
                            ui.notify("Gate already resolved.", type="warning")
                            _pane.refresh()
                            return
                        try:
                            _approve_descriptor(rid, descriptor, vault=vault)
                            ui.notify(f"Approved: {descriptor.title}", type="positive")
                        except Exception as exc:
                            ui.notify(f"Approve failed: {exc}", type="negative")
                        finally:
                            _pane.refresh()

                    button(approve_label, variant="primary", on_click=_on_approve)

    _pane()

    # UI-thread timer is the SOLE driver of refresh (slot-error tolerant),
    # mirroring live_runs_pane — the queue is file-backed so a poll is enough.
    safe_timer(2.0, _pane.refresh)

    # Detach on disconnect to avoid leaks (no EventBus subscription here, but
    # keep the contract symmetric with the other rail panes).
    try:
        app.on_disconnect(lambda: None)
    except Exception:
        pass
