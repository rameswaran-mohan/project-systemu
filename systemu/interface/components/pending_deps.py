"""Pending Tool Dependencies — pip packages waiting for operator approval.

When the dependency installer runs in PROMPT mode (the local-mode default)
and a Shadow encounters a tool that needs an un-approved package, the
package is recorded as pending in :class:`DepApprovalStore`.  This card
surfaces those entries so the operator can approve / dismiss them
without dropping to the CLI.

Mirrors ``pending_tools`` in shape: list with one row per package, an
Approve button per row, and a small CTA toward the full Tools page when
there are no entries.  Lives in two places:

  * Tools page (full list, primary action surface)
  * Overview expansion (badge-style summary, lazy-loaded)
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

from nicegui import ui

from systemu.interface.dashboard_state import THEME
from systemu.runtime.dep_approvals import DepApprovalStore


def _store() -> DepApprovalStore:
    """Re-read the approval file on every render so the UI reflects CLI
    actions (``sharing_on tools deps approve``) without a restart."""
    return DepApprovalStore(Path("data") / "dep_approvals.json")


def build_pending_deps(*, compact: bool = False) -> None:
    """Render the Pending Dependencies card.

    Args:
        compact: When True, render a tighter version suitable for the
                 Overview expansion (caps the list at 5 rows, no header,
                 single CTA at the bottom).
    """
    store    = _store()
    pending  = store.list_pending()
    approved = store.list_approved()

    @ui.refreshable
    def _render() -> None:
        fresh_pending  = _store().list_pending()
        fresh_approved = _store().list_approved()
        _render_inner(fresh_pending, fresh_approved, compact=compact)

    if not pending and not approved and not compact:
        # On the Tools page give the operator a one-line explainer so
        # the empty state is intelligible.
        ui.label(
            "No pending dependencies. Tools that declare pip packages will "
            "ask for approval here before installing."
        ).style(f"color: {THEME['text_muted']}; font-style: italic; font-size: 13px;")
        return

    _render_inner(pending, approved, compact=compact, on_change=_render.refresh)
    # Re-render every ~5s so CLI-side approvals show up promptly.
    if not compact:
        ui.timer(5.0, _render.refresh)


def _render_inner(
    pending:  List[Dict],
    approved: List[Dict],
    *,
    compact: bool,
    on_change=None,
) -> None:
    if compact and not pending:
        # Compact mode in the Overview: keep the body tight when nothing
        # needs attention.  Show the count of approved deps as a hint.
        if approved:
            ui.label(f"{len(approved)} approved · 0 pending").style(
                f"color: {THEME['text_muted']}; font-size: 12px;"
            )
        else:
            ui.label("No pending dependencies.").style(
                f"color: {THEME['text_muted']}; font-style: italic; font-size: 12px;"
            )
        ui.button(
            "Manage tool dependencies →",
            on_click=lambda: ui.navigate.to("/tools"),
        ).style(
            f"background: {THEME['surface2']}; color: {THEME['text']}; "
            f"border: 1px solid {THEME['border']}; border-radius: 8px; "
            f"font-size: 12px; padding: 6px 12px; margin-top: 8px;"
        )
        return

    if pending:
        ui.label(
            f"{len(pending)} package{'s' if len(pending) != 1 else ''} waiting for approval."
        ).style(
            f"color: {THEME['warning']}; font-size: 13px; font-weight: 600; margin-bottom: 8px;"
        )

        with ui.column().classes("w-full").style(
            f"gap: 0; background: {THEME['surface']}; "
            f"border: 1px solid {THEME['border']}; border-radius: 10px; overflow: hidden;"
        ):
            for entry in (pending[:5] if compact else pending):
                _pending_row(entry, on_change=on_change)

    if approved and not compact:
        ui.label(
            f"Approved ({len(approved)})"
        ).style(
            f"color: {THEME['text_muted']}; font-size: 11px; font-weight: 700; "
            f"letter-spacing: 0.08em; text-transform: uppercase; margin-top: 14px; "
            f"margin-bottom: 6px;"
        )
        with ui.column().classes("w-full").style(
            f"gap: 0; background: {THEME['surface']}; "
            f"border: 1px solid {THEME['border']}; border-radius: 10px; overflow: hidden;"
        ):
            for entry in approved:
                _approved_row(entry, on_change=on_change)


def _pending_row(entry: Dict, *, on_change=None) -> None:
    package    = entry.get("package", "?")
    seen_tool  = entry.get("first_seen_tool") or "—"
    seen_count = int(entry.get("request_count", 0))
    with ui.row().style(
        f"width: 100%; gap: 10px; padding: 8px 12px; align-items: center; "
        f"border-bottom: 1px solid {THEME['border']};"
    ):
        ui.label("📦").style("font-size: 14px; min-width: 18px;")
        with ui.column().style("flex: 1; gap: 1px;"):
            ui.label(package).style(
                f"font-size: 13px; font-weight: 600; color: {THEME['text']};"
            )
            ui.label(
                f"requested by '{seen_tool}' · seen {seen_count}×"
            ).style(f"font-size: 11px; color: {THEME['text_muted']};")
        ui.button(
            "Approve",
            on_click=lambda p=package, t=seen_tool: _on_approve(p, t, on_change),
        ).style(
            f"background: {THEME['success']}; color: white; border-radius: 6px; "
            f"font-size: 11px; font-weight: 600; padding: 4px 10px;"
        )


def _approved_row(entry: Dict, *, on_change=None) -> None:
    package     = entry.get("package", "?")
    approved_at = entry.get("approved_at") or "—"
    by          = entry.get("approved_by") or "—"
    with ui.row().style(
        f"width: 100%; gap: 10px; padding: 6px 12px; align-items: center; "
        f"border-bottom: 1px solid {THEME['border']};"
    ):
        ui.label("✅").style("font-size: 13px; min-width: 18px;")
        with ui.column().style("flex: 1; gap: 1px;"):
            ui.label(package).style(
                f"font-size: 12px; font-weight: 600; color: {THEME['text']};"
            )
            ui.label(f"approved {approved_at} by {by}").style(
                f"font-size: 10px; color: {THEME['text_muted']};"
            )
        ui.button(
            "Revoke",
            on_click=lambda p=package: _on_revoke(p, on_change),
        ).style(
            f"background: transparent; color: {THEME['danger']}; "
            f"border: 1px solid {THEME['danger']}; border-radius: 6px; "
            f"font-size: 10px; font-weight: 600; padding: 3px 8px;"
        )


def _on_approve(package: str, tool_name: str, on_change=None) -> None:
    store = _store()
    if store.approve(package, approved_by="operator (dashboard)", tool_name=tool_name):
        ui.notify(f"Approved {package}", type="positive")
    else:
        ui.notify(f"{package} was already approved", type="info")
    if on_change:
        try:
            on_change()
        except Exception:
            pass


def _on_revoke(package: str, on_change=None) -> None:
    store = _store()
    if store.revoke(package):
        ui.notify(f"Revoked {package}", type="warning")
    else:
        ui.notify(f"{package} was not approved", type="info")
    if on_change:
        try:
            on_change()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Small helpers used by other pages to query the store without importing
# the full UI module.

def pending_count() -> int:
    """Cheap lookup for the Overview badge / route highlight."""
    try:
        return len(_store().list_pending())
    except Exception:
        return 0
