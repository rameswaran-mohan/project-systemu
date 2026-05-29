"""Console page (v0.8.8) — single-screen operator console.

Layout (top to bottom):
  1. Header: "🖥️ Console" + horizontal Quick Actions row
  2. Six clickable stat tiles (navigate to list pages)
  3. Pending Actions mini-pane (scrollable, capped height)
  4. Two event panes: Supervisor live events (left) + Insights event log (right)
  5. "More" — five collapsed lazy-loaded expansion cards

Replaces the v0.7.2 Overview. build_overview_page is re-exported from
overview.py for back-compat.
"""
from __future__ import annotations

from nicegui import ui

from systemu.interface.dashboard_state import AppState, THEME
from systemu.interface.nav_helpers import tile_nav_target


def build_console_page() -> None:
    state = AppState.get()
    vault = state.vault

    scrolls    = vault.load_index("scrolls")
    shadows    = vault.load_index("shadow_army")
    tools      = vault.load_index("tools")
    skills     = vault.load_index("skills")
    evolutions = vault.load_index("evolutions")
    activities = vault.load_index("activities")

    pending_evolutions = [e for e in evolutions if e.get("status") == "proposed"]
    pending_scrolls    = [s for s in scrolls    if s.get("status") == "pending_approval"]

    # ── Header + Quick Actions ─────────────────────────────────────────
    with ui.row().style("width: 100%; align-items: center; justify-content: space-between; margin-bottom: 16px;"):
        ui.label("🖥️ Console").style(
            f"font-size: 26px; font-weight: 800; color: {THEME['text']};"
        )
        with ui.row().style("gap: 8px;"):
            ui.button("⚡ Record", on_click=_trigger_record_dialog).style(
                f"background: {THEME['primary']}; color: white; border-radius: 8px; font-size: 13px;"
            )
            ui.button("🧬 Evolve", on_click=_run_evolution).style(
                f"background: {THEME['surface2']}; color: {THEME['text']}; "
                f"border: 1px solid {THEME['border']}; border-radius: 8px; font-size: 13px;"
            )
            ui.button("🔔 Notifications", on_click=lambda: ui.navigate.to("/notifications")).style(
                f"background: {THEME['surface2']}; color: {THEME['text']}; "
                f"border: 1px solid {THEME['border']}; border-radius: 8px; font-size: 13px;"
            )
            ui.button("🔧 Forge", on_click=lambda: ui.navigate.to("/tools")).style(
                f"background: {THEME['surface2']}; color: {THEME['text']}; "
                f"border: 1px solid {THEME['border']}; border-radius: 8px; font-size: 13px;"
            )
            ui.button("↻ Restart Workers", on_click=_force_restart_workers).style(
                f"background: {THEME['surface2']}; color: {THEME['warning']}; "
                f"border: 1px solid {THEME['warning']}; border-radius: 8px; font-size: 13px;"
            ).tooltip("Restart Supervisor threads — use when a shadow appears stuck")

    # ── Clickable stat tiles ───────────────────────────────────────────
    with ui.row().classes("w-full gap-4 flex-wrap"):
        _stat_card("📜", "Scrolls",    len(scrolls),
                   THEME["primary"], f"{len(pending_scrolls)} pending", nav_target=tile_nav_target("Scrolls"))
        _stat_card("👥", "Shadows",    len(shadows),
                   THEME["info"], f"{len([s for s in shadows if s.get('status')=='awakened'])} active",
                   nav_target=tile_nav_target("Shadows"))
        _stat_card("🔧", "Tools",      len(tools),
                   THEME["success"], f"{len([t for t in tools if t.get('status')=='forged'])} forged",
                   nav_target=tile_nav_target("Tools"))
        _stat_card("🧠", "Skills",     len(skills),
                   "#a78bfa", "across all shadows", nav_target=tile_nav_target("Skills"))
        _stat_card("📋", "Activities", len(activities),
                   THEME["info"], f"{len([a for a in activities if a.get('status')=='unassigned'])} unassigned",
                   nav_target=tile_nav_target("Activities"))
        _stat_card("🧬", "Evolutions", len(pending_evolutions),
                   THEME["warning"], "pending review", nav_target=tile_nav_target("Evolutions"))

    ui.separator().style(f"background:{THEME['border']}; margin: 20px 0;")

    # ── Pending Actions mini-pane ──────────────────────────────────────
    with ui.row().style("width: 100%; align-items: center; gap: 8px;"):
        ui.label("⏳ Pending Actions").style(
            f"font-size: 15px; font-weight: 700; color: {THEME['text']};"
        )
        ui.link("→ Insights", "/insights?tab=actions").style(
            f"font-size: 12px; color: {THEME['primary']};"
        )
    _build_pending_actions_minipane(vault)

    ui.separator().style(f"background:{THEME['border']}; margin: 20px 0;")

    # ── Two event panes ────────────────────────────────────────────────
    with ui.row().classes("w-full gap-4").style("flex-wrap: wrap;"):
        with ui.column().style("flex: 1; min-width: 360px;"):
            ui.label("▶️ Supervisor Live Events").style(
                f"font-size: 14px; font-weight: 700; color: {THEME['text']}; margin-bottom: 8px;"
            )
            from systemu.interface.components.live_events_pane import build_supervisor_events_pane
            build_supervisor_events_pane()
        with ui.column().style("flex: 1; min-width: 360px;"):
            ui.label("🔔 Events Log").style(
                f"font-size: 14px; font-weight: 700; color: {THEME['text']}; margin-bottom: 8px;"
            )
            from systemu.interface.pages.notifications_page import build_events_log_pane
            build_events_log_pane()

    ui.separator().style(f"background:{THEME['border']}; margin: 20px 0;")

    # ── More (collapsed expansions) ────────────────────────────────────
    _build_expansions()


