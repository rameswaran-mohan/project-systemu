"""Home spine (Phase 6) — the at-a-glance command center.

Route: ``/`` (the Home spine).  Spec §5: "At-a-glance: what's running, what
needs me.  Cards are LINKS, not re-renders of other pages."

This page is a DASHBOARD OF LINKS to the six spines plus two at-a-glance
summaries — it never re-renders another page's content in place:

  1. "What needs you" — a SUMMARY of pending operator gates (count + the top
     2-3 gate titles), each linking to ``/inbox``.  The full resolvable cards
     live in the Inbox page + the persistent right rail; Home only glances.
  2. "What's running" — the shared ``workflow_pipeline`` card (capture→…→done
     counts + active jobs), linking to ``/work``.
  3. Spine quick-links — the clickable stat tiles (``_stat_card``), each a
     link to its spine.
  4. "Recent activity" — a short list of recent workflows, each linking to
     ``/workflow/{id}``.  NOT a re-render of the Work list.
  5. "Pending approvals" — the tool/dep approval queue (a Home-owned surface,
     not a foreign page) and the header quick-actions are kept.

What was REMOVED (the spec gap this rebuild closes):
  * the "Pending Actions" pane that re-rendered the unified decision cards (a
    duplicate decision surface; its old link target ``/insights?tab=actions``
    no longer even exists) → replaced by the "What needs you" summary → /inbox.
  * the "More" expansions that re-rendered Learning Curves / Memory Status /
    Skills Snapshot (duplicates of Insights / Shadows) → replaced by a single
    "View analytics → /insights" + "Shadows memory → /shadows" link row.

The pure summary models (``home_needs_you_summary`` /
``home_recent_workflows``) are unit-tested without a NiceGUI runtime; the
NiceGUI builders compose them with design-system token classes only (lint
``test_ui_style_no_new_violations`` stays at 0 new).
"""
from __future__ import annotations

from typing import Any, Dict, List

from systemu.interface.nav_helpers import tile_nav_target

# ``_UNTITLED`` keeps an option-less / malformed gate readable in the glance
# rather than rendering a blank line.
_UNTITLED = "(untitled gate)"


# ─────────────────────────────────────────────────────────────────────────────
#  Pure summary models (unit-tested in tests/test_home_page.py)
# ─────────────────────────────────────────────────────────────────────────────


def home_needs_you_summary(descriptors, *, top_n: int = 3) -> Dict[str, Any]:
    """SUMMARY of pending operator gates for the Home glance card (pure).

    ``descriptors`` is exactly what ``InboxQueue.list_descriptors()`` returns:
    a list of ``(decision_id, GateDescriptor)`` tuples.  Returns the total
    ``count``, the leading ``top_n`` gate ``titles`` (so the operator sees what
    is waiting without leaving Home), and the ``link`` to the full Inbox where
    gates are actually resolved.  Empty input → ``count 0 / top []``.

    Defensive: a malformed row (no ``.title``) yields the ``_UNTITLED``
    placeholder rather than crashing the glance.
    """
    rows = list(descriptors or [])
    titles: List[str] = []
    for _dec_id, d in rows[:top_n]:
        title = (getattr(d, "title", "") or "").strip()
        titles.append(title or _UNTITLED)
    return {"count": len(rows), "top": titles, "link": "/inbox"}


def home_recent_workflows(snapshots, *, limit: int = 5) -> List[Dict[str, Any]]:
    """Recent workflows for the Home "Recent activity" list (pure, testable).

    Maps ``WorkflowSnapshot`` objects → lightweight row dicts (newest first),
    each carrying a LINK to ``/workflow/{id}`` — a glance that points at the
    Work spine, NOT a re-render of the Work list.  Missing ``updated_at`` sorts
    last (treated as the empty string).
    """
    snaps = list(snapshots or [])
    snaps.sort(key=lambda s: getattr(s, "updated_at", "") or "", reverse=True)
    rows: List[Dict[str, Any]] = []
    for s in snaps[:limit]:
        wid = getattr(s, "workflow_id", "")
        rows.append({
            "workflow_id": wid,
            "title": getattr(s, "title", "") or wid,
            "status": getattr(s, "status", ""),
            "stage": getattr(s, "stage", ""),
            "link": f"/workflow/{wid}",
        })
    return rows


