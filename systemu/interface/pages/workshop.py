"""NiceGUI Dashboard — Workshop page (Phase 1: editable artifacts).

Tabs:
  Scrolls    — interactive LLM rebuild (unchanged)

Phase 5 Slice 3c: the Tools + Skills tabs were folded out — those edits now
happen IN-PAGE from the Build registry rows (``entity_rows`` →
``components.entity_edit``).
Phase 5 Slice 4c: the Shadows tab was folded out — shadow edit now happens
IN-PAGE from the Shadows (/army) cards (``components.entity_edit.
open_shadow_edit_dialog``, active-lock enforced).  The /workshop route stays
registered: the Scrolls rebuild lives here until Work owns it.
"""

from __future__ import annotations

import json

from nicegui import ui

from systemu.interface.dashboard_state import AppState, THEME


# ─────────────────────────────────────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────────────────────────────────────

def build_workshop_page(deeplink_type: str | None = None, deeplink_id: str | None = None) -> None:
    from systemu.interface.nav_helpers import resolve_deeplink_tab

    state = AppState.get()
    vault = state.vault

    ui.label("🛠️ Workshop").style(
        f"font-size: 22px; font-weight: 800; color: {THEME['text']}; margin-bottom: 20px;"
    )

    target_tab_label = resolve_deeplink_tab(deeplink_type)

    with ui.tabs().classes("w-full") as tabs:
        tab_scrolls    = ui.tab("Scrolls")

    # Phase 5 Slice 2c: the Activities tab is gone — it merely re-mounted
    # build_activities_page(), a duplicate of the /activities route.
    # Phase 5 Slice 3c: the Tools + Skills tabs are gone — those edits fold into
    # the Build registry rows (edit-in-place via components.entity_edit).
    # Phase 5 Slice 4c: the Shadows tab is gone — shadow edit folds into the
    # Shadows (/army) cards (open_shadow_edit_dialog).
    _tab_by_label = {
        "Scrolls": tab_scrolls,
    }
    initial_tab = _tab_by_label.get(target_tab_label, tab_scrolls)

    with ui.tab_panels(tabs, value=initial_tab).classes("w-full bg-transparent"):

        with ui.tab_panel(tab_scrolls):
            _scrolls_tab(state, vault, preselect_id=deeplink_id if deeplink_type == "scroll" else None)


# ─────────────────────────────────────────────────────────────────────────────
#  Scrolls tab — keep existing interactive behaviour intact
# ─────────────────────────────────────────────────────────────────────────────

def _scrolls_tab(state, vault, preselect_id: str | None = None) -> None:
    scrolls = vault.load_index("scrolls")
    if not scrolls:
        ui.label("No scrolls available.").style(f"color: {THEME['text_muted']};")
        return

    ui.label("Select a Scroll to modify:").style(f"font-weight: 600; color: {THEME['text']};")
    options = {s["id"]: f"{s['name']} (ID: {s['id']})" for s in scrolls}
    selected_scroll = ui.radio(options).style(f"color: {THEME['text']}; margin-bottom: 20px;")
    # v0.8.8: pre-select when arriving via Edit deeplink
    if preselect_id and preselect_id in options:
        selected_scroll.value = preselect_id

    def show_scroll_data():
        if not selected_scroll.value:
            ui.notify("Please select a Scroll first", type="warning")
            return
        try:
            s_data = vault.get_scroll(selected_scroll.value).model_dump(mode="json")
            with ui.dialog() as dlg, ui.card().style(
                f"background: {THEME['surface']}; max-width: 800px; width: 100%;"
            ):
                ui.label("Scroll Full Data").classes("text-lg font-bold")
                ui.code(json.dumps(s_data, indent=2), language="json").style(
                    "max-height: 500px; overflow-y: auto;"
                )
                ui.button("Close", on_click=dlg.close)
            dlg.open()
        except Exception as exc:
            ui.notify(f"Error reading scroll: {exc}", type="negative")

    ui.button("Read Selected Scroll", on_click=show_scroll_data).style(
        f"background: {THEME['surface2']}; color: {THEME['text']}; margin-bottom: 20px;"
    )

    ui.label("Modification Instructions:").style(f"font-weight: 600; color: {THEME['text']};")
    prompt_input = ui.textarea("e.g. 'Make the narrative markdown more formal...'").classes("w-full").style(
        f"background: {THEME['surface']}; color: {THEME['text']}; margin-bottom: 20px;"
    )

    async def on_rebuild():
        if not selected_scroll.value:
            ui.notify("Please select a Scroll first", type="warning")
            return
        if not prompt_input.value.strip():
            ui.notify("Please provide a modification prompt", type="warning")
            return
        ui.notify("Rebuilding Scroll... This may take a few moments.", type="info")
        from systemu.pipelines.workshop_module import rebuild_scroll
        try:
            updated = await rebuild_scroll(
                selected_scroll.value, prompt_input.value, state.config, vault
            )
            ui.notify(
                f"Scroll \"{updated.name}\" rebuilt — check Notifications to re-approve and re-run extraction.",
                type="positive",
                timeout=8000,
            )
        except ValueError as ve:
            ui.notify(str(ve), type="negative", timeout=10000)
        except Exception as exc:
            ui.notify(f"Unexpected error: {exc}", type="negative")

    ui.button("Rebuild Scroll", on_click=on_rebuild).style(
        f"background: {THEME['primary']}; color: white; padding: 10px 20px;"
    )
