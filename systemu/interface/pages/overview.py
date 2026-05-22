"""Overview page — single-pane operator console.

Layout (top to bottom):

  1. Five stat cards (Scrolls / Shadows / Tools / Skills / Evolutions)
  2. Workflow Pipeline card (NEW — shows live in-flight workflows)
  3. Recent Activity feed + Quick Actions (existing)
  4. Expansion cards (Learning Curves / Memory Status / Skills Snapshot / Pending Tools)

Full-route pages (/flywheel, /memory, /skills, /tools) keep their
existing implementations — external bookmarks still resolve.  This
page is the new default discovery surface.
"""

from __future__ import annotations

from nicegui import ui

from systemu.interface.dashboard_state import AppState, THEME


def build_overview_page() -> None:
    """Render the overview / dashboard home page."""
    state = AppState.get()
    vault = state.vault

    # ── Load counts ────────────────────────────────────────────────────
    scrolls    = vault.load_index("scrolls")
    shadows    = vault.load_index("shadow_army")
    tools      = vault.load_index("tools")
    skills     = vault.load_index("skills")
    evolutions = vault.load_index("evolutions")
    activities = vault.load_index("activities")

    pending_evolutions = [e for e in evolutions if e.get("status") == "proposed"]
    pending_scrolls    = [s for s in scrolls    if s.get("status") == "pending_approval"]

    # ── Stat cards ─────────────────────────────────────────────────────
    with ui.row().classes("w-full gap-4 flex-wrap"):
        _stat_card("📜", "Scrolls",    len(scrolls),
                   THEME["primary"], f"{len(pending_scrolls)} pending")
        _stat_card("👥", "Shadows",    len(shadows),
                   THEME["info"], f"{len([s for s in shadows if s.get('status')=='awakened'])} active")
        _stat_card("🔧", "Tools",      len(tools),
                   THEME["success"], f"{len([t for t in tools if t.get('status')=='forged'])} forged")
        _stat_card("🧠", "Skills",     len(skills),
                   "#a78bfa", "across all shadows")
        _stat_card("🧬", "Evolutions", len(pending_evolutions),
                   THEME["warning"], "pending review")

    ui.separator().style(f"background:{THEME['border']}; margin: 24px 0;")

    # ── Workflow pipeline (new — item 5) ──────────────────────────────
    try:
        from systemu.interface.components.workflow_pipeline import build_workflow_pipeline
        build_workflow_pipeline()
    except Exception as exc:
        # Tracker may not be initialised in some test contexts — degrade gracefully.
        ui.label(f"Workflow tracker unavailable: {exc}").style(
            f"color: {THEME['text_muted']}; font-style: italic;"
        )

    ui.separator().style(f"background:{THEME['border']}; margin: 24px 0;")

    # ── Activity feed + Quick actions ──────────────────────────────────
    with ui.row().classes("w-full gap-6"):
        # Activity feed (left)
        with ui.column().classes("flex-1"):
            ui.label("Recent Activity").style(
                f"font-size: 16px; font-weight: 700; color: {THEME['text']}; "
                f"margin-bottom: 12px;"
            )
            recent = _build_activity_feed(scrolls, activities, shadows, evolutions)
            if not recent:
                ui.label("No recent activity — record a session to get started.").style(
                    f"color: {THEME['text_muted']}; font-style: italic;"
                )
            else:
                with ui.column().style(
                    f"background: {THEME['surface']}; border: 1px solid {THEME['border']}; "
                    f"border-radius: 12px; overflow: hidden; width: 100%;"
                ):
                    for item in recent:
                        _activity_row(item)

        # Quick actions (right)
        with ui.column().style("min-width: 220px; gap: 12px;"):
            ui.label("Quick Actions").style(
                f"font-size: 16px; font-weight: 700; color: {THEME['text']}; margin-bottom: 4px;"
            )
            ui.button(
                "⚡ Record New Session",
                on_click=lambda: _trigger_record_dialog(),
            ).style(
                f"background: {THEME['primary']}; color: white; border-radius: 8px; width: 100%;"
            )
            ui.button(
                "🧬 Run Evolution Check",
                on_click=_run_evolution,
            ).style(
                f"background: {THEME['surface2']}; color: {THEME['text']}; "
                f"border: 1px solid {THEME['border']}; border-radius: 8px; width: 100%;"
            )
            ui.button(
                "🔔 View Notifications",
                on_click=lambda: ui.navigate.to("/notifications"),
            ).style(
                f"background: {THEME['surface2']}; color: {THEME['text']}; "
                f"border: 1px solid {THEME['border']}; border-radius: 8px; width: 100%;"
            )
            ui.button(
                "🔧 Forge Proposed Tools",
                on_click=lambda: ui.navigate.to("/tools"),
            ).style(
                f"background: {THEME['surface2']}; color: {THEME['text']}; "
                f"border: 1px solid {THEME['border']}; border-radius: 8px; width: 100%;"
            )
            ui.button(
                "Force Restart Workers",
                on_click=_force_restart_workers,
            ).style(
                f"background: {THEME['surface2']}; color: {THEME['warning']}; "
                f"border: 1px solid {THEME['warning']}; border-radius: 8px; width: 100%;"
            ).tooltip("Restart Supervisor threads — use when a shadow appears stuck")

    ui.separator().style(f"background:{THEME['border']}; margin: 24px 0;")

    # ── Expansion cards ────────────────────────────────────────────────
    _build_expansions()