def _pending_approvals_count(vault) -> int:
    """Count items awaiting operator approval — proposed tools + pending deps.

    Pure + robust: never raises (returns 0 on any error). In file mode the
    DepApprovalStore reads ``data/dep_approvals.json``.
    """
    try:
        tools = vault.load_index("tools") or []
    except Exception:
        tools = []
    proposed = sum(1 for t in tools if t.get("status") == "proposed")
    try:
        from pathlib import Path
        from systemu.runtime.dep_approvals import DepApprovalStore
        deps = len(DepApprovalStore(Path("data") / "dep_approvals.json").list_pending())
    except Exception:
        deps = 0
    return proposed + deps


# ─────────────────────────────────────────────────────────────────────────────
#  Data loaders (vault / tracker → pure-model inputs).  Defensive: any failure
#  yields an empty result so the Home shell never breaks.
# ─────────────────────────────────────────────────────────────────────────────


def _load_needs_you_summary(vault) -> Dict[str, Any]:
    try:
        from systemu.interface.command.inbox import InboxQueue
        descriptors = InboxQueue(vault).list_descriptors()
    except Exception:
        descriptors = []
    return home_needs_you_summary(descriptors)


def _load_recent_workflows() -> List[Dict[str, Any]]:
    try:
        from systemu.runtime.workflow_tracker import WorkflowTracker
        tracker = WorkflowTracker.get()
        tracker.refresh_from_vault()
        return home_recent_workflows(tracker.list_all())
    except Exception:
        return []


# ─────────────────────────────────────────────────────────────────────────────
#  Page
# ─────────────────────────────────────────────────────────────────────────────


def build_home_page() -> None:
    """Render the Home spine: a dashboard of LINKS + two at-a-glance summaries.

    Token classes / plain-string styles only (lint stays at 0 new). The order
    follows the operator's question: what needs me → what's running → where do
    I go → what just happened → what's awaiting approval.
    """
    from nicegui import ui

    from systemu.interface.dashboard_state import AppState
    state = AppState.get()
    vault = state.vault

    scrolls    = vault.load_index("scrolls")
    shadows    = vault.load_index("shadow_army")
    tools      = vault.load_index("tools")
    skills     = vault.load_index("skills")
    evolutions = vault.load_index("evolutions")
    activities = vault.load_index("activities")

    pending_evolutions = [e for e in evolutions if e.get("status") == "proposed"]
    pending_scrolls    = [s for s in scrolls    if s.get("status") == "pending_approval"]

    # ── Header + quick actions ─────────────────────────────────────────
    with ui.row().classes("w-full items-center justify-between q-mb-md"):
        ui.label("🏠 Home").classes("s-page-title")
        with ui.row().classes("items-center q-gutter-sm"):
            ui.button("⚡ Record", on_click=_trigger_record_dialog) \
                .props("flat no-caps").classes("s-btn s-btn--primary")
            ui.button("🧬 Evolve", on_click=_run_evolution) \
                .props("flat no-caps").classes("s-btn s-btn--ghost")
            ui.button("🔔 Notifications", on_click=lambda: ui.navigate.to("/notifications")) \
                .props("flat no-caps").classes("s-btn s-btn--ghost")
            ui.button("🔧 Forge", on_click=lambda: ui.navigate.to("/tools")) \
                .props("flat no-caps").classes("s-btn s-btn--ghost")
            ui.button("↻ Restart Workers", on_click=_force_restart_workers) \
                .props("flat no-caps").classes("s-btn s-btn--warn") \
                .tooltip("Restart Supervisor threads — use when a shadow appears stuck")

    # ── Two at-a-glance summaries: needs-you + what's running ───────────
    with ui.row().classes("w-full q-gutter-md q-mb-md").style("flex-wrap: wrap;"):
        with ui.column().style("flex: 1; min-width: 320px;"):
            _build_needs_you_card(vault)
        with ui.column().style("flex: 1; min-width: 320px;"):
            _build_whats_running_card()

    # ── Spine quick-links (clickable stat tiles → each spine) ──────────
    ui.label("Spines").classes("s-section-head q-mt-sm")
    with ui.row().classes("w-full q-gutter-sm q-mb-md").style("flex-wrap: wrap;"):
        _stat_card("📜", "Scrolls", len(scrolls),
                   f"{len(pending_scrolls)} pending", nav_target=tile_nav_target("Scrolls"))
        _stat_card("👥", "Shadows", len(shadows),
                   f"{len([s for s in shadows if s.get('status')=='awakened'])} active",
                   nav_target=tile_nav_target("Shadows"))
        _stat_card("🔧", "Tools", len(tools),
                   f"{len([t for t in tools if t.get('status')=='forged'])} forged",
                   nav_target=tile_nav_target("Tools"))
        _stat_card("🧠", "Skills", len(skills),
                   "across all shadows", nav_target=tile_nav_target("Skills"))
        _stat_card("📋", "Activities", len(activities),
                   f"{len([a for a in activities if a.get('status')=='unassigned'])} unassigned",
                   nav_target=tile_nav_target("Activities"))
        _stat_card("🧬", "Evolutions", len(pending_evolutions),
                   "pending review", nav_target=tile_nav_target("Evolutions"))

    # ── Recent activity (links to /workflow/{id}) ──────────────────────
    _build_recent_activity_card()

    # ── Pending approvals (Home-owned surface — tools + deps) ──────────
    n_appr = _pending_approvals_count(vault)
    with ui.expansion(f"Pending Approvals ({n_appr})", value=n_appr > 0).classes("w-full q-mb-sm s-card"):
        with ui.column().classes("w-full").style("padding: 8px 4px; gap: 12px;"):
            from systemu.interface.components.pending_tools import build_pending_tools
            from systemu.interface.components.pending_deps import build_pending_deps
            build_pending_tools()
            build_pending_deps(compact=True)

    # ── Analytics / memory deep-links (replaces the old "More" re-renders) ──
    with ui.row().classes("w-full items-center q-gutter-md q-mt-sm"):
        ui.label("Go deeper:").classes("s-muted")
        ui.link("📈 View analytics", "/insights").classes("s-cell s-cell--bold") \
            .style("text-decoration: none;")
        ui.link("🧠 Shadows memory", "/shadows").classes("s-cell s-cell--bold") \
            .style("text-decoration: none;")


