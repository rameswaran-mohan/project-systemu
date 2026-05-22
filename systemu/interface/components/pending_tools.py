"""Pending Tools — proposed tools awaiting review.

Surfaced as an Overview expansion card.  Highlights tools the Tool
Forge has proposed but the operator hasn't yet enabled.  The full
/tools route owns the full registry (forged + deployed + retired).
"""

from __future__ import annotations

from typing import Dict, List

from nicegui import ui

from systemu.interface.dashboard_state import AppState, THEME


def build_pending_tools() -> None:
    """Render the list of tools in 'proposed' status."""
    state = AppState.get()
    vault = state.vault

    try:
        tools: List[Dict] = vault.load_index("tools") or []
    except Exception:
        tools = []

    proposed = [t for t in tools if t.get("status") == "proposed"]

    if not proposed:
        ui.label("Nothing pending — every Tool Forge proposal has been reviewed.").style(
            f"color: {THEME['text_muted']}; font-style: italic;"
        )
        ui.button(
            "Open Tools →",
            on_click=lambda: ui.navigate.to("/tools"),
        ).style(
            f"background: {THEME['surface2']}; color: {THEME['text']}; "
            f"border: 1px solid {THEME['border']}; border-radius: 8px; "
            f"font-size: 12px; padding: 6px 12px; margin-top: 8px;"
        )
        return

    ui.label(
        f"{len(proposed)} tool{'s' if len(proposed) != 1 else ''} waiting for your review."
    ).style(
        f"color: {THEME['warning']}; font-size: 13px; font-weight: 600; margin-bottom: 8px;"
    )

    with ui.column().classes("w-full").style(
        f"gap: 0; background: {THEME['surface']}; "
        f"border: 1px solid {THEME['border']}; border-radius: 10px; overflow: hidden;"
    ):
        for t in proposed[:10]:
            with ui.row().style(
                f"width: 100%; gap: 10px; padding: 8px 12px; align-items: center; "
                f"border-bottom: 1px solid {THEME['border']};"
            ):
                ui.label("🔧").style("font-size: 14px; min-width: 18px;")
                with ui.column().style("flex: 1; gap: 1px;"):
                    ui.label(t.get("name") or t.get("id", "?")).style(
                        f"font-size: 13px; font-weight: 600; color: {THEME['text']};"
                    )
                    desc = (t.get("description") or "").strip()
                    if desc:
                        ui.label(desc[:90]).style(
                            f"font-size: 11px; color: {THEME['text_muted']};"
                        )
                ui.label("proposed").style(
                    f"font-size: 10px; font-weight: 700; color: {THEME['warning']}; "
                    f"padding: 2px 8px; "
                    f"background: color-mix(in srgb, {THEME['warning']} 15%, transparent); "
                    f"border-radius: 999px;"
                )

    ui.button(
        "Review proposed tools →",
        on_click=lambda: ui.navigate.to("/tools"),
    ).style(
        f"background: {THEME['warning']}; color: white; border-radius: 8px; "
        f"font-size: 12px; padding: 6px 12px; margin-top: 12px; font-weight: 600;"
    )
