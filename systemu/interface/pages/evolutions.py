"""NiceGUI Dashboard — Evolutions page.

Shows pending proposals with Approve / Reject buttons.
Approved proposals are optionally applied immediately.
Shows history of resolved (approved/rejected) evolutions.
"""

from __future__ import annotations

from nicegui import ui

from systemu.core.utils import utcnow
from systemu.interface.dashboard_state import AppState, THEME, status_badge_html
from systemu.interface.name_resolver import resolve_names


_TYPE_ICON = {
    "upgrade":  "⬆️",
    "merge":    "🔀",
    "split":    "✂️",
    "combine":  "🔗",
    "discover": "💡",
}


def build_evolutions_page() -> None:
    state = AppState.get()
    vault = state.vault

    with ui.row().classes("w-full items-center justify-between").style("margin-bottom: 20px;"):
        ui.label("🧬 Evolution Engine").style(
            f"font-size: 22px; font-weight: 800; color: {THEME['text']};"
        )
        ui.button("▶ Run Evolution Check Now", on_click=_run_evolution).style(
            f"background: {THEME['primary']}; color: white; border-radius: 8px;"
        )

    evolutions = vault.load_index("evolutions")
    pending    = [e for e in evolutions if e.get("status") == "proposed"]
    history    = [e for e in evolutions if e.get("status") in ("approved", "rejected", "applied")]

    # ── Pending Proposals ──────────────────────────────────────────────────
    ui.label(f"Pending Proposals ({len(pending)})").style(
        f"font-size: 15px; font-weight: 700; color: {THEME['warning']}; margin-bottom: 12px;"
    )

    if not pending:
        ui.label("No pending proposals. Run an evolution check or wait for the daily schedule.").style(
            f"color: {THEME['text_muted']}; font-style: italic; margin-bottom: 24px;"
        )
    else:
        for evo in pending:
            _proposal_card(evo)

    ui.separator().style(f"background: {THEME['border']}; margin: 24px 0;")

    # ── History ────────────────────────────────────────────────────────────
    ui.label(f"Resolved History ({len(history)})").style(
        f"font-size: 15px; font-weight: 700; color: {THEME['text']}; margin-bottom: 12px;"
    )
    if not history:
        ui.label("No resolved evolutions yet.").style(
            f"color: {THEME['text_muted']}; font-style: italic;"
        )
    else:
        with ui.element("table").style(
            f"width: 100%; border-collapse: collapse; background: {THEME['surface']}; "
            f"border: 1px solid {THEME['border']}; border-radius: 12px; overflow: hidden;"
        ):
            with ui.element("thead"):
                with ui.element("tr"):
                    for col in ["Type", "Target", "Description", "Status"]:
                        with ui.element("th").style(
                            f"background: {THEME['surface2']}; color: {THEME['text_muted']}; "
                            f"font-size: 11px; font-weight: 600; text-transform: uppercase; "
                            f"letter-spacing: 0.08em; padding: 10px 16px; "
                            f"text-align: left; border-bottom: 1px solid {THEME['border']};"
                        ):
                            ui.label(col)
            with ui.element("tbody"):
                for evo in history[-20:]:
                    with ui.element("tr"):
                        etype = evo.get("evolution_type", "?")
                        _td(f"{_TYPE_ICON.get(etype, '🔹')} {etype}")
                        _td(evo.get("target_entity_type", "—"))
                        _td((evo.get("description", "") or "")[:80] + "…"
                            if len(evo.get("description", "")) > 80
                            else evo.get("description", "—"))
                        with ui.element("td").style("padding: 12px 16px;"):
                            ui.html(status_badge_html(evo.get("status", "?")))


def _proposal_card(evo: dict) -> None:
    etype = evo.get("evolution_type", "?")
    icon  = _TYPE_ICON.get(etype, "🔹")
    eid   = evo.get("id", "")

    with ui.card().style(
        f"background: {THEME['surface']}; border: 1px solid {THEME['border']}; "
        f"border-left: 4px solid {THEME['warning']}; "
        f"border-radius: 12px; padding: 20px; margin-bottom: 12px; width: 100%;"
    ):
        # Header
        with ui.row().style("align-items: center; justify-content: space-between; margin-bottom: 8px;"):
            with ui.row().style("align-items: center; gap: 10px;"):
                ui.label(icon).style("font-size: 20px;")
                ui.label(f"{etype.upper()} — {evo.get('target_entity_type', '?')}").style(
                    f"font-size: 14px; font-weight: 700; color: {THEME['warning']};"
                )
            ui.label(", ".join(resolve_names(evo.get("target_ids", [])[:3], AppState.get().vault))).style(
                f"font-size: 12px; color: {THEME['text_muted']};"
            )

        # Description
        ui.label(evo.get("description", "")).style(
            f"font-size: 15px; font-weight: 600; color: {THEME['text']}; margin-bottom: 6px;"
        )
        # Rationale
        ui.label(evo.get("rationale", "")).style(
            f"font-size: 13px; color: {THEME['text_muted']}; line-height: 1.5; margin-bottom: 12px;"
        )
        # Buttons
        with ui.row().style("gap: 10px;"):
            ui.button(
                "✓ Approve",
                on_click=lambda _, i=eid: _approve_evolution(i),
            ).style(
                f"background: {THEME['success']}; color: white; border-radius: 8px; font-size: 13px;"
            )
            ui.button(
                "✗ Reject",
                on_click=lambda _, i=eid: _reject_evolution(i),
            ).style(
                f"background: {THEME['danger']}; color: white; border-radius: 8px; font-size: 13px;"
            )


def _approve_evolution(evolution_id: str) -> None:
    state = AppState.get()
    try:
        from systemu.core.models import EvolutionStatus
        from datetime import datetime
        evo = state.vault.get_evolution(evolution_id)
        evo.status = EvolutionStatus.APPROVED
        state.vault.save_evolution(evo)
        ui.notify(f"Evolution approved. Use 'evolve apply {evolution_id}' to apply.", type="positive")
    except Exception as exc:
        ui.notify(f"Error: {exc}", type="negative")


def _reject_evolution(evolution_id: str) -> None:
    state = AppState.get()
    try:
        from systemu.core.models import EvolutionStatus
        from datetime import datetime
        evo = state.vault.get_evolution(evolution_id)
        evo.status = EvolutionStatus.REJECTED
        evo.resolved_at = utcnow()
        state.vault.save_evolution(evo)
        ui.notify("Evolution rejected.", type="warning")
    except Exception as exc:
        ui.notify(f"Error: {exc}", type="negative")


def _run_evolution() -> None:
    """Dispatch evolution check as background job to avoid blocking the UI."""
    from systemu.interface.jobs import JobManager
    import sys
    from pathlib import Path
    state = AppState.get()
    jm = JobManager.get()
    cwd = state.project_root
    cmd = [sys.executable, "-m", "sharing_on", "evolve", "run"]
    jm.start_job("Evolution Check", "evolve", cmd, cwd)
    ui.notify("Evolution check running in background — check Active Tasks.", type="positive")


def _td(text: str) -> None:
    with ui.element("td").style(
        f"padding: 12px 16px; border-bottom: 1px solid {THEME['border']}; "
        f"color: {THEME['text']}; font-size: 13px;"
    ):
        ui.label(text)