def _build_pending_actions_minipane(vault) -> None:
    """Compact pending-decisions list reusing the shared render helper."""
    from systemu.interface.pages.insights import (
        _build_pending_decision_view_model, render_decision_card,
    )
    from systemu.approval.decision_queue import OperatorDecisionQueue

    view = _build_pending_decision_view_model(vault)

    @ui.refreshable
    def _mini():
        if isinstance(view, dict) and (view.get("_empty") or view.get("_no_vault")):
            ui.label("No pending actions.").style(f"color: {THEME['text_muted']}; font-size: 12px;")
            return
        if isinstance(view, dict) and "_error" in view:
            ui.label(f"Error: {view['_error']}").style(f"color: {THEME['danger']}; font-size: 12px;")
            return
        queue = OperatorDecisionQueue(vault)
        for card in view:
            render_decision_card(card, queue, _mini.refresh)

    with ui.scroll_area().style("max-height: 160px; width: 100%;"):
        _mini()


# ── Stat card (now nav-aware) ──────────────────────────────────────────

def _stat_card(icon: str, label: str, value: int, color: str, subtitle: str,
               nav_target: str | None = None) -> None:
    base = (
        f"background: {THEME['surface']}; border: 1px solid {THEME['border']}; "
        f"border-radius: 12px; padding: 20px 24px; min-width: 140px; "
        f"align-items: center; gap: 4px; flex: 1;"
    )
    if nav_target:
        base += " cursor: pointer;"
    card = ui.column().style(base)
    if nav_target:
        card.on("click", lambda t=nav_target: ui.navigate.to(t))
        card.classes("s-tile-clickable")
    with card:
        ui.label(icon).style("font-size: 28px;")
        ui.label(str(value)).style(f"font-size: 36px; font-weight: 800; color: {color};")
        ui.label(label).style(f"font-size: 14px; font-weight: 600; color: {THEME['text']};")
        ui.label(subtitle).style(f"font-size: 11px; color: {THEME['text_muted']};")


# ── Expansions + handlers (carried over verbatim from overview.py) ─────
# The activity-feed helpers _activity_row / _build_activity_feed are NOT
# carried over — the feed is removed in v0.8.8 (the event panes supersede it).

