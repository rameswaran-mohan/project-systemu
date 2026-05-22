"""Workflow Pipeline card — visualises in-flight workflows by stage.

Used by:
    • Overview (top of page, below stats)
    • Systemu Chat sidebar

Reads from ``WorkflowTracker.get()`` and refreshes every 2 s so the
operator can see workflows advancing through the pipeline live.

Each stage shows a count and the workflows currently sitting in it.
Clicking a workflow opens its detail page (`/workflow/<id>`).
"""

from __future__ import annotations

from nicegui import ui

from systemu.interface.dashboard_state import THEME
from systemu.runtime.workflow_tracker import STAGES, WorkflowTracker


_STAGE_ICONS = {
    "capture":   "🎙️",
    "scroll":    "📜",
    "activity":  "📋",
    "execution": "⚙️",
    "done":      "✅",
    "failed":    "⚠️",
}

_STAGE_COLORS = {
    "capture":   THEME["info"],
    "scroll":    THEME["primary"],
    "activity":  "#a78bfa",
    "execution": THEME["warning"],
    "done":      THEME["success"],
    "failed":    THEME["danger"],
}


def build_workflow_pipeline(*, compact: bool = False, refresh_seconds: float = 2.0) -> None:
    """Render the Workflow Pipeline card.

    Args:
        compact:         When True, render a tighter version suitable for the
                         Systemu Chat sidebar.  Drops the per-stage workflow
                         list and shows only stage chips.
        refresh_seconds: Cadence to re-poll the tracker.  2 s is the default
                         and keeps it responsive without burning CPU.
    """
    tracker = WorkflowTracker.get()

    @ui.refreshable
    def _render():
        # Reconcile from the vault on every render — covers the case
        # where the runtime updates an activity's status without
        # publishing a matching EventBus event (the tracker's
        # event-subscription path is best-effort).  Cheap O(N) walk.
        try:
            tracker.refresh_from_vault()
        except Exception:
            pass
        counts = tracker.counts_by_stage()
        active = tracker.list_active()

        # ── Title row ──────────────────────────────────────────────────
        with ui.row().classes("w-full items-center").style(
            "gap: 12px; margin-bottom: 10px;"
        ):
            ui.label("🔄 Workflows").style(
                f"font-size: 15px; font-weight: 700; color: {THEME['text']};"
            )
            ui.label(f"{len(active)} in-flight").style(
                f"font-size: 12px; color: {THEME['text_muted']};"
            )

        # ── Stage chip row ─────────────────────────────────────────────
        with ui.row().classes("w-full flex-wrap").style(
            f"gap: 8px; padding: 12px; background: {THEME['surface']}; "
            f"border: 1px solid {THEME['border']}; border-radius: 10px;"
        ):
            for idx, stage in enumerate(STAGES):
                _stage_chip(stage, counts.get(stage, 0))
                if idx < len(STAGES) - 1:
                    ui.label("▸").style(
                        f"color: {THEME['text_muted']}; font-size: 14px; "
                        f"align-self: center;"
                    )

        # ── Active workflow list (full mode only) ──────────────────────
        if compact or not active:
            return

        with ui.column().classes("w-full").style(
            f"gap: 6px; margin-top: 10px; padding: 10px; "
            f"background: {THEME['surface']}; border: 1px solid {THEME['border']}; "
            f"border-radius: 10px;"
        ):
            ui.label("Active").style(
                f"font-size: 11px; font-weight: 700; color: {THEME['text_muted']}; "
                f"letter-spacing: 0.08em; text-transform: uppercase;"
            )
            # Show the most-recently-updated 6 first
            for w in sorted(active, key=lambda x: x.updated_at, reverse=True)[:6]:
                _workflow_row(w)

    _render()
    ui.timer(refresh_seconds, _render.refresh)


def _stage_chip(stage: str, count: int) -> None:
    icon  = _STAGE_ICONS.get(stage, "•")
    color = _STAGE_COLORS.get(stage, THEME["text_muted"])
    with ui.row().style(
        f"align-items: center; gap: 6px; padding: 6px 10px; "
        f"background: color-mix(in srgb, {color} 10%, transparent); "
        f"border: 1px solid color-mix(in srgb, {color} 35%, transparent); "
        f"border-radius: 999px;"
    ):
        ui.label(icon).style("font-size: 13px;")
        ui.label(stage.upper()).style(
            f"font-size: 10px; font-weight: 700; color: {color}; letter-spacing: 0.06em;"
        )
        ui.label(str(count)).style(
            f"font-size: 12px; font-weight: 700; color: {THEME['text']};"
        )


def _workflow_row(snap) -> None:
    icon  = _STAGE_ICONS.get(snap.stage, "•")
    color = _STAGE_COLORS.get(snap.stage, THEME["text_muted"])
    with ui.row().style(
        f"width: 100%; gap: 10px; padding: 6px 8px; align-items: center; "
        f"border-radius: 6px; cursor: pointer;"
    ).on(
        "click", lambda _, wid=snap.workflow_id: ui.navigate.to(f"/workflow/{wid}")
    ):
        ui.label(icon).style("font-size: 14px; min-width: 18px;")
        with ui.column().style("flex: 1; gap: 2px;"):
            ui.label(snap.title[:60]).style(
                f"font-size: 13px; color: {THEME['text']}; font-weight: 600;"
            )
            ui.label(f"{snap.stage} · {snap.status}").style(
                f"font-size: 11px; color: {color};"
            )