def _build_expansions() -> None:
    """Render the four collapsed-by-default Overview expansion cards.

    Lazy-load each card body on expansion open — keeps the initial
    Overview render cheap.
    """
    ui.label("More").style(
        f"font-size: 13px; color: {THEME['text_muted']}; font-weight: 700; "
        f"letter-spacing: 0.08em; text-transform: uppercase; margin-bottom: 8px;"
    )

    _expansion("📈", "Learning Curves",
               "Compact view of Shadow execution metrics and the data flywheel.",
               _load_learning_curves)

    _expansion("🧠", "Memory Status",
               "Per-Shadow memory buffer health and consolidation readiness.",
               _load_memory_status)

    _expansion("🧩", "Skills Snapshot",
               "Top skills by usage, with a link to the full skill registry.",
               _load_skills_snapshot)

    _expansion("🔧", "Pending Tools",
               "Tools the Tool Forge has proposed but you haven't enabled yet.",
               _load_pending_tools)

    _expansion("📦", "Tool Dependencies",
               "Pip packages awaiting your approval before tools may install them.",
               _load_pending_deps)


def _expansion(icon: str, title: str, subtitle: str, body_loader) -> None:
    """Wrap a lazy-loaded expansion card.  body_loader is called on first open."""
    with ui.expansion(
        title,
        icon="",  # we render our own glyph
    ).classes("w-full").style(
        f"background: {THEME['surface']}; border: 1px solid {THEME['border']}; "
        f"border-radius: 12px; margin-bottom: 12px;"
    ) as exp:
        with exp.add_slot("header"):
            with ui.row().style("align-items: center; gap: 10px;"):
                ui.label(icon).style("font-size: 18px;")
                with ui.column().style("gap: 0;"):
                    ui.label(title).style(
                        f"font-size: 14px; font-weight: 700; color: {THEME['text']};"
                    )
                    ui.label(subtitle).style(
                        f"font-size: 11px; color: {THEME['text_muted']};"
                    )

        # Lazy body: build once on first open, then keep.
        body_state = {"built": False}
        body_container = ui.column().classes("w-full").style("padding: 6px 12px 12px;")

        def _build_body() -> None:
            if body_state["built"]:
                return
            with body_container:
                try:
                    body_loader()
                except Exception as exc:
                    ui.label(f"Error loading: {exc}").style(
                        f"color: {THEME['danger']}; font-size: 12px;"
                    )
            body_state["built"] = True

        exp.on("update:model-value", lambda _e: _build_body())


# ── Body loaders ───────────────────────────────────────────────────────

def _load_learning_curves() -> None:
    from systemu.interface.components.learning_curves import build_learning_curves
    build_learning_curves()


