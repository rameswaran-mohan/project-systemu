"""Insights — tabbed parent page hosting Memory, Flywheel, Events, and Pending
Actions panels.

v0.7.2 sidebar consolidation: three small read-only analytics surfaces that
used to live as separate top-level routes (/memory, /flywheel,
/notifications) are now tabs inside one /insights destination.  The
underlying page builders are unchanged — this module only composes them.

v0.8.0 adds a fourth "actions" tab that renders pending OperatorDecision
cards from the decision queue (Pattern 1).

Direct-link to a specific tab via ``/insights?tab=memory|flywheel|events|actions``.
The legacy URLs (/memory, /flywheel, /notifications) are preserved as
redirect handlers in ``dashboard.py`` so bookmarks + notification deep
links continue to work.
"""

from __future__ import annotations

import logging

from nicegui import ui

from systemu.interface.dashboard_state import THEME
from systemu.interface.pages.flywheel_page import build_flywheel_page
from systemu.interface.pages.memory_consolidation_page import (
    build_memory_consolidation_page,
)
from systemu.interface.pages.notifications_page import build_notifications_page

logger = logging.getLogger(__name__)

_VALID_TABS = ("memory", "flywheel", "events", "actions")


def build_insights_page(default_tab: str = "memory") -> None:
    """Render the Insights page with four tabs.

    Args:
        default_tab: Which tab is active on load.  Falls back to ``memory``
                     when the query string supplies anything unrecognised.
    """
    if default_tab not in _VALID_TABS:
        default_tab = "memory"

    ui.label("Insights").style(
        f"font-size: 28px; font-weight: 800; color: {THEME['text']}; margin-bottom: 4px;"
    )
    ui.label(
        "Operational analytics — memory health, the data flywheel, and the live Manual Logs."
    ).style(
        f"color: {THEME['text_muted']}; font-size: 14px; margin-bottom: 20px;"
    )

    # ── Tabs header ─────────────────────────────────────────────────────────
    with ui.tabs().style(
        f"background: {THEME['surface']}; border-bottom: 1px solid {THEME['border']};"
    ) as tabs:
        ui.tab("memory", label="💡 Memory")
        ui.tab("flywheel", label="🔁 Flywheel")
        ui.tab("events", label="🔔 Manual Logs")
        ui.tab("actions", label="⏳ Pending Actions")

    # ── Tab panels ──────────────────────────────────────────────────────────
    # Each panel calls the existing page builder verbatim — no logic moves.
    with ui.tab_panels(tabs, value=default_tab).classes("w-full").style(
        "padding-top: 16px;"
    ):
        with ui.tab_panel("memory"):
            build_memory_consolidation_page()
        with ui.tab_panel("flywheel"):
            build_flywheel_page()
        with ui.tab_panel("events"):
            # v0.8.16: render the live, origin-filtered Manual Logs feed at the
            # top so the full-page tab matches the Console mini-pane (capture /
            # manual / scheduled).  The existing pending-notifications file-tail
            # content stays below — this is purely additive.
            from systemu.interface.components.live_events_pane import (
                build_supervisor_events_pane,
            )
            ui.label("🔔 Live Manual Logs").style(
                f"font-size: 14px; font-weight: 700; color: {THEME['text']}; "
                f"margin-bottom: 8px;"
            )
            build_supervisor_events_pane(
                origins=frozenset({"capture", "manual", "scheduled"})
            )
            ui.separator().style("margin: 16px 0;")
            build_notifications_page()
        with ui.tab_panel("actions"):
            _render_pending_decisions()


def _build_pending_decision_view_model(vault):
    """Pure-data helper — testable without NiceGUI runtime.

    Returns a list of dicts:
        [{"id":, "title":, "body":, "options":, "context":, "dedup_key":}]
    OR one of the sentinels:
        {"_no_vault": True}
        {"_empty": True}
        {"_error": str}
    """
    if vault is None:
        return {"_no_vault": True}
    from systemu.approval.decision_queue import OperatorDecisionQueue
    queue = OperatorDecisionQueue(vault)
    try:
        pending = queue.list_pending()
    except Exception as exc:
        return {"_error": str(exc)}
    if not pending:
        return {"_empty": True}
    return [
        {
            "id": d.id,
            "title": d.title,
            "body": d.body,
            "options": list(d.options),
            "context": dict(d.context),
            "dedup_key": d.dedup_key,
        }
        for d in pending
    ]


