"""T1b — the read-only OnTheTable page (`/table`), spec UNIFIED-v2 §5.10.c.

Renders the projected inventory (`table_reconciler.project`) as a zoned board:
Services & Accounts · Tools & Capabilities · Files & Data · Keys · Preferences ·
Devices. READ-ONLY in this release — no add/remove/consult (those are T2/T3). The
zoning + summary logic is pure and unit-tested here; the nicegui rendering itself
is operator-verifiable.

All values crossing into nicegui are plain strings (name/detail/status/kind) — no
functions or non-serializable objects — per the v0.9.45 serialization-crash rule.
"""
from __future__ import annotations

from typing import Any, Dict, List

from nicegui import ui

from systemu.interface.dashboard_state import AppState

# zone label -> the item kinds it holds (ordered for display)
_ZONE_ORDER: List[tuple] = [
    ("Services & Accounts", {"service", "mcp_server"}),
    ("Tools & Capabilities", {"tool"}),
    ("Files & Data", {"data_root"}),
    ("Keys", {"credential_ref"}),
    ("Preferences", {"preference"}),
    ("Devices", {"device"}),
]
_OTHER = "Other"

# status -> Quasar color token (no raw hex; re-theming lives in design tokens).
_STATUS_COLOR = {
    "ready": "positive", "configuring": "warning", "stale": "negative",
    "broken": "negative", "declared": "grey", "suggested": "info",
}

# a compact per-kind summary label
_KIND_SUMMARY = [
    ("mcp_server", "service", "services"),
    ("tool", "tool", "tools"),
    ("data_root", "folder", "folders"),
    ("credential_ref", "key", "keys"),
]


def _kind(it: Any) -> str:
    return getattr(it, "kind", "") or (it.get("kind", "") if isinstance(it, dict) else "")


def zone_of(kind: str) -> str:
    for label, kinds in _ZONE_ORDER:
        if kind in kinds:
            return label
    return _OTHER


def group_into_zones(items: List[Any]) -> Dict[str, List[Any]]:
    """Group TableItems into display zones; drop empty zones, preserve order."""
    zones: Dict[str, List[Any]] = {label: [] for label, _ in _ZONE_ORDER}
    zones[_OTHER] = []
    for it in items:
        zones[zone_of(_kind(it))].append(it)
    return {k: v for k, v in zones.items() if v}


def summarize(items: List[Any]) -> str:
    """A one-line count summary, e.g. '2 services · 12 tools · 3 keys'."""
    counts: Dict[str, int] = {}
    for it in items:
        counts[_kind(it)] = counts.get(_kind(it), 0) + 1
    parts = []
    for kind, singular, plural in _KIND_SUMMARY:
        n = counts.get(kind, 0)
        if n:
            parts.append(f"{n} {singular if n == 1 else plural}")
    return " · ".join(parts) if parts else "nothing yet"


def _project(vault) -> List[Any]:
    if vault is None:
        return []
    try:
        from systemu.runtime import table_reconciler
        return table_reconciler.project(vault)
    except Exception:
        return []


def _render_card(it: Any) -> None:
    status = getattr(it, "status", "") or ""
    color = _STATUS_COLOR.get(status, "grey")
    with ui.card().classes("s-card").style("min-width: 220px; max-width: 340px;"):
        with ui.row().classes("items-center no-wrap").style("gap: 6px;"):
            ui.icon("circle", size="xs").props(f"color={color}")
            ui.label(getattr(it, "name", "") or "").classes("text-weight-medium ellipsis")
        detail = getattr(it, "detail", "") or ""
        if detail:
            ui.label(detail).classes("s-muted").style("font-size: 12px;")
        with ui.row().classes("items-center").style("gap: 4px;"):
            if status:
                ui.badge(status).props("outline")
            ui.badge(_kind(it) or "item").props("outline color=grey")
            prov = getattr(it, "provenance", "") or ""
            if prov and prov != "migrated":
                ui.badge(prov).props("outline color=blue")


def build_table_page() -> None:
    state = AppState.get()
    vault = getattr(state, "vault", None)
    items = _project(vault)

    with ui.row().classes("w-full items-center justify-between q-mb-md"):
        with ui.column().classes("q-gutter-none"):
            ui.label("On the table").classes("text-h6")
            ui.label(
                "Everything systemu can see it has — services, tools, files, keys. "
                "Read-only for now."
            ).classes("s-muted")
        ui.label(summarize(items)).classes("s-muted")

    zones = group_into_zones(items)
    if not zones:
        with ui.card().classes("s-card"):
            ui.label("Your table is empty.").classes("text-subtitle1")
            ui.label(
                "Connect a service or forge a tool and it'll appear here."
            ).classes("s-muted")
        return

    for label, _kinds in list(_ZONE_ORDER) + [(_OTHER, set())]:
        zitems = zones.get(label)
        if not zitems:
            continue
        ui.label(f"{label} ({len(zitems)})").classes("text-subtitle1 q-mt-md q-mb-xs")
        with ui.row().classes("w-full wrap").style("gap: 8px;"):
            for it in zitems:
                _render_card(it)
