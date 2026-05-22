"""Learning Curves — compact summary of the Flywheel metrics.

Surfaced as an Overview expansion card.  The full /flywheel route
still owns deep-dive analysis with per-shadow detail and the SVG
animation.  This component shows only the global headline numbers
and a link to the full page.
"""

from __future__ import annotations

from nicegui import ui

from systemu.interface.dashboard_state import AppState, THEME


def build_learning_curves() -> None:
    """Render the compact Flywheel summary."""
    state = AppState.get()

    try:
        from systemu.runtime.metrics_tracker import load_all_metrics
        all_metrics = load_all_metrics(state.config.vault_dir)
    except Exception:
        all_metrics = []

    total_execs    = sum(m.get("total_executions", 0) for m in all_metrics)
    total_success  = sum(m.get("success_count", 0) for m in all_metrics)
    total_mem      = sum(m.get("memory_entry_count", 0) for m in all_metrics)
    total_high     = sum(m.get("high_confidence_entries", 0) for m in all_metrics)
    success_rate   = (
        round(total_success / total_execs * 100, 1) if total_execs else 0.0
    )

    if total_execs == 0:
        ui.label("No execution data yet — run a Shadow to start the flywheel.").style(
            f"color: {THEME['text_muted']}; font-style: italic;"
        )
        ui.button(
            "Open Flywheel →",
            on_click=lambda: ui.navigate.to("/flywheel"),
        ).style(
            f"background: {THEME['surface2']}; color: {THEME['text']}; "
            f"border: 1px solid {THEME['border']}; border-radius: 8px; "
            f"font-size: 12px; padding: 6px 12px; margin-top: 8px;"
        )
        return

    with ui.row().classes("w-full gap-3 flex-wrap"):
        _tile("⚙️", "Executions",      str(total_execs),     THEME["primary"])
        _tile("✅", "Success rate",    f"{success_rate}%",   THEME["success"])
        _tile("📚", "Memory entries",  str(total_mem),       "#a78bfa")
        _tile("🔥", "High-conf",       str(total_high),      THEME["warning"])
        _tile("👥", "Shadows tracked", str(len(all_metrics)),THEME["info"])

    ui.button(
        "Open Flywheel →",
        on_click=lambda: ui.navigate.to("/flywheel"),
    ).style(
        f"background: {THEME['surface2']}; color: {THEME['text']}; "
        f"border: 1px solid {THEME['border']}; border-radius: 8px; "
        f"font-size: 12px; padding: 6px 12px; margin-top: 12px;"
    )


def _tile(icon: str, label: str, value: str, color: str) -> None:
    with ui.column().style(
        f"background: {THEME['surface2']}; border: 1px solid {THEME['border']}; "
        f"border-radius: 10px; padding: 10px 14px; min-width: 120px; flex: 1; "
        f"gap: 2px;"
    ):
        with ui.row().style("align-items: center; gap: 6px;"):
            ui.label(icon).style("font-size: 16px;")
            ui.label(label).style(
                f"font-size: 11px; color: {THEME['text_muted']}; font-weight: 600; "
                f"text-transform: uppercase; letter-spacing: 0.06em;"
            )
        ui.label(value).style(
            f"font-size: 20px; font-weight: 800; color: {color};"
        )