def render_decision_card(card: dict, queue, on_resolved) -> None:
    """Render one OperatorDecision card with working resolve+dispatch buttons.

    Shared by the /insights Pending Actions tab and the v0.8.8 Console
    pending-actions mini-pane so the resolve + v0.8.5 dispatch behavior is
    defined in exactly one place.

    Args:
        card: dict with keys id, title, body, options, context, dedup_key
              (as produced by ``_build_pending_decision_view_model``).
        queue: ``OperatorDecisionQueue`` instance used to resolve the choice.
        on_resolved: no-arg callback invoked after a successful resolve — each
                     surface passes its own ``@ui.refreshable`` ``.refresh``
                     so the resolved card drops in-place without a full reload.
    """
    with ui.card().style(
        f"background: {THEME['surface']}; border: 1px solid {THEME['border']}; "
        f"border-radius: 12px; padding: 16px; margin-bottom: 12px;"
    ):
        ui.label(card["title"]).style(
            f"font-size: 16px; font-weight: 600; color: {THEME['text']};"
        )
        ui.label(card["body"]).style(
            f"font-size: 13px; color: {THEME['text_muted']}; margin-bottom: 12px;"
        )
        if card["dedup_key"]:
            ui.label(f"dedup: {card['dedup_key']}").style(
                f"font-size: 11px; color: {THEME['text_muted']}; "
                f"font-family: monospace; margin-bottom: 8px;"
            )

        def _make_handler(did, dedup_key, label, the_queue):
            def _click(_e=None):
                try:
                    the_queue.resolve(did, choice=label)
                    ui.notify(f"Resolved: {label}", type="positive")
                    on_resolved()
                    # v0.8.5: dispatch the resolved choice to the registered
                    # pipeline handler in a worker thread so the UI event loop
                    # isn't blocked on multi-second LLM continuations.
                    from systemu.approval.decision_dispatcher import dispatch
                    from systemu.interface.dashboard_state import AppState
                    _state = AppState.get()
                    if _state.config and _state.vault:
                        import threading
                        def _dispatch_resolved():
                            try:
                                decision = _state.vault.get_decision(did)
                            except Exception:
                                logger.exception(
                                    "[Insights] could not load decision %s for dispatch", did,
                                )
                                return
                            dispatch(decision, label, _state.config, _state.vault)
                        # daemon=True trades off shutdown cleanup for unblocked exits:
                        # if the dashboard process exits while this thread is mid-LLM-call,
                        # the thread is killed without finishing the vault write. The
                        # idempotency guard in shadow_decision.decide_shadow (which checks
                        # for an already-assigned shadow on retry) covers the most common
                        # crash-between-saves case, but a kill *during* save_shadow JSON
                        # serialization is not covered. Acceptable for v0.8.5 because the
                        # hourly_shadow_sweep will reconcile any partial state on restart.
                        threading.Thread(
                            target=_dispatch_resolved,
                            daemon=True,
                        ).start()
                except Exception as exc:
                    ui.notify(f"Failed to resolve: {exc}", type="negative")
            return _click

        with ui.row().style("gap: 8px;"):
            for opt in card["options"]:
                ui.button(
                    opt,
                    on_click=_make_handler(card["id"], card["dedup_key"], opt, queue),
                ).style(
                    f"background: {THEME['surface2']}; color: {THEME['text']}; "
                    f"border: 1px solid {THEME['border']}; border-radius: 6px; "
                    f"padding: 6px 14px; font-size: 13px;"
                )

        if card["context"]:
            with ui.expansion("Context", icon="info").style("margin-top: 10px;"):
                ui.json_editor({"content": {"json": card["context"]}}).style(
                    "max-height: 200px;"
                )


@ui.refreshable
def _render_pending_decisions() -> None:
    """Render the Pending Actions tab — OperatorDecision cards from
    the v0.8.0 queue (Pattern 1).

    For each pending decision, draws one card with:
      - Title + body
      - Action buttons (one per option) wired to queue.resolve(...)
      - Context (collapsed) for the operator to inspect.

    Decorated with @ui.refreshable so resolve button handlers can call
    _render_pending_decisions.refresh() to drop the card in-place without
    a full page reload.
    """
    from systemu.interface.dashboard_state import AppState

    state = AppState.get()
    view = _build_pending_decision_view_model(state.vault)

    if isinstance(view, dict) and view.get("_no_vault"):
        ui.label("Vault unavailable.").style(
            f"color: {THEME['text_muted']}; padding: 16px;"
        )
        return
    if isinstance(view, dict) and "_error" in view:
        ui.label(f"Failed to load pending decisions: {view['_error']}").style(
            f"color: {THEME['danger']}; padding: 16px;"
        )
        return
    if isinstance(view, dict) and view.get("_empty"):
        ui.label(
            "No pending operator decisions. (When a CLI subprocess "
            "asks for a decision in queue-mode, it will appear here.)"
        ).style(
            f"color: {THEME['text_muted']}; padding: 16px;"
        )
        return

    # view is a list of card dicts — render each via the shared helper so the
    # resolve + v0.8.5 dispatch behavior lives in exactly one place.
    from systemu.approval.decision_queue import OperatorDecisionQueue

    queue = OperatorDecisionQueue(state.vault)

    for card in view:
        render_decision_card(card, queue, _render_pending_decisions.refresh)