def _load_memory_status() -> None:
    from systemu.interface.components.memory_status import build_memory_status
    build_memory_status()


def _load_skills_snapshot() -> None:
    from systemu.interface.components.skills_snapshot import build_skills_snapshot
    build_skills_snapshot()


def _load_pending_tools() -> None:
    from systemu.interface.components.pending_tools import build_pending_tools
    build_pending_tools()


def _load_pending_deps() -> None:
    from systemu.interface.components.pending_deps import build_pending_deps
    build_pending_deps(compact=True)


# ── Stat card + activity feed + actions (unchanged from original) ──────

def _force_restart_workers() -> None:
    """Gracefully restart the Supervisor background threads."""
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


def _stat_card(icon: str, label: str, value: int, color: str, subtitle: str) -> None:
    with ui.column().style(
        f"background: {THEME['surface']}; border: 1px solid {THEME['border']}; "
        f"border-radius: 12px; padding: 20px 24px; min-width: 150px; "
        f"align-items: center; gap: 4px; flex: 1;"
    ):
        ui.label(icon).style("font-size: 28px;")
        ui.label(str(value)).style(
            f"font-size: 36px; font-weight: 800; color: {color};"
        )
        ui.label(label).style(
            f"font-size: 14px; font-weight: 600; color: {THEME['text']};"
        )
        ui.label(subtitle).style(
            f"font-size: 11px; color: {THEME['text_muted']};"
        )


def _activity_row(item) -> None:
    with ui.row().style(
        f"padding: 12px 16px; border-bottom: 1px solid {THEME['border']}; "
        f"align-items: center; gap: 12px;"
    ):
        ui.label(item["icon"]).style("font-size: 20px; min-width: 28px;")
        with ui.column().style("gap: 2px;"):
            ui.label(item["title"]).style(
                f"font-size: 14px; font-weight: 600; color: {THEME['text']};"
            )
            ui.label(item["subtitle"]).style(
                f"font-size: 12px; color: {THEME['text_muted']};"
            )


def _build_activity_feed(scrolls, activities, shadows, evolutions) -> list:
    """Build a unified recent-activity list, sorted by recency heuristic."""
    items = []
    for s in scrolls[-5:]:
        items.append({
            "icon": "📜", "title": s.get("name", s["id"]),
            "subtitle": f"Scroll · {s.get('status', '?')}",
            "sort": s.get("created_at", ""),
        })
    for a in activities[-3:]:
        items.append({
            "icon": "📋", "title": a.get("name", a["id"]),
            "subtitle": f"Activity · {a.get('status', '?')}",
            "sort": a.get("created_at", ""),
        })
    for sh in shadows[-3:]:
        items.append({
            "icon": "👤", "title": sh.get("name", sh["id"]),
            "subtitle": f"Shadow · {sh.get('status', '?')}",
            "sort": sh.get("created_at", ""),
        })
    for e in [ev for ev in evolutions if ev.get("status") == "proposed"][-3:]:
        items.append({
            "icon": "🧬", "title": e.get("description", e["id"])[:60],
            "subtitle": f"Evolution · {e.get('evolution_type', '?')}",
            "sort": e.get("created_at", ""),
        })
    items.sort(key=lambda x: x["sort"], reverse=True)
    return items[:10]


def _trigger_record_dialog() -> None:
    """Open the record session dialog (reuses dashboard.py handler)."""
    try:
        from systemu.interface.dashboard import _show_record_dialog
        _show_record_dialog()
    except Exception as exc:
        ui.notify(f"Could not open record dialog: {exc}", type="negative")


def _run_evolution() -> None:
    """Trigger the evolution engine as a background job to avoid blocking the UI."""
    from systemu.interface.jobs import JobManager
    import sys

    state = AppState.get()
    jm = JobManager.get()
    cwd = state.project_root
    cmd = [sys.executable, "-m", "sharing_on", "evolve", "run"]
    jm.start_job("Evolution Check", "evolve", cmd, cwd)
    ui.notify("Evolution check dispatched as background job.", type="positive")
