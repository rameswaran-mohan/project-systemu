"""In-place Scroll rebuild dialog (Phase 6 Slice 6f).

Workshop's last surface — the interactive Scrolls rebuild (read the scroll,
type a modification prompt, run the LLM ``rebuild_scroll`` pipeline) — is lifted
out of the dissolving ``/workshop`` route into a dialog the Scrolls page opens
in-place from its row ``✏️ Edit`` button.

The save path is UNCHANGED: ``workshop_module.rebuild_scroll(scroll_id, prompt,
config, vault)`` — the same pipeline ``scroll_remediator`` and the v0.8.x bundle
tests exercise — runs exactly as it did inside ``_scrolls_tab``.

Import-light, same discipline as ``scroll_gate``: NiceGUI is imported only inside
``open_scroll_rebuild_dialog``, so the testable applier (``apply_scroll_rebuild``)
runs headless.
"""
from __future__ import annotations

from typing import Callable, Optional

from systemu.pipelines.workshop_module import rebuild_scroll


async def apply_scroll_rebuild(
    scroll_id: str,
    prompt: str,
    config,
    vault,
    *,
    on_saved: Optional[Callable[[], None]] = None,
):
    """Run the UNCHANGED ``rebuild_scroll`` pipeline for ``scroll_id`` with
    ``prompt``, then fire ``on_saved``.

    Returns the rebuilt :class:`Scroll` on success, or ``False`` when the prompt
    is blank (the dialog shell turns that into a warning notify — no pipeline
    call).  Pipeline errors (``ValueError`` from not-found / validation, or any
    other ``Exception``) propagate to the caller, which maps them to a negative
    notify; ``on_saved`` does NOT fire in that case.
    """
    if not prompt or not prompt.strip():
        return False
    updated = await rebuild_scroll(scroll_id, prompt, config, vault)
    if on_saved is not None:
        on_saved()
    return updated


def open_scroll_rebuild_dialog(
    scroll_id: str, *, on_saved: Optional[Callable[[], None]] = None,
) -> None:
    """Open the in-place Scroll rebuild dialog for ``scroll_id``.

    Renders the read-current-data + modification-prompt + Rebuild UI that used
    to live in the Workshop Scrolls tab, wired to the UNCHANGED
    ``rebuild_scroll`` pipeline via :func:`apply_scroll_rebuild`.  ``on_saved``
    runs after a successful rebuild (callers refresh their rows).
    """
    import json

    from nicegui import ui

    from systemu.interface.dashboard_state import AppState
    from systemu.interface.design import card

    state = AppState.get()
    vault = state.vault

    try:
        scroll = vault.get_scroll(scroll_id)
    except KeyError:
        ui.notify("Scroll not found.", type="negative")
        return

    scroll_name = getattr(scroll, "name", scroll_id)

    with ui.dialog() as dlg, card(classes="s-dialog q-pa-lg"):
        ui.label(f"🛠️ Rebuild Scroll — {scroll_name}").classes("s-dialog-title q-mb-md")

        def _show_scroll_data() -> None:
            try:
                s_data = vault.get_scroll(scroll_id).model_dump(mode="json")
            except Exception as exc:
                ui.notify(f"Error reading scroll: {exc}", type="negative")
                return
            with ui.dialog() as data_dlg, card(classes="s-dialog q-pa-lg"):
                ui.label("Scroll Full Data").classes("s-dialog-title q-mb-md")
                ui.code(json.dumps(s_data, indent=2), language="json").classes(
                    "s-input-full"
                )
                ui.button("Close", on_click=data_dlg.close).classes(
                    "s-btn s-btn--ghost q-mt-md")
            data_dlg.open()

        ui.button("Read Current Scroll", on_click=_show_scroll_data).classes(
            "s-btn s-btn--ghost q-mb-md")

        ui.label("Modification Instructions:").classes("s-section-head")
        prompt_input = ui.textarea(
            placeholder="e.g. 'Make the narrative markdown more formal...'"
        ).classes("s-input s-input-full q-mb-md")

        async def _on_rebuild() -> None:
            if not (prompt_input.value or "").strip():
                ui.notify("Please provide a modification prompt", type="warning")
                return
            ui.notify("Rebuilding Scroll... This may take a few moments.", type="info")
            try:
                updated = await apply_scroll_rebuild(
                    scroll_id, prompt_input.value, state.config, vault,
                    on_saved=on_saved,
                )
            except ValueError as ve:
                ui.notify(str(ve), type="negative", timeout=10000)
                return
            except Exception as exc:  # noqa: BLE001 — surface any pipeline error
                ui.notify(f"Unexpected error: {exc}", type="negative")
                return
            ui.notify(
                f'Scroll "{updated.name}" rebuilt — check Notifications to '
                "re-approve and re-run extraction.",
                type="positive",
                timeout=8000,
            )
            dlg.close()

        with ui.row().classes("q-gutter-sm q-mt-md"):
            ui.button("Rebuild Scroll", on_click=_on_rebuild).classes(
                "s-btn s-btn--primary")
            ui.button("Cancel", on_click=dlg.close).classes("s-btn s-btn--ghost")
    dlg.open()
