"""Memory Status — compact per-Shadow memory health summary.

Surfaced as an Overview expansion card.  Reads vault.load_index for
shadows and the memory consolidator's metrics where available.  The
full /memory route still owns deep inspection + Run-Consolidator
controls.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List

from nicegui import ui

from systemu.interface.dashboard_state import AppState, THEME
from systemu.runtime.memory_rules import needs_consolidation


def build_memory_status() -> None:
    """Render the compact per-Shadow memory health summary."""
    state = AppState.get()
    vault = state.vault

    try:
        shadows: List[Dict] = vault.load_index("shadow_army") or []
    except Exception:
        shadows = []

    if not shadows:
        ui.label("No Shadow agents yet — memory tracking activates after the first execution.").style(
            f"color: {THEME['text_muted']}; font-style: italic;"
        )
        return

    rows = []
    for sh in shadows:
        rows.append(_collect_shadow_memory_stats(state, sh))

    # Aggregate header
    total_buffer = sum(r["buffer_lines"] for r in rows)
    total_memory = sum(r["memory_lines"] for r in rows)
    pending      = sum(1 for r in rows if r["pending_consolidation"])

    with ui.row().classes("w-full gap-3 flex-wrap"):
        _stat("edit_note", "Buffer entries",       str(total_buffer), THEME["info"])
        _stat("menu_book", "Consolidated entries", str(total_memory), THEME["primary"])
        _stat("hourglass_empty", "Awaiting consolidation", str(pending), THEME["warning"])

    # Per-shadow rows
    with ui.column().classes("w-full").style(
        f"gap: 0; margin-top: 12px; background: {THEME['surface']}; "
        f"border: 1px solid {THEME['border']}; border-radius: 10px; overflow: hidden;"
    ):
        for r in rows[:8]:  # cap to avoid blowing out the card
            with ui.row().style(
                f"width: 100%; gap: 12px; padding: 8px 12px; align-items: center; "
                f"border-bottom: 1px solid {THEME['border']};"
            ):
                ui.icon("person").style("font-size: 14px; min-width: 18px;")
                with ui.column().style("flex: 1; gap: 1px;"):
                    ui.label(r["name"]).style(
                        f"font-size: 13px; font-weight: 600; color: {THEME['text']};"
                    )
                    ui.label(
                        f"buffer: {r['buffer_lines']} · "
                        f"memory: {r['memory_lines']}"
                    ).style(
                        f"font-size: 11px; color: {THEME['text_muted']};"
                    )
                if r["pending_consolidation"]:
                    ui.label("pending").style(
                        f"font-size: 10px; font-weight: 700; color: {THEME['warning']}; "
                        f"padding: 2px 6px; "
                        f"background: color-mix(in srgb, {THEME['warning']} 15%, transparent); "
                        f"border-radius: 999px;"
                    )

    with ui.row().style("gap: 8px; margin-top: 12px;"):
        ui.button(
            "Open Memory page →",
            on_click=lambda: ui.navigate.to("/insights?tab=memory"),
        ).style(
            f"background: {THEME['surface2']}; color: {THEME['text']}; "
            f"border: 1px solid {THEME['border']}; border-radius: 8px; "
            f"font-size: 12px; padding: 6px 12px;"
        )


def _collect_shadow_memory_stats(state, shadow: Dict) -> Dict:
    name      = shadow.get("name") or shadow.get("id", "?")
    shadow_id = shadow.get("id", "")
    vault     = state.vault

    # Use the *parsed* buffer entries + memory text (the canonical inputs the
    # consolidation page and the engine see) rather than raw file lines, and
    # route the pending flag through the ONE shared needs_consolidation rule.
    try:
        md_text, buf = vault.load_shadow_memory(shadow_id)
    except Exception:
        md_text, buf = "", []

    buffer_lines = len(buf)
    memory_lines = _count_lines(
        Path(state.config.vault_dir) / "shadow_army" / shadow_id / "SHADOW_MEMORY.md"
    )
    pending      = needs_consolidation(buf, md_text)
    return {
        "name":                  name,
        "shadow_id":             shadow_id,
        "buffer_lines":          buffer_lines,
        "memory_lines":          memory_lines,
        "pending_consolidation": pending,
    }


def _count_lines(path: Path) -> int:
    try:
        if not path.exists():
            return 0
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            return sum(1 for _ in f)
    except Exception:
        return 0


def _stat(icon: str, label: str, value: str, color: str) -> None:
    with ui.column().style(
        f"background: {THEME['surface2']}; border: 1px solid {THEME['border']}; "
        f"border-radius: 10px; padding: 10px 14px; min-width: 140px; flex: 1; gap: 2px;"
    ):
        with ui.row().style("align-items: center; gap: 6px;"):
            ui.icon(icon).style("font-size: 16px;")
            ui.label(label).style(
                f"font-size: 11px; color: {THEME['text_muted']}; font-weight: 600; "
                f"text-transform: uppercase; letter-spacing: 0.06em;"
            )
        ui.label(value).style(
            f"font-size: 20px; font-weight: 800; color: {color};"
        )
