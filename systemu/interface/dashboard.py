"""Systemu NiceGUI Web Dashboard — main entry point.

Starts a NiceGUI app on localhost:<port> (default 8765).
Provides a persistent sidebar navigation and six page routes:
  /              Overview (stat cards + activity feed)
  /scrolls       Scroll list + detail
  /army          Shadow Army card grid
  /tools         Tool registry
  /evolutions    Evolution proposals + history
  /settings      LLM tier config + auto-approve

Called by:
  - `sharing_on daemon start` (background mode)
  - `systemu/scheduler/daemon.py` (subprocess entry)
  - `_run_dashboard()` in this module (foreground debug)

Thread-safety: NiceGUI runs in its own thread. Vault reads are safe
because vault.py uses atomic file writes. LLM calls triggered from buttons
run in the NiceGUI event loop — no async issues.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
#  Security banner helper (v0.6.9)
# ─────────────────────────────────────────────────────────────────────────────

def _autoforge_banner_message() -> "str | None":
    """return a persistent security banner message when auto-forge
    mode is on.  Returns None when disabled so callers can skip rendering.

    SYSTEMU_AUTO_FORGE_TOOLS=true bypasses all three tool security gates
    (spec review, code review, enable toggle).  This banner ensures the
    operator can't miss the warning by scrolling past the stdout log."""
    import os
    if (os.environ.get("SYSTEMU_AUTO_FORGE_TOOLS") or "").lower() == "true":
        return (
            "AUTO_FORGE_TOOLS is enabled. All three tool security gates "
            "(spec review, code review, enable toggle) are bypassed. "
            "LLM-generated code is saved + enabled without human review. "
            "DEV/TEST ONLY — disable via SYSTEMU_AUTO_FORGE_TOOLS=false in .env."
        )
    return None


# ─────────────────────────────────────────────────────────────────────────────
#  Nav layout helper
# ─────────────────────────────────────────────────────────────────────────────

NAV_ITEMS = [
    ("/",               "🏠",  "Overview"),
    ("/chat",           "💬",  "Chat"),
    ("/systemu-chat",   "🤖",  "Systemu Chat"),
    ("/scrolls",        "📜",  "Scrolls"),
    ("/army",           "👥",  "Shadow Army"),
    ("/activities",     "📋",  "Activities"),
    ("/tools",          "🔧",  "Tools"),
    ("/skills",         "🧠",  "Skills"),
    ("/memory",         "💡",  "Memory"),
    ("/flywheel",       "⚙️",  "Flywheel"),
    ("/workshop",       "🛠️",  "Workshop"),
    ("/evolutions",     "🧬",  "Evolutions"),
    ("/notifications",  "🔔",  "Notifications"),
    ("/settings",       "⚙️",  "Settings"),
]


