"""Skills Snapshot — top-N skills with usage counts.

Surfaced as an Overview expansion card.  The full /skills route owns
search + detail + edit; this component is read-only.
"""

from __future__ import annotations

from typing import Dict, List

from nicegui import ui

from systemu.interface.dashboard_state import AppState, THEME


def build_skills_snapshot(top_n: int = 5) -> None:
    """Render a compact list of the top-N skills."""
    state = AppState.get()
    vault = state.vault

    try:
        skills: List[Dict] = vault.load_index("skills") or []
    except Exception:
        skills = []

    if not skills:
        ui.label("No skills registered yet.  Skills are auto-extracted from your Scrolls.").style(
            f"color: {THEME['text_muted']}; font-style: italic;"
        )
        ui.button(
            "Open Skills →",
            on_click=lambda: ui.navigate.to("/skills"),
        ).style(
            f"background: {THEME['surface2']}; color: {THEME['text']}; "
            f"border: 1px solid {THEME['border']}; border-radius: 8px; "
            f"font-size: 12px; padding: 6px 12px; margin-top: 8px;"
        )
        return

    # Sort by usage / referenced count (best-effort — schemas vary).
    skills_sorted = sorted(
        skills,
        key=lambda s: (
            s.get("usage_count", 0)
            or s.get("referenced_by_count", 0)
            or 0
        ),
        reverse=True,
    )

    with ui.column().classes("w-full").style(
        f"gap: 0; background: {THEME['surface']}; "
        f"border: 1px solid {THEME['border']}; border-radius: 10px; overflow: hidden;"
    ):
        for s in skills_sorted[:top_n]:
            with ui.row().style(
                f"width: 100%; gap: 10px; padding: 8px 12px; align-items: center; "
                f"border-bottom: 1px solid {THEME['border']};"
            ):
                ui.label("🧩").style("font-size: 14px; min-width: 18px;")
                with ui.column().style("flex: 1; gap: 1px;"):
                    ui.label(s.get("name") or s.get("id", "?")).style(
                        f"font-size: 13px; font-weight: 600; color: {THEME['text']};"
                    )
                    desc = (s.get("description") or "").strip()
                    if desc:
                        ui.label(desc[:80]).style(
                            f"font-size: 11px; color: {THEME['text_muted']};"
                        )
                refs = s.get("usage_count", 0) or s.get("referenced_by_count", 0) or 0
                ui.label(f"{refs}×").style(
                    f"font-size: 12px; color: {THEME['primary']}; font-weight: 700; "
                    f"min-width: 36px; text-align: right;"
                )

    with ui.row().style("margin-top: 12px;"):
        ui.label(f"Showing top {min(top_n, len(skills))} of {len(skills)}").style(
            f"font-size: 11px; color: {THEME['text_muted']}; flex: 1;"
        )
        ui.button(
            "Open Skills →",
            on_click=lambda: ui.navigate.to("/skills"),
        ).style(
            f"background: {THEME['surface2']}; color: {THEME['text']}; "
            f"border: 1px solid {THEME['border']}; border-radius: 8px; "
            f"font-size: 12px; padding: 6px 12px;"
        )