# Back-compat alias: the route + importers historically reference
# build_console_page; Home is its content now (the page was retitled in Phase 6
# and rebuilt as an at-a-glance spine here). Keep the old name importable.
build_console_page = build_home_page


# ─────────────────────────────────────────────────────────────────────────────
#  Glance cards
# ─────────────────────────────────────────────────────────────────────────────


def _build_needs_you_card(vault) -> None:
    """"What needs you" — a SUMMARY (count + top gate titles) linking to /inbox.

    NOT the full resolvable cards (those live in /inbox + the right rail). Live
    via a 2s poll, mirroring the rail's cadence."""
    from nicegui import ui
    from systemu.interface.design.primitives import card
    from systemu.interface.ui_helpers import safe_timer

    with card(classes="w-full"):
        with ui.row().classes("w-full items-center justify-between q-mb-sm"):
            ui.label("📥 What needs you").classes("s-cell s-cell--bold")
            ui.link("Open Inbox →", "/inbox").classes("s-muted").style("text-decoration: none;")

        @ui.refreshable
        def _summary() -> None:
            s = _load_needs_you_summary(vault)
            if s["count"] == 0:
                ui.label("Nothing waiting on you.").classes("s-muted")
                return
            ui.label(f"{s['count']} pending").classes("s-pill s-pill--warn q-mb-sm")
            for title in s["top"]:
                ui.link(f"• {title}", s["link"]).classes("s-cell") \
                    .style("text-decoration: none; display: block;")
            extra = s["count"] - len(s["top"])
            if extra > 0:
                ui.link(f"+{extra} more in Inbox →", s["link"]).classes("s-muted") \
                    .style("text-decoration: none;")

        _summary()
        safe_timer(2.0, _summary.refresh)