def _build_layout(page_title: str, current_path: str):
    """Return a NiceGUI context manager that renders the sidebar + header."""
    from nicegui import ui
    from systemu.interface.dashboard_state import THEME, GLOBAL_CSS

    ui.add_css(GLOBAL_CSS)
    # Google Font
    ui.add_head_html(
        '<link rel="preconnect" href="https://fonts.googleapis.com">'
        '<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">'
    )

    # Root wrapper
    with ui.row().style(
        f"width: 100vw; min-height: 100vh; background: {THEME['bg']}; gap: 0;"
    ):
        # Hamburger button — visible only on narrow viewports.  Toggles
        # the .s-sidebar-open class on <body> which expands the sidebar.
        ui.html(
            '<button class="s-sidebar-toggle" '
            'aria-label="Toggle navigation" '
            'onclick="document.body.classList.toggle(\'s-sidebar-open\')">☰</button>'
        )
        # Backdrop — sits behind the expanded sidebar.  Clicking it
        # dismisses the sidebar.  Visible only when sidebar is open on
        # narrow viewports (handled via CSS).
        ui.html(
            '<div class="s-sidebar-backdrop" '
            'onclick="document.body.classList.remove(\'s-sidebar-open\')"></div>'
        )

        # ── Sidebar ────────────────────────────────────────────────────────
        with ui.column().classes("s-sidebar").style(
            f"width: 220px; min-width: 220px; background: {THEME['surface']}; "
            f"border-right: 1px solid {THEME['border']}; padding: 24px 12px; gap: 4px; "
            f"height: 100vh; position: sticky; top: 0; overflow-y: auto;"
        ):
            # Logo / brand — sidebar collapses to icon-only on narrow viewports
            # (handled via the .s-sidebar / .s-sidebar-label CSS classes
            # defined in GLOBAL_CSS).
            with ui.row().classes("s-sidebar-header").style(
                "align-items: center; gap: 10px; padding: 8px 12px; margin-bottom: 16px;"
            ):
                ui.label("⚡").style("font-size: 22px;")
                ui.label("Systemu").classes("s-sidebar-label").style(
                    f"font-size: 18px; font-weight: 800; color: {THEME['text']};"
                )

            # Nav links — the text part carries .s-sidebar-label so it hides
            # cleanly when the sidebar collapses on narrow viewports.
            for path, icon, label in NAV_ITEMS:
                is_active = current_path == path or (path != "/" and current_path.startswith(path))
                bg = f"color-mix(in srgb, {THEME['primary']} 15%, transparent)" if is_active else "transparent"
                text_color = THEME["text"] if is_active else THEME["text_muted"]
                with ui.element("a").props(f'href="{path}"').style(
                    f"display: flex; align-items: center; gap: 10px; padding: 10px 14px; "
                    f"border-radius: 8px; background: {bg}; color: {text_color}; "
                    f"font-size: 14px; font-weight: {'600' if is_active else '500'}; "
                    f"text-decoration: none; transition: background 0.15s;"
                ):
                    ui.label(icon).style("min-width: 22px; text-align: center;")
                    ui.label(label).classes("s-sidebar-label")

            # Spacer + daemon status
            ui.space()
            with ui.column().classes("s-sidebar-footer").style(
                f"padding: 12px; background: {THEME['surface2']}; border-radius: 8px; "
                f"margin-top: 16px; gap: 4px;"
            ):
                ui.label("⚡ Daemon active").style(
                    f"font-size: 11px; color: {THEME['success']}; font-weight: 600;"
                )
                # Resolve the actual bind address rather than hard-coding
                # localhost:8765 (smoke-run bug 5).
                import os as _os
                _host = _os.getenv("SYSTEMU_DASHBOARD_HOST") or "localhost"
                _port = _os.getenv("SYSTEMU_DASHBOARD_PORT") or "8765"
                ui.label(f"{_host}:{_port}").style(
                    f"font-size: 11px; color: {THEME['text_muted']};"
                )

        # ── Main content area ──
        with ui.column().style(
            f"flex: 1; padding: 32px 40px; overflow-y: auto; background: {THEME['bg']}; position: relative;"
        ):
            from systemu.interface.jobs import JobManager, JobStatus
            jm = JobManager.get()
            
            # ── Inline record dialog builder (must be inside page slot context) ──
            def _open_record_dialog():
                with ui.dialog() as dlg, ui.card().style(
                    f"background: {THEME['surface']}; border: 1px solid {THEME['border']}; border-radius: 16px; padding: 28px; min-width: 420px;"
                ):
                    ui.label("🔴 New Capture Session").style(
                        f"font-size: 18px; font-weight: 700; color: {THEME['text']}; margin-bottom: 16px;"
                    )
                    name_input = ui.input(label="Session Name (Task desc)", placeholder="Deploy new web page").style("width: 100%;")
                    
                    def _do_record():
                        if jm.has_active_capture():
                            ui.notify("A capture session is already running!", type="negative")
                            return
                        task_name = name_input.value.strip() or "Unnamed Task"
                        import sys
                        from systemu.interface.dashboard_state import AppState
                        cwd = AppState.get().project_root   # Always absolute
                        cmd = [sys.executable, "-m", "sharing_on", "record",
                               "--name", task_name, "--no-analyze"]
                        jm.start_job(f"Recording: {task_name}", "capture", cmd, cwd)
                        ui.notify("Recording started!", type="positive")
                        dlg.close()
                        
                    with ui.row().style("gap: 10px; margin-top: 16px;"):
                        ui.button("Start Recording", on_click=_do_record).style(
                            f"background: #ef4444; color: white; border-radius: 8px;"
                        )
                        ui.button("Cancel", on_click=dlg.close).style(
                            f"background: {THEME['surface2']}; color: {THEME['text']}; border-radius: 8px;"
                        )
                dlg.open()

            # Top Header Row
            with ui.row().classes("w-full items-center justify-between").style(
                f"margin-bottom: 24px; border-bottom: 1px solid {THEME['border']}; padding-bottom: 12px;"
            ):
                ui.label(page_title).style(
                    f"font-size: 26px; font-weight: 800; color: {THEME['text']};"
                )
                
                with ui.row().style("gap: 12px; align-items: center;"):
                    # Record button references the local closure — always in valid slot context
                    ui.button("🔴 Record Session", on_click=_open_record_dialog).style(
                        f"background: #ef4444; color: white; border-radius: 8px; font-weight: 600; padding: 6px 12px; font-size: 13px;"
                    )

                    # Active Tasks button — count badge sits next to button, menu uses @ui.refreshable
                    with ui.row().style("align-items: center; gap: 4px;"):
                        tasks_count = ui.label("").style(
                            f"background: #ef4444; color: white; border-radius: 10px; "
                            f"padding: 2px 7px; font-size: 10px; font-weight: 700; line-height: 1.6;"
                        )
                        tasks_count.set_visibility(False)

                        @ui.refreshable
                        def _render_jobs_list():
                            active_jobs = jm.get_active_jobs()
                            if not active_jobs:
                                ui.label("No pending background tasks.").style(
                                    f"color: {THEME['text_muted']}; font-size: 12px; padding: 4px;"
                                )
                            else:
                                for j in active_jobs:
                                    with ui.row().classes("w-full items-center justify-between").style(
                                        f"padding: 8px; border-bottom: 1px solid {THEME['border']};"
                                    ):
                                        with ui.column().style("gap: 2px;"):
                                            ui.label(j.name).style(
                                                f"font-size: 13px; font-weight: 600; color: {THEME['text']}; line-height: 1;"
                                            )
                                            ui.label(f"{j.type.upper()} • {j.status.value}").style(
                                                f"font-size: 10px; color: {THEME['primary']}; font-weight: 700; letter-spacing: 0.05em;"
                                            )
                                        ui.button(
                                            "⛔ Stop",
                                            on_click=lambda _, jid=j.id: jm.cancel_job_hard(jid),
                                        ).style(
                                            f"background: #ef4444; color: white; border-radius: 6px; "
                                            f"padding: 4px 10px; font-size: 11px; font-weight: 600;"
                                        )

                        with ui.button("⚙️ Active Tasks").style(
                            f"background: {THEME['surface2']}; color: {THEME['text']}; border-radius: 8px; font-size: 13px;"
                        ):
                            with ui.menu().style(
                                f"background: {THEME['surface']}; border: 1px solid {THEME['border']}; min-width: 320px; padding: 8px;"
                            ):
                                _render_jobs_list()

            # persistent security banner when auto-forge mode is on.
            # Rendered above page content so the operator sees it on every
            # route, not just in stdout where it scrolls away.
            _banner = _autoforge_banner_message()
            if _banner:
                with ui.row().classes("w-full bg-red-100 border-l-4 border-red-500 p-3 q-mb-md").style(
                    "background: #fee2e2; border-left: 4px solid #ef4444; "
                    "padding: 12px 16px; margin-bottom: 16px; border-radius: 4px; "
                    "align-items: center; gap: 12px;"
                ):
                    ui.icon("warning").style("color: #b91c1c; font-size: 24px;")
                    ui.label(_banner).style(
                        "color: #7f1d1d; font-weight: 700; font-size: 13px; line-height: 1.4;"
                    )

            # Page content is rendered here by the caller
            content_area = ui.column().classes("w-full")
            
            # ── Obfuscation Overlay ──
            overlay = ui.column().style(
                f"position: fixed; top: 0; left: 0; width: 100vw; height: 100vh; "
                f"background: rgba(10, 10, 12, 0.97); z-index: 9999; "
                f"align-items: center; justify-content: center; gap: 20px; "
                f"backdrop-filter: blur(10px);"
            )
            overlay.set_visibility(False)
            
            with overlay:
                ui.label("🔴 Capture Recording Active...").style(
                    f"font-size: 32px; font-weight: 800; color: #ef4444;"
                )
                ui.label("Dashboard is hidden from screenshots to prevent data leakage.").style(
                    f"font-size: 16px; color: {THEME['text_muted']};"
                )
                with ui.row().style("gap: 20px; margin-top: 20px;"):
                    def _btn_stop():
                        overlay.set_visibility(False)  # Instant UI feedback
                        ui.notify("Stopping capture, scheduling scroll pipeline...", type="info")
                        _stop_capture(jm)
                        
                    def _btn_cancel():
                        overlay.set_visibility(False)  # Instant UI feedback
                        ui.notify("Cancelling capture and cleaning up files...", type="warning")
                        _cancel_capture(jm)
                
                    ui.button("⏹ Stop & Analyze", on_click=_btn_stop).style(
                        f"background: {THEME['success']}; color: white; border-radius: 8px; padding: 12px 24px; font-size: 16px; font-weight: 600;"
                    )
                    ui.button("🗑️ Cancel & Trash", on_click=_btn_cancel).style(
                        f"background: {THEME['surface2']}; color: {THEME['text']}; border-radius: 8px; padding: 12px 24px; font-size: 16px; font-weight: 600;"
                    )

            # ── Polling Timer — updates count badge + refreshes jobs list ──
            # _last_state tracks (id, status) pairs so the DOM is only rebuilt
            # when something actually changed, not on every 1-second tick.
            _last_state = [frozenset()]

            def _update_jobs():
                try:
                    active = jm.get_active_jobs()
                    count  = len(active)

                    tasks_count.set_text(str(count))
                    tasks_count.set_visibility(count > 0)

                    current_state = frozenset((j.id, j.status) for j in active)
                    if current_state != _last_state[0]:
                        _last_state[0] = current_state
                        _render_jobs_list.refresh()

                    overlay.set_visibility(jm.has_active_capture())
                except Exception:
                    # Never permanently disable the timer; log and continue
                    logger.exception("[Dashboard] Active Tasks update error")

            from systemu.interface.ui_helpers import safe_timer
            safe_timer(1.0, _update_jobs)
            
            return content_area