def _build_expansions() -> None:
    """Render the four collapsed-by-default Console expansion cards.

    Lazy-load each card body on expansion open — keeps the initial
    Console render cheap.
    """
    ui.label("More").style(
        f"font-size: 13px; color: {THEME['text_muted']}; font-weight: 700; "
        f"letter-spacing: 0.08em; text-transform: uppercase; margin-bottom: 8px;"
    )

    _expansion("📈", "Learning Curves",
               "Compact view of Shadow execution metrics and the data flywheel.",
               _load_learning_curves)

    _expansion("🧠", "Memory Status",
               "Per-Shadow memory buffer health and consolidation readiness.",
               _load_memory_status)

    _expansion("🧩", "Skills Snapshot",
               "Top skills by usage, with a link to the full skill registry.",
               _load_skills_snapshot)

    _expansion("🔧", "Pending Tools",
               "Tools the Tool Forge has proposed but you haven't enabled yet.",
               _load_pending_tools)

    _expansion("📦", "Tool Dependencies",
               "Pip packages awaiting your approval before tools may install them.",
               _load_pending_deps)


def _expansion(icon: str, title: str, subtitle: str, body_loader) -> None:
    """Wrap a lazy-loaded expansion card.  body_loader is called on first open."""
    with ui.expansion(
        title,
        icon="",  # we render our own glyph
    ).classes("w-full").style(
        f"background: {THEME['surface']}; border: 1px solid {THEME['border']}; "
        f"border-radius: 12px; margin-bottom: 12px;"
    ) as exp:
        with exp.add_slot("header"):
            with ui.row().style("align-items: center; gap: 10px;"):
                ui.label(icon).style("font-size: 18px;")
                with ui.column().style("gap: 0;"):
                    ui.label(title).style(
                        f"font-size: 14px; font-weight: 700; color: {THEME['text']};"
                    )
                    ui.label(subtitle).style(
                        f"font-size: 11px; color: {THEME['text_muted']};"
                    )

        # Lazy body: build once on first open, then keep.
        body_state = {"built": False}
        body_container = ui.column().classes("w-full").style("padding: 6px 12px 12px;")

        def _build_body() -> None:
            if body_state["built"]:
                return
            with body_container:
                try:
                    body_loader()
                except Exception as exc:
                    ui.label(f"Error loading: {exc}").style(
                        f"color: {THEME['danger']}; font-size: 12px;"
                    )
            body_state["built"] = True

        exp.on("update:model-value", lambda _e: _build_body())


# ── Body loaders ───────────────────────────────────────────────────────

def _load_learning_curves() -> None:
    from systemu.interface.components.learning_curves import build_learning_curves
    build_learning_curves()


def _load_memory_status() -> None:
    from systemu.interface.components.memory_status import build_memory_status
    build_memory_status()


def _load_skills_snapshot() -> None:
    from systemu.interface.components.skills_snapshot import build_skills_snapshot
    build_skills_snapshot()


def _load_pending_tools() -> None:
    from systemu.interface.components.pending_tools import build_pending_tools
    build_pending_tools()


def _load_pending_deps() -> None:
    from systemu.interface.components.pending_deps import build_pending_deps
    build_pending_deps(compact=True)


# ── Quick-action handlers ──────────────────────────────────────────────

def _force_restart_workers() -> None:
    """Gracefully restart the Supervisor background threads."""
    try:
        from systemu.runtime.supervisor import Supervisor
        import time as _time
        sup = Supervisor.get()
        sup._shutdown_event.set()
        _time.sleep(0.5)
        sup._shutdown_event.clear()
        sup.start()
        ui.notify("Workers restarted successfully.", type="positive")
    except Exception as exc:
        ui.notify(f"Restart failed: {exc}", type="negative")


def _trigger_record_dialog() -> None:
    """Open the record session dialog (reuses dashboard.py handler)."""
    try:
        from systemu.interface.dashboard import _show_record_dialog
        _show_record_dialog()
    except Exception as exc:
        ui.notify(f"Could not open record dialog: {exc}", type="negative")


def _run_evolution() -> None:
    """Trigger the evolution engine as a background job to avoid blocking the UI."""
    from systemu.interface.jobs import JobManager
    import sys

    state = AppState.get()
    jm = JobManager.get()
    cwd = state.project_root
    cmd = [sys.executable, "-m", "sharing_on", "evolve", "run"]
    jm.start_job("Evolution Check", "evolve", cmd, cwd)
    ui.notify("Evolution check dispatched as background job.", type="positive")
