"""R-P3b — the /privacy "What leaves this machine" page (§15.7 honest interim rule).

A thin renderer over ``runtime.privacy.privacy_report()`` (the pure, unit-tested
data). Token classes only — no inline colors / raw hex (mirrors /health).
"""
from __future__ import annotations


def build_privacy_page() -> None:
    """Render /privacy. The data is deterministic + tested; this only lays it out."""
    from nicegui import ui

    from systemu.runtime.privacy import privacy_report

    report = privacy_report()

    with ui.column().classes("w-full items-center"):
        with ui.column().classes("s-card").style("max-width: 860px; width: 100%;"):
            ui.label("What leaves this machine").classes("s-page-title")
            ui.label(report["headline"]).classes("s-muted")

            for s in report["sections"]:
                ui.label(s["title"]).classes("s-section-head")
                if s.get("severity") == "warn":
                    ui.label(s["detail"]).classes("s-banner s-banner--warn w-full")
                else:
                    ui.label(s["detail"]).classes("s-cell w-full")

            ui.label(
                "This is the honest current reality — not a promise of zero egress. "
                "Local-first here means custody, verification, and the vault."
            ).classes("s-muted")