# ── Dashboard Global Job Management Buttons ──

# NOTE: _show_record_dialog is kept for backward compatibility with overview.py imports
# but the real dialog is now inlined inside _build_layout() for proper NiceGUI slot context.
def _show_record_dialog():
    """Fallback: open the record dialog via page navigate to ensure correct slot context."""
    from nicegui import ui
    ui.navigate.to("/")
    ui.notify("Click the 🔴 Record Session button in the header.", type="info")


def _stop_capture(jm):
    import os
    import sys
    from nicegui import ui
    from pathlib import Path
    from systemu.interface.dashboard_state import AppState
    import threading
    
    state = AppState.get()
    cwd = state.project_root   # Absolute
    captures_dir = Path(cwd) / "captures"
    
    capture_jobs = [
        j for j in jm.jobs.values()
        if j.type == "capture" and j.status.value in ("running", "stopping")
    ]
    
    for j in capture_jobs:
        job_name = j.name  # capture name BEFORE stopping (closure-safe)
        jm.stop_job_gracefully(j.id)
        
        if captures_dir.exists():
            def _launch_refine(captured_name=job_name):
                import time
                # Wait for the process to finish flushing (2s)
                time.sleep(2)
                # Find the most recently modified capture directory
                dirs = sorted(
                    [d for d in captures_dir.iterdir() if d.is_dir()],
                    key=os.path.getmtime
                )
                if dirs:
                    latest = dirs[-1]
                    refine_cmd = [
                        sys.executable, "-m", "sharing_on",
                        "scrolls", "refine", str(latest)
                    ]
                    # --auto when running non-interactively (env
                    # var SYSTEMU_NON_INTERACTIVE — renamed from the misleading
                    # SYSTEMU_AUTO_APPROVE_SCROLLS).
                    if state.config.non_interactive:
                        refine_cmd.append("--auto")
                    jm.start_job(
                        f"Refining: {captured_name}",
                        "refine",
                        refine_cmd,
                        cwd,
                    )
                    logger.info("[Dashboard] Refine job dispatched for: %s", latest)
                else:
                    logger.warning("[Dashboard] No capture directory found to refine.")
            
            # Run in a daemon thread so we don't block NiceGUI
            t = threading.Thread(target=_launch_refine, daemon=True, name="refine-launcher")
            t.start()