def _build_whats_running_card() -> None:
    """"What's running" — the shared workflow_pipeline card + active jobs count,
    linking to /work. Reuses components/workflow_pipeline (no re-implementation)."""
    from nicegui import ui
    from systemu.interface.design.primitives import card
    from systemu.interface.components.workflow_pipeline import build_workflow_pipeline

    with card(classes="w-full"):
        with ui.row().classes("w-full items-center justify-between q-mb-sm"):
            ui.label("⚙️ What's running").classes("s-cell s-cell--bold")
            ui.link("Open Work →", "/work").classes("s-muted").style("text-decoration: none;")
        # Compact pipeline: stage chips only (the active list is the Work page).
        build_workflow_pipeline(compact=True)


def _build_recent_activity_card() -> None:
    """"Recent activity" — recent workflows, each a LINK to /workflow/{id}.
    NOT a re-render of the Work list (it shows ≤5 rows, title + stage only)."""
    from nicegui import ui
    from systemu.interface.design.primitives import card
    from systemu.interface.ui_helpers import safe_timer

    ui.label("Recent activity").classes("s-section-head q-mt-sm")
    with card(classes="w-full q-mb-sm"):
        @ui.refreshable
        def _list() -> None:
            rows = _load_recent_workflows()
            if not rows:
                ui.label("No recent workflows.").classes("s-muted")
                return
            for row in rows:
                with ui.row().classes("w-full items-center q-gutter-sm"):
                    ui.link(row["title"], row["link"]).classes("s-cell s-cell--bold col-grow") \
                        .style("text-decoration: none;")
                    ui.label(row["stage"]).classes("s-pill s-pill--accent")
                    ui.label(row["status"]).classes("s-pill s-pill--muted")

        _list()
        safe_timer(2.0, _list.refresh)


# ─────────────────────────────────────────────────────────────────────────────
#  Spine stat tile (a LINK to its spine — token-class clean)
# ─────────────────────────────────────────────────────────────────────────────


def _stat_card(icon: str, label: str, value: int, subtitle: str,
               nav_target: str | None = None) -> None:
    """Clickable stat tile that navigates to a spine list page."""
    from nicegui import ui
    from systemu.interface.design.primitives import card

    tile = card()
    tile.style("min-width: 140px; flex: 1; cursor: pointer; align-items: center;"
               if nav_target else "min-width: 140px; flex: 1; align-items: center;")
    if nav_target:
        tile.on("click", lambda t=nav_target: ui.navigate.to(t))
    with tile:
        ui.label(icon).style("font-size: 26px;")
        ui.label(str(value)).classes("s-page-title")
        ui.label(label).classes("s-cell s-cell--bold")
        ui.label(subtitle).classes("s-muted")


# ─────────────────────────────────────────────────────────────────────────────
#  Quick-action handlers (carried over — header buttons must keep working)
# ─────────────────────────────────────────────────────────────────────────────


def _force_restart_workers() -> None:
    """Gracefully restart the Supervisor background threads."""
    from nicegui import ui
    try:
        from systemu.runtime.supervisor import Supervisor
        import time as _time
        sup = Supervisor.get()
        sup._shutdown_event.set()
        _time.sleep(0.5)
        sup._shutdown_event.clear()
        sup.start()
        ui.notify("Workers restarted successfully.", type="positive")
    except Exception as exc:
        ui.notify(f"Restart failed: {exc}", type="negative")


def _trigger_record_dialog() -> None:
    """Open the record session dialog (reuses dashboard.py handler)."""
    from nicegui import ui
    try:
        from systemu.interface.dashboard import _show_record_dialog
        _show_record_dialog()
    except Exception as exc:
        ui.notify(f"Could not open record dialog: {exc}", type="negative")


def _run_evolution() -> None:
    """Trigger the evolution engine as a background job to avoid blocking the UI."""
    from nicegui import ui
    from systemu.interface.command.dispatch import dispatch
    from systemu.interface.dashboard_state import AppState

    state = AppState.get()
    cwd = state.project_root
    dispatch("evolve run", [], cwd=cwd, stream=True, job_type="evolve",
             dedup_key="evolve:run")
    ui.notify("Evolution check dispatched as background job.", type="positive")
