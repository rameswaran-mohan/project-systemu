"""Header "Status" dropdown (W5.2) — task outcomes at a glance.

The operator had no persistent surface answering "what happened to my
tasks?": outcomes lived behind /work → workflow detail, and the chat history
only on /chat. This renders a Status button next to the "Needs you" badge
whose dropdown lists recent tasks — status pill, outcome message (final
summary / error / park reason), a click-through to the workflow detail, and
the artifacts folder where produced files land.

The row model (:func:`build_status_rows`) is pure so the accounting is
unit-testable without NiceGUI.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Statuses that mean "the operator must act for this task to move".
_ATTENTION_STATUSES = frozenset({"pending_decision", "waiting_on_tools"})

# Human copy for states whose entries carry no summary/error of their own.
_STATUS_FALLBACK_OUTCOME = {
    "running":           "Running…",
    "queued":            "Queued — the Supervisor will pick it up.",
    "pending_decision":  "Waiting for your answer — open the Inbox.",
    "skipped_no_shadow": "No shadow took the task.",
    "cancelled":         "Cancelled.",
}


def _ts_display(ts: str) -> str:
    """ISO timestamp → compact 'YYYY-MM-DD HH:MM' display (best-effort)."""
    s = str(ts or "")
    return s[:16].replace("T", " ")


def build_status_rows(vault, limit: int = 25) -> List[Dict[str, Any]]:
    """Newest-first task rows for the Status dropdown.

    Sourced from the vault chat history (the operator-submitted task log).
    Row: ``{"ts", "ts_display", "name", "status", "outcome", "target",
    "attention"}`` where ``target`` is the workflow-detail route when the
    task got far enough to have a scroll. Defensive: failures yield ``[]``.
    """
    try:
        entries = vault.load_chat_history(limit=limit)  # newest LAST
    except Exception:
        logger.debug("[StatusMenu] could not load chat history", exc_info=True)
        return []
    rows: List[Dict[str, Any]] = []
    for e in reversed(entries):  # newest first
        status = str(e.get("status") or "unknown")
        outcome = (
            str(e.get("summary") or "").strip()
            or str(e.get("error") or "").strip()
            or _STATUS_FALLBACK_OUTCOME.get(status, "")
        )
        scroll_id = e.get("scroll_id")
        rows.append({
            "ts": e.get("ts") or "",
            "ts_display": _ts_display(e.get("ts") or ""),
            "name": str(e.get("prompt") or "(untitled task)")[:90],
            "status": status,
            "outcome": outcome[:240],
            "target": f"/workflow/{scroll_id}" if scroll_id else None,
            "attention": status in _ATTENTION_STATUSES,
        })
    return rows


def artifacts_dir_path() -> Optional[str]:
    """Absolute artifacts folder (config.output_dir) or None — where produced
    files land (per-file tracking doesn't exist; the folder is the link)."""
    from systemu.interface.pages.workflow_detail import artifacts_dir_label
    try:
        return artifacts_dir_label()
    except Exception:
        return None


def render_status_menu(vault) -> None:
    """The header Status button + dropdown (thin shell over the pure model)."""
    from nicegui import ui

    from systemu.interface.design.primitives import button, status_pill
    from systemu.interface.ui_helpers import safe_timer

    with button("Status", variant="ghost"):
        with ui.menu().classes("s-menu").style("min-width: 380px; padding: 8px;"):

            @ui.refreshable
            def _rows() -> None:
                rows = build_status_rows(vault)
                if not rows:
                    ui.label("No tasks yet — submit one from Chat.").classes(
                        "s-muted").style("font-size: 12px; padding: 8px;")
                    return
                with ui.scroll_area().style("max-height: 360px; width: 100%;"):
                    for row in rows:
                        _render_row(row)

            def _render_row(row: Dict[str, Any]) -> None:
                box_classes = "s-row-box" + (" s-banner--warn" if row["attention"] else "")
                with ui.element("div").classes(box_classes).style(
                    "display: flex; flex-direction: column; gap: 2px; "
                    "margin-bottom: 6px; padding: 8px 10px;"
                ):
                    with ui.row().classes("w-full items-center").style("gap: 8px;"):
                        status_pill(row["status"])
                        name = ui.label(row["name"]).classes("s-cell s-cell--bold").style(
                            "flex: 1; overflow: hidden; text-overflow: ellipsis; "
                            "white-space: nowrap;"
                        )
                        if row["target"]:
                            name.style("cursor: pointer;")
                            name.on("click", lambda _, t=row["target"]: ui.navigate.to(t))
                        ui.label(row["ts_display"]).classes("s-mono")
                    if row["outcome"]:
                        ui.label(row["outcome"]).classes("s-muted").style(
                            "font-size: 12px; white-space: normal;"
                        )

            _rows()

            # Artifacts folder — where produced files are stored. Browsers
            # can't open file:// from an http page, so offer copy-to-clipboard.
            path = artifacts_dir_path()
            if path:
                with ui.row().classes("w-full items-center").style(
                    "gap: 8px; margin-top: 8px; padding: 0 8px;"
                ):
                    ui.label("Artifacts:").classes("s-field-label")
                    ui.label(path).classes("s-mono").style(
                        "flex: 1; overflow: hidden; text-overflow: ellipsis; "
                        "white-space: nowrap;"
                    )

                    def _copy(_=None, p=path):
                        escaped = p.replace("\\", "\\\\").replace("'", "\\'")
                        ui.run_javascript(
                            f"navigator.clipboard.writeText('{escaped}')")
                        ui.notify("Artifacts path copied.", type="positive")

                    button("Copy", variant="ghost", on_click=_copy)

            safe_timer(2.0, _rows.refresh)