def _cancel_capture(jm):
    import os
    import shutil
    from nicegui import ui
    from pathlib import Path
    from systemu.interface.dashboard_state import AppState
    
    state = AppState.get()
    cwd = state.project_root   # Absolute
    captures_dir = Path(cwd) / "captures"
    
    capture_jobs = [
        j for j in jm.jobs.values()
        if j.type == "capture" and j.status.value in ("running", "stopping")
    ]
    for j in capture_jobs:
        jm.cancel_job_hard(j.id)
    
    # Remove the most-recent capture dir (was being written to)
    if captures_dir.exists():
        dirs = sorted(
            [d for d in captures_dir.iterdir() if d.is_dir()],
            key=os.path.getmtime
        )
        if dirs:
            shutil.rmtree(dirs[-1], ignore_errors=True)
            ui.notify(f"Trashed capture: {dirs[-1].name}", type="positive")
        else:
            ui.notify("Capture cancelled (no directory to clean up).", type="positive")
    else:
        ui.notify("Capture cancelled.", type="positive")



# ─────────────────────────────────────────────────────────────────────────────
#  Page route registrations
# ─────────────────────────────────────────────────────────────────────────────

def register_routes() -> None:
    """Register all NiceGUI @ui.page routes."""
    from nicegui import ui

    # Deferred imports to keep this module importable without nicegui installed
    from systemu.interface.pages.overview                  import build_overview_page
    from systemu.interface.pages.scrolls                   import build_scrolls_page
    from systemu.interface.pages.army                      import build_army_page
    from systemu.interface.pages.tools                     import build_tools_page
    from systemu.interface.pages.skills_page               import build_skills_page
    from systemu.interface.pages.workshop                  import build_workshop_page
    from systemu.interface.pages.evolutions                import build_evolutions_page
    from systemu.interface.pages.settings                  import build_settings_page
    from systemu.interface.pages.notifications_page        import build_notifications_page
    from systemu.interface.pages.activities                import build_activities_page
    from systemu.interface.pages.shadow_memory_page        import build_shadow_memory_page
    from systemu.interface.pages.memory_consolidation_page import build_memory_consolidation_page
    from systemu.interface.pages.flywheel_page             import build_flywheel_page
    from systemu.interface.pages.chat_page                 import build_chat_page
    from systemu.interface.pages.systemu_chat              import build_systemu_chat_page
    from systemu.interface.pages.workflow_detail           import build_workflow_detail_page
    from systemu.interface.pages import recover as _recover_page_module  # noqa: F401  # registers /recover/<scope>/<id>

    @ui.page("/")
    def page_overview():
        with _build_layout("🏠 Overview", "/"):
            build_overview_page()

    @ui.page("/workflow/{workflow_id}")
    def page_workflow_detail(workflow_id: str):
        with _build_layout(f"🔄 Workflow — {workflow_id}", "/"):
            build_workflow_detail_page(workflow_id)

    @ui.page("/scrolls")
    def page_scrolls():
        with _build_layout("📜 Scrolls", "/scrolls"):
            build_scrolls_page()

    @ui.page("/army")
    def page_army():
        with _build_layout("👥 Shadow Army", "/army"):
            build_army_page()

    @ui.page("/memory")
    def page_memory():
        with _build_layout("💡 Memory Consolidation", "/memory"):
            build_memory_consolidation_page()

    @ui.page("/memory/{shadow_id}")
    def page_shadow_memory(shadow_id: str):
        with _build_layout(f"🧠 Memory — {shadow_id}", "/memory"):
            build_shadow_memory_page(shadow_id)

    @ui.page("/activities")
    def page_activities():
        with _build_layout("📋 Activities", "/activities"):
            build_activities_page()

    @ui.page("/tools")
    def page_tools():
        with _build_layout("🔧 Tool Registry", "/tools"):
            build_tools_page()

    @ui.page("/skills")
    def page_skills():
        with _build_layout("🧠 Skills Registry", "/skills"):
            build_skills_page()

    @ui.page("/workshop")
    def page_workshop():
        with _build_layout("🛠️ Workshop", "/workshop"):
            build_workshop_page()

    @ui.page("/evolutions")
    def page_evolutions():
        with _build_layout("🧬 Evolutions", "/evolutions"):
            build_evolutions_page()

    @ui.page("/notifications")
    def page_notifications():
        with _build_layout("🔔 Notifications", "/notifications"):
            build_notifications_page()

    @ui.page("/settings")
    def page_settings():
        with _build_layout("⚙️ Settings", "/settings"):
            build_settings_page()

    @ui.page("/flywheel")
    def page_flywheel():
        with _build_layout("⚙️ Data Flywheel", "/flywheel"):
            build_flywheel_page()

    @ui.page("/chat")
    def page_chat():
        with _build_layout("💬 Chat", "/chat"):
            build_chat_page()

    @ui.page("/systemu-chat")
    def page_systemu_chat():
        with _build_layout("🤖 Systemu Chat", "/systemu-chat"):
            build_systemu_chat_page()


# ─────────────────────────────────────────────────────────────────────────────
#  Public entry points
# ─────────────────────────────────────────────────────────────────────────────

def run_dashboard(
    config,
    *,
    port:    int  = 8765,
    host:    str  = "",      # empty → resolved from env below
    reload:  bool = False,
    dark:    bool = True,
) -> None:
    """Start the NiceGUI dashboard (blocking — call in a thread or foreground).

    Args:
        config:  Config object with vault path + tier model names.
        port:    Port to listen on (default 8765).
        host:    Bind host.  Empty string (default) reads SYSTEMU_DASHBOARD_HOST
                 from the environment; falls back to "127.0.0.1" so local dev
                 stays secure.  Docker sets SYSTEMU_DASHBOARD_HOST=0.0.0.0 in
                 docker-compose.yml so the port mapping works.
        reload:  Hot-reload on file changes (dev only).
        dark:    Enable NiceGUI dark mode.
    """
    import os
    if not host:
        host = os.getenv("SYSTEMU_DASHBOARD_HOST", "127.0.0.1")

    try:
        from nicegui import ui, app as ng_app
    except ImportError:
        logger.error(
            "[Dashboard] NiceGUI not installed. Run: pip install nicegui"
        )
        return

    from systemu.interface.dashboard_state import AppState
    try:
        state = AppState.create(config)
    except Exception as exc:
        logger.error(
            "[Dashboard] AppState.create() failed — dashboard cannot start: %s",
            exc, exc_info=True,
        )
        return

    # ── Start Supervisor (Phase 2) ─────────────────────────────────────────
    # Initializes the activity queue, dispatcher thread, and heartbeat watchdog.
    # Connects to EventBus so all events flow to Systemu Chat in real time.
    try:
        from systemu.runtime.supervisor import Supervisor
        from systemu.interface.notifications import set_vault
        set_vault(state.vault)    # ensure event log path is wired before supervisor starts
        sup = Supervisor.init(config, state.vault)
        state.supervisor = sup
        logger.info("[Dashboard] Supervisor started successfully.")
    except Exception as _sup_exc:
        logger.warning("[Dashboard] Supervisor failed to start (non-fatal): %s", _sup_exc)

    # ── Start WorkflowTracker (UX Phase 2) ─────────────────────────────────
    # Subscribes to the EventBus and warms its cache from the vault so the
    # Overview's Workflow Pipeline card and the /workflow/<id> route have
    # data ready from first render.
    try:
        from systemu.runtime.workflow_tracker import WorkflowTracker
        from systemu.interface.event_bus import EventBus as _EventBus
        WorkflowTracker.init(state.vault, _EventBus.get())
        logger.info("[Dashboard] WorkflowTracker initialised.")
    except Exception as _wt_exc:
        logger.warning(
            "[Dashboard] WorkflowTracker failed to start (non-fatal): %s",
            _wt_exc,
        )

    # ── Start messaging gateway (Phase 3, opt-in) ──────────────────────────
    # Reads SHARING_ON_TELEGRAM_BOT_TOKEN; if absent the gateway is dormant
    # and zero behaviour changes for existing users.  Allowlist enforcement
    # happens inside the gateway — empty allowlist means refuse to start.
    try:
        from systemu.messaging.telegram_gateway import build_from_env as _build_telegram
        from systemu.messaging.handlers import default_handlers as _msg_handlers
        gateway = _build_telegram(command_handlers=_msg_handlers())
        if gateway is not None:
            gateway.start()
            state.messaging_gateway = gateway
            logger.info("[Dashboard] Telegram gateway started.")

            # Subscribe the EventBus→push translator so the bot can
            # proactively notify on approval requests, execution
            # completion, watchdog fires, and tool proposals.
            try:
                from systemu.messaging.event_pusher import EventPusher
                from systemu.interface.event_bus import EventBus as _EventBus2
                pusher = EventPusher(gateway)
                pusher.subscribe(_EventBus2.get())
                state.messaging_pusher = pusher
                logger.info("[Dashboard] Messaging event pusher subscribed.")
            except Exception as _push_exc:
                logger.warning(
                    "[Dashboard] EventPusher failed to start (non-fatal): %s",
                    _push_exc,
                )
        else:
            logger.debug(
                "[Dashboard] Telegram gateway not configured "
                "(set SHARING_ON_TELEGRAM_BOT_TOKEN to enable)."
            )
    except Exception as _msg_exc:
        logger.warning(
            "[Dashboard] Messaging gateway failed to start (non-fatal): %s",
            _msg_exc,
        )

    register_routes()

    # ── Graceful shutdown hook ─────────────────────────────────────────────
    @ng_app.on_shutdown
    async def _on_app_shutdown():
        """Persist supervisor queue and signal threads to stop on SIGTERM/SIGINT."""
        try:
            from systemu.runtime.supervisor import Supervisor
            Supervisor.get().shutdown()
        except Exception:
            pass
        # Stop the messaging gateway + event pusher if running.
        try:
            pusher = getattr(state, "messaging_pusher", None)
            if pusher is not None:
                pusher.shutdown()
        except Exception:
            pass
        try:
            gw = getattr(state, "messaging_gateway", None)
            if gw is not None:
                gw.stop()
        except Exception:
            pass

    logger.info("[Dashboard] Starting on http://%s:%d", host, port)
    ui.run(
        host=host,
        port=port,
        title="Systemu Dashboard",
        favicon="⚡",
        dark=dark,
        reload=reload,
        show=False,          # Don't auto-open browser
        uvicorn_logging_level="warning",
    )


def run_dashboard_thread(
    config,
    *,
    port: int = 8765,
) -> threading.Thread:
    """Run the dashboard in a background thread. Returns the thread."""
    t = threading.Thread(
        target=run_dashboard,
        kwargs={"config": config, "port": port},
        daemon=True,
        name="systemu-dashboard",
    )
    t.start()
    logger.info("[Dashboard] Dashboard thread started on port %d", port)
    return t
