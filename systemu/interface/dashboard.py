"""Systemu NiceGUI Web Dashboard — main entry point.

Starts a NiceGUI app on localhost:<port> (default 8765).
Provides a persistent sidebar navigation and six page routes:
  /              Overview (stat cards + activity feed)
  /scrolls       Scroll list + detail
  /shadows       Shadows card grid (storage key: shadow_army)
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
    """v0.6.9: return a persistent security banner message when auto-forge
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

# v0.9.11 Phase 5: the 6-spine command-center nav (flat — no groups).
# Each spine points at its current primary route; sub-routes fold in over
# Slices 2-4 (repoint + redirect then). Inbox is NOT a spine — it lives in
# the persistent right rail + the /inbox page (plus the header "Needs you"
# badge for narrow viewports).
#
# URL routes for the merged pages (/systemu-chat → /chat?tab=live,
# /memory|/flywheel|/notifications → /insights?tab=…) are preserved as
# redirect handlers in register_routes() below — bookmarks and email
# deep-links continue to work.
# Line icons (Material Symbols via icons.py — board 4 §2 "drop emoji for a line
# set").  icon(concept) → a Quasar/Material symbol name, rendered with ui.icon —
# never an emoji literal.
from systemu.interface.design.icons import icon as _icon

# v0.9.37: brand logo. Dropped in at systemu/interface/assets/logo.png and used
# for the favicon + sidebar mark when present; falls back to the bolt glyph so a
# missing file never breaks the dashboard. Shipped via the package-data glob.
_BRAND_LOGO = Path(__file__).resolve().parent / "assets" / "logo.png"


# ─────────────────────────────────────────────────────────────────────────────
#  R-SEC1: route-guard middleware (authentication enforcement)
# ─────────────────────────────────────────────────────────────────────────────
#
# Rule: when a passphrase is configured (state.dashboard_auth_active is True)
# EVERY dashboard route requires an authenticated session — unauthenticated
# browser navigation → 302 /login, an unauthenticated XHR/API request → 401.
# When no passphrase is configured (the loopback default) the guard is a STRICT
# pass-through no-op, so the existing UI regression floor is unchanged.
#
# The security core is factored into two pure functions (``_is_auth_allowlisted``
# and ``_guard_decision``) so the allow/deny logic can be unit-tested exhaustively
# without a live server. ``_install_route_guard`` wraps them in a Starlette HTTP
# middleware that FAILS CLOSED on any internal error and never touches
# ``app.storage.user`` outside a request context in a way that can raise open.

# Path prefixes that are ALWAYS exempt — the NiceGUI framework infra the login
# page depends on. /_nicegui/* (static JS + the websocket) MUST be exempt or the
# login page can never load its own assets; /assets + /static are our font/static
# mounts. These are matched ONLY on a SEGMENT boundary (p == pre or p startswith
# pre + "/") — Finding B: a raw ``startswith`` treated /assets-export,
# /staticdata, /_nicegui_admin, … as allowlisted, a latent auth bypass.
_AUTH_ALLOWLIST_PREFIXES = ("/_nicegui", "/_nicegui_ws", "/assets", "/static")
# Exact-match exemptions — the login page itself + the favicon fetched pre-auth
# by the browser chrome. Exact (not prefix) so /login-history etc. stay guarded.
_AUTH_ALLOWLIST_EXACT = ("/login", "/favicon.ico")


def _is_auth_allowlisted(path: str) -> bool:
    """True iff ``path`` is exempt from the auth guard (login page + framework
    infra). Pure + defensive: a non-str / odd path is treated as NOT allowlisted
    (fail-closed — an unexpected shape gets guarded, never waved through).

    Finding B: prefix matches require a SEGMENT boundary — ``p == pre`` or
    ``p.startswith(pre + "/")`` — so ``/assets`` allows ``/assets`` and
    ``/assets/fonts/x.woff2`` but NOT ``/assets-export``. Exact entries
    (``/login``, ``/favicon.ico``) match verbatim, so ``/login-history`` and
    ``/loginX`` stay guarded."""
    try:
        p = path or ""
        if p in _AUTH_ALLOWLIST_EXACT:
            return True
        for pre in _AUTH_ALLOWLIST_PREFIXES:
            if p == pre or p.startswith(pre + "/"):
                return True
        return False
    except Exception:
        return False


def _guard_decision(path: str, accept_header: str, *, authed: bool, active: bool) -> str:
    """The pure route-guard decision. Returns one of ``"pass"``, ``"redirect"``
    (302 → /login for a browser navigation), or ``"401"`` (JSON, for XHR/API).

    * ``active`` False → always ``"pass"`` (no passphrase configured; no-op).
    * allowlisted path → ``"pass"`` (login page + framework infra).
    * ``authed`` True → ``"pass"``.
    * otherwise: ``"redirect"`` when the caller looks like an HTML browser
      navigation (``text/html`` in Accept), else ``"401"`` — a missing/JSON
      Accept is treated as a non-navigation so we never 302-loop an API client.
    """
    if not active:
        return "pass"
    if _is_auth_allowlisted(path):
        return "pass"
    if authed:
        return "pass"
    if "text/html" in (accept_header or ""):
        return "redirect"
    return "401"


# Holder for the state object the (single) installed guard reads. NiceGUI's
# ``app`` is a process-wide singleton, so the guard middleware must be added
# EXACTLY ONCE — re-installing on a second run_dashboard() call would stack a
# second guard on the shared app (and Starlette forbids adding middleware once
# the app has started). We install one guard that reads the CURRENT state from
# this holder; re-installation just re-points the holder.
_ROUTE_GUARD_STATE: "list" = [None]
_ROUTE_GUARD_INSTALLED: "list[bool]" = [False]


def _install_route_guard(ng_app, state) -> None:
    """Register the authentication route-guard middleware on ``ng_app`` (once).

    ``state`` is the live ``AppState`` — the guard reads ``dashboard_auth_active``
    off it on EVERY request (so a later posture change is honoured) via
    ``getattr(state, "dashboard_auth_active", False)``. Fails closed: any error
    inside the guard denies the request (redirect for HTML / 401 otherwise); it
    never raises open, and never lets ``app.storage.user`` access (which raises
    outside a request context) leak a request through.

    Idempotent: the middleware is added to ``ng_app`` only on the first call;
    subsequent calls just re-point the state holder so a re-``run_dashboard`` (or
    a second call within the same process) never stacks a duplicate guard.
    """
    from starlette.responses import RedirectResponse, JSONResponse

    # Re-point the holder to the current state on every call.
    _ROUTE_GUARD_STATE[0] = state
    if _ROUTE_GUARD_INSTALLED[0]:
        return  # guard already on the app; the holder swap above is enough.

    def _deny(accept_header: str):
        if "text/html" in (accept_header or ""):
            return RedirectResponse("/login", status_code=302)
        return JSONResponse({"detail": "authentication required"}, status_code=401)

    def _compute_decision(request) -> str:
        """Compute pass|redirect|401 for a request. FAILS CLOSED: any error in
        the guard's OWN logic denies (deny for HTML → redirect else 401). This
        deliberately wraps ONLY the decision — the pass-through ``call_next`` is
        outside it, so a DOWNSTREAM render error is never masked as an auth
        redirect (it propagates as its real 500)."""
        try:
            _state = _ROUTE_GUARD_STATE[0]
            active = bool(getattr(_state, "dashboard_auth_active", False))
            if not active:
                return "pass"                    # regression floor: strict no-op.
            path = request.url.path
            accept = request.headers.get("accept", "")
            # Allowlisted paths never need a session (login page + infra) — do
            # NOT touch app.storage.user for these (it can raise on infra paths).
            if _is_auth_allowlisted(path):
                return "pass"
            # Resolve the session flag defensively — app.storage.user raises
            # outside a request/websocket context; treat any failure as UNAUTHED.
            authed = False
            try:
                from nicegui import app as _ng_app
                authed = _ng_app.storage.user.get("authed") is True
            except Exception:
                authed = False
            # R-UTL1 / U-1a: a valid static bearer token is an EQUAL credential
            # to a session, so an API client is not denied before it reaches the
            # handler. This only ever ADDS a way to pass; it never waives the
            # guard (a bad/absent token leaves `authed` exactly as it was), and
            # the /api handlers re-check independently — the guard is one of two
            # fences, not the only one.
            if not authed:
                try:
                    from systemu.interface.task_api import authenticate as _api_auth
                    _st = _ROUTE_GUARD_STATE[0]
                    _vault = getattr(_st, "vault", None)
                    if _vault is not None:
                        authed = _api_auth(
                            _vault, request.headers.get("authorization"))[0]
                except Exception:
                    authed = False
            return _guard_decision(path, accept, authed=authed, active=active)
        except Exception:
            logger.warning("[Dashboard] route guard error — failing closed",
                           exc_info=True)
            # Fail CLOSED: deny. HTML → redirect, else 401.
            try:
                accept = request.headers.get("accept", "")
            except Exception:
                accept = ""
            return "redirect" if "text/html" in (accept or "") else "401"

    @ng_app.middleware("http")
    async def _auth_guard(request, call_next):
        decision = _compute_decision(request)
        if decision != "pass":
            try:
                accept = request.headers.get("accept", "")
            except Exception:
                accept = ""
            return _deny(accept)
        # Pass-through is OUTSIDE the fail-closed wrapper: a downstream handler
        # error surfaces as its real status, never as a spurious auth redirect.
        return await call_next(request)

    _ROUTE_GUARD_INSTALLED[0] = True


NAV_SPINES = [
    ("/",         _icon("home"),     "Home"),
    ("/work",     _icon("work"),     "Work"),      # Slice 2a: workflow-centric list (scrolls+activities fold in)
    ("/shadows",  _icon("shadow"),   "Shadows"),
    ("/tools",    _icon("build"),    "Build"),     # tools+skills+evolutions fold in (Slice 3)
    ("/insights", _icon("insights"), "Insights"),
    ("/settings", _icon("settings"), "Settings"),
]
NAV_ITEMS = NAV_SPINES   # back-compat alias (callers iterate (path, icon, label))

# Every current route → its spine (for exact active-route highlighting).
SPINE_OF = {
    "/": "/",
    "/work": "/work", "/scrolls": "/work", "/activities": "/work", "/workflow": "/work", "/chat": "/work",
    "/shadows": "/shadows", "/memory": "/shadows", "/army": "/shadows",  # 6h: /army is the legacy alias, still lights Shadows
    "/tools": "/tools", "/skills": "/tools", "/evolutions": "/tools",
    "/insights": "/insights",
    "/settings": "/settings", "/privacy": "/settings",   # R-P3b privacy page lives under Settings
    # /inbox intentionally absent → no left-nav highlight (right rail owns it)
}


def spine_of(current_path: str) -> str:
    """Return the 6-spine nav path a given route belongs to ("" if none —
    e.g. /inbox, which lives in the right rail)."""
    if current_path in SPINE_OF:
        return SPINE_OF[current_path]
    first = "/" + current_path.lstrip("/").split("/", 1)[0]
    return SPINE_OF.get(first, "")


def active_nav_path(current_path: str, nav_paths: list) -> str:
    """The nav spine to render active for current_path (exact via spine_of);
    "" when no spine owns it — no char-prefix false positives (the old
    `startswith` lit /tools for /toolsmith)."""
    sp = spine_of(current_path)
    return sp if sp in nav_paths else ""


# Header line-icon for a route — the line icon of its owning spine (board 4 §2).
_HEADER_ICON_CONCEPT = {
    "/": "home", "/work": "work", "/shadows": "shadow",
    "/tools": "build", "/insights": "insights", "/settings": "settings",
}


def _spine_icon(current_path: str) -> str:
    """Material symbol for the page header — the line icon of the route's spine
    (falls back to the inbox glyph for the spine-less /inbox)."""
    concept = _HEADER_ICON_CONCEPT.get(spine_of(current_path))
    return _icon(concept) if concept else _icon("inbox")


def _record_dispatch_args(task_name: str, mode: str, source: str,
                          generalization: str = "standard") -> list:
    """Build the ``record`` dispatch args for the dashboard dialog (v0.9.35
    Phase 0, was Feature D capture-scope). "all" = today's behaviour (no
    source flags). "single" requires a non-empty source target; a blank target
    degrades to all so we never spawn a useless ``--sources single`` with
    nothing to narrow to.

    v0.9.35 Phase 1: ``generalization`` is a SEPARATE record-time toggle —
    "standard" (default) emits no flag so the spawned argv is byte-identical to
    pre-v0.9.35; "broad"/"narrow" append ``--generalize <mode>``."""
    args = ["--name", task_name, "--no-analyze"]
    if mode == "single" and source.strip():
        args += ["--sources", "single", "--source", source.strip()]
    if generalization in ("broad", "narrow"):
        args += ["--generalize", generalization]
    return args


def _build_layout(page_title: str, current_path: str):
    """Return a NiceGUI context manager that renders the sidebar + header."""
    from nicegui import ui
    from systemu.interface.dashboard_state import THEME, GLOBAL_CSS
    from systemu.interface.design.primitives import button as ds_button

    ui.add_css(GLOBAL_CSS)
    # W4.3: fonts are vendored locally now — the @font-face rules in GLOBAL_CSS
    # point at /assets/fonts (served from systemu/interface/assets/fonts). No
    # Google Fonts CDN <link> here: offline-safe + no third-party request.

    # Root wrapper
    with ui.row().style(
        "width: 100vw; min-height: 100vh; background: transparent; gap: 0;"
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
            f"width: clamp(160px, 13vw, 200px); min-width: clamp(160px, 13vw, 200px); background: {THEME['surface']}; "
            f"border-right: 1px solid {THEME['border']}; padding: 24px 12px; gap: 4px; "
            f"height: 100vh; position: sticky; top: 0; overflow-y: auto;"
        ):
            # Logo / brand — sidebar collapses to icon-only on narrow viewports
            # (handled via the .s-sidebar / .s-sidebar-label CSS classes
            # defined in GLOBAL_CSS).
            with ui.row().classes("s-sidebar-header").style(
                "align-items: center; gap: 10px; padding: 8px 12px; margin-bottom: 16px;"
            ):
                if _BRAND_LOGO.exists():
                    ui.image(str(_BRAND_LOGO)).style(
                        "width: 34px; height: 34px; border-radius: 8px; "
                        "flex: 0 0 auto;")
                else:
                    ui.icon("bolt").style(f"font-size: 26px; color: {THEME['primary']};")
                ui.label("Systemu").classes("s-sidebar-label").style(
                    f"font-size: 18px; font-weight: 800; color: {THEME['text']};"
                )

            # v0.9.11 Phase 5: flat 6-spine nav — no groups, no expansion.
            # The active spine is resolved once via spine_of (exact, then
            # first-segment), so folded sub-routes (/activities, /skills,
            # /workflow/{id}, …) highlight their owning spine.
            nav_paths = [p for p, _i, _l in NAV_SPINES]
            active_path = active_nav_path(current_path, nav_paths)

            def _render_nav_link(path: str, icon: str, label: str) -> None:
                is_active = active_path == path
                bg = f"color-mix(in srgb, {THEME['primary']} 15%, transparent)" if is_active else "transparent"
                text_color = THEME["text"] if is_active else THEME["text_muted"]
                # W4.4 a11y: aria-label so the icon-only collapsed nav (narrow
                # viewports hide .s-sidebar-label) still has an accessible name;
                # aria-current marks the active spine for assistive tech.
                _aria_current = ' aria-current="page"' if is_active else ""
                with ui.element("a").props(
                    f'href="{path}" aria-label="{label}"{_aria_current}'
                ).style(
                    f"display: flex; align-items: center; gap: 10px; padding: 10px 14px; "
                    f"border-radius: 8px; background: {bg}; color: {text_color}; "
                    f"font-size: 14px; font-weight: {'600' if is_active else '500'}; "
                    f"text-decoration: none; transition: background 0.15s;"
                ):
                    icon_color = THEME["primary"] if is_active else THEME["text_muted"]
                    ui.icon(icon).style(
                        # Fixed 22px centered box so every glyph (incl. wide ones)
                        # stays in the same column — narrow/wide icons no longer
                        # shift the nav alignment.
                        f"flex: 0 0 22px; display: inline-flex; align-items: center; "
                        f"justify-content: center; font-size: 20px; line-height: 1; "
                        f"color: {icon_color};"
                    )
                    ui.label(label).classes("s-sidebar-label")

            for path, icon, label in NAV_SPINES:
                _render_nav_link(path, icon, label)

            # Spacer + daemon status
            ui.space()
            with ui.column().classes("s-sidebar-footer").style(
                f"padding: 12px; background: {THEME['surface2']}; border-radius: 8px; "
                f"margin-top: 16px; gap: 4px;"
            ):
                ui.label("Daemon active").style(
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
                # R-SEC1: a Sign-out link — shown ONLY when authentication is
                # enforced (a passphrase is configured). When auth is inactive
                # (loopback default) nothing renders here, so the existing UI
                # regression floor is untouched.
                try:
                    from systemu.interface.dashboard_state import AppState as _AuthState
                    if getattr(_AuthState.get(), "dashboard_auth_active", False):
                        from systemu.interface.pages.login import logout as _logout
                        ui.link("Sign out", "#").on(
                            "click", lambda _e: _logout()
                        ).classes("s-muted").style(
                            "font-size: 11px; margin-top: 6px; cursor: pointer;"
                        )
                except Exception:
                    pass  # never let the sign-out link break the sidebar

        # ── Main content area ──
        with ui.column().style(
            f"flex: 1; padding: 32px 40px; overflow-y: auto; background: transparent; position: relative;"
        ):
            # v0.8.0.2: top-of-page health banner -- surfaces multi-daemon,
            # missing OPENROUTER key, read-only vault.  Silent when healthy.
            try:
                from systemu.interface.components.health_banner import render_health_banner
                from systemu.interface.dashboard_state import AppState
                from pathlib import Path
                _hb_state = AppState.get()
                _hb_vault_dir = Path(_hb_state.config.vault_dir).resolve() if _hb_state and _hb_state.config else None
            except Exception:
                _hb_vault_dir = None
                render_health_banner = None
            if render_health_banner is not None:
                try:
                    render_health_banner(_hb_vault_dir)
                except Exception:
                    pass  # never let banner failure break the dashboard

            # R-UX3 / UX-13: the Ctrl+K palette, mounted once per page from the
            # shared layout so it exists on EVERY route. Navigate/prefill only —
            # it can never run anything (see command_palette's safety line).
            try:
                from systemu.interface.components.command_palette import (
                    build_command_palette)
                from systemu.interface.dashboard_state import AppState as _CpState
                _cp_state = _CpState.get()
                build_command_palette(getattr(_cp_state, "vault", None))
            except Exception:
                pass  # never let the palette break the page

            from systemu.interface.jobs import JobManager, JobStatus
            jm = JobManager.get()
            
            # ── Inline record dialog builder (must be inside page slot context) ──
            def _open_record_dialog():
                with ui.dialog() as dlg, ui.card().style(
                    f"background: {THEME['surface']}; border: 1px solid {THEME['border']}; border-radius: 16px; padding: 28px; min-width: 420px;"
                ):
                    ui.label("New Capture Session").style(
                        f"font-size: 18px; font-weight: 700; color: {THEME['text']}; margin-bottom: 16px;"
                    )
                    name_input = ui.input(label="Session Name (Task desc)", placeholder="Deploy new web page").style("width: 100%;")

                    # v0.9.35 Phase 0: capture-sources toggle (single = one app).
                    sources_toggle = ui.toggle(
                        {"all": "All sources", "single": "One source"},
                        value="all",
                    ).style("margin-top: 12px;")
                    source_input = ui.input(
                        label="App / process or origin",
                        placeholder="chrome.exe  or  https://github.com",
                    ).style("width: 100%;")
                    source_input.bind_visibility_from(sources_toggle, "value",
                                                      backward=lambda v: v == "single")

                    # v0.9.35 Phase 1: record-time GENERALIZATION toggle (separate
                    # from the capture-sources toggle above). standard == today.
                    # NOTE: plain concatenated style string (not an f-string) so the
                    # UI-style lint stays green — it only flags inline .style(f"…").
                    ui.label("Reusability").style(
                        "margin-top: 16px; font-size: 12px; color: "
                        + THEME["text_muted"] + ";"
                    )
                    generalize_toggle = ui.toggle(
                        {"broad": "Reusable (ask params)",
                         "standard": "As recorded",
                         "narrow": "Exact (baked in)"},
                        value="standard",
                    ).style("margin-top: 4px;")

                    def _do_record():
                        if jm.has_active_capture():
                            ui.notify("A capture session is already running!", type="negative")
                            return
                        task_name = name_input.value.strip() or "Unnamed Task"
                        from systemu.interface.command.dispatch import dispatch
                        from systemu.interface.dashboard_state import AppState
                        cwd = AppState.get().project_root   # Always absolute
                        args = _record_dispatch_args(
                            task_name,
                            mode=sources_toggle.value,
                            source=source_input.value or "",
                            generalization=generalize_toggle.value,
                        )
                        dispatch("record", args,
                                 cwd=cwd, stream=True, job_type="capture",
                                 dedup_key=f"record:{task_name}")
                        ui.notify("Recording started!", type="positive")
                        dlg.close()
                        
                    with ui.row().style("gap: 10px; margin-top: 16px;"):
                        ui.button("Start Recording", on_click=_do_record).style(
                            f"background: {THEME['danger']}; color: white; border-radius: 8px;"
                        )
                        ui.button("Cancel", on_click=dlg.close).style(
                            f"background: {THEME['surface2']}; color: {THEME['text']}; border-radius: 8px;"
                        )
                dlg.open()

            # W12 (audit): Home's "Record" tile used to call a navigate-to-/
            # fallback that visibly did NOTHING on the home page itself.
            # Stash the real opener per-client so any page can launch it.
            try:
                ui.context.client.systemu_open_record_dialog = _open_record_dialog
            except Exception:
                pass

            # Top Header Row
            with ui.row().classes("w-full items-center justify-between").style(
                f"margin-bottom: 24px; border-bottom: 1px solid {THEME['border']}; padding-bottom: 12px;"
            ):
                with ui.row().style("align-items: center; gap: 12px;"):
                    import re as _re_title
                    _clean_title = _re_title.sub(r"^\s*[^\w\s]+\s*", "", page_title)
                    ui.icon(_spine_icon(current_path)).style(
                        f"font-size: 28px; color: {THEME['primary']};"
                    )
                    ui.label(_clean_title).style(
                        f"font-size: 26px; font-weight: 800; color: {THEME['text']};"
                    )
                
                with ui.row().style("gap: 12px; align-items: center;"):
                    # "Needs you (N)" — Phase 5 amendment A1: the always-
                    # visible Inbox fallback.  The right rail dies <1100px and
                    # the sidebar collapses at 768px; this HEADER badge is the
                    # narrow-viewport path to parked harness gates.  Token
                    # classes only (s-pill tint) — zero new inline f-styles.
                    try:
                        from systemu.interface.dashboard_state import AppState as _NyState
                        _ny_state = _NyState.get()
                        _ny_vault = getattr(_ny_state, "vault", None) if _ny_state else None
                    except Exception:
                        _ny_vault = None

                    # W5.2: "Status" — recent tasks with their outcome message
                    # + workflow link + artifacts path, so the operator never
                    # has to hunt for "what happened to my task".
                    if _ny_vault is not None:
                        try:
                            from systemu.interface.components.status_menu import (
                                render_status_menu,
                            )
                            render_status_menu(_ny_vault)
                        except Exception:
                            # W7.3: loud — a swallowed render error here made the
                            # button look like it never shipped.
                            logger.warning("[Dashboard] Status menu failed to render",
                                           exc_info=True)
                    # W11.5: an unfinished tour stays visible until completed
                    # (the tour never redirects — this pill is its memory).
                    try:
                        from systemu.interface.tour import render_tour_pill
                        render_tour_pill(_ny_vault)
                    except Exception:
                        logger.debug("[Dashboard] tour pill failed", exc_info=True)

                    _ny_model = needs_you_badge_model(_ny_vault)
                    needs_you_badge = ui.link(
                        f"Needs you ({_ny_model['count']})",
                        _ny_model["target"],
                    ).classes("s-pill s-pill--warn").style(
                        "text-decoration: none; cursor: pointer;"
                    )
                    needs_you_badge.set_visibility(_ny_model["visible"])
                    # W11.6: the header's primary controls explain themselves.
                    with needs_you_badge:
                        ui.tooltip("Approvals and questions waiting for you — click to answer")

                    def _update_needs_you():
                        m = needs_you_badge_model(_ny_vault)
                        needs_you_badge.set_text(f"Needs you ({m['count']})")
                        needs_you_badge.set_visibility(m["visible"])

                    from systemu.interface.ui_helpers import safe_timer as _ny_timer
                    _ny_timer(2.0, _update_needs_you)

                    # T1b: discoverable link to the read-only OnTheTable board
                    # (spine-less, like /inbox). A quiet neutral pill next to
                    # "Needs you"; no count/timer — it's a static entry point.
                    ui.link("On the table", "/table").classes("s-pill").style(
                        "text-decoration: none; cursor: pointer;"
                    )

                    # ＋New — the global creation menu (Record session / Submit
                    # task).  Trigger uses the design-system primitive (token
                    # classes, no inline f-style); the dropdown uses the
                    # `.s-menu` token class — net-zero new inline styles vs the
                    # single styled Record button this replaces.
                    with ds_button("＋ New", variant="primary"):
                        ui.tooltip("Start something: record yourself doing a task, or submit one in chat")
                        with ui.menu().classes("s-menu"):
                            ui.menu_item("Record session", on_click=_open_record_dialog)
                            ui.menu_item(
                                "Submit task",
                                on_click=lambda: ui.navigate.to("/chat?tab=compose"),
                            )

                    # Active Tasks button — count badge sits next to button, menu uses @ui.refreshable
                    with ui.row().style("align-items: center; gap: 4px;"):
                        tasks_count = ui.label("").style(
                            f"background: {THEME['danger']}; color: white; border-radius: 10px; "
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
                                            "Stop",
                                            on_click=lambda _, jid=j.id: jm.cancel_job_hard(jid),
                                        ).style(
                                            f"background: {THEME['danger']}; color: white; border-radius: 6px; "
                                            f"padding: 4px 10px; font-size: 11px; font-weight: 600;"
                                        )

                        with ui.button("Active Tasks").style(
                            f"background: {THEME['surface2']}; color: {THEME['text']}; border-radius: 8px; font-size: 13px;"
                        ):
                            with ui.menu().style(
                                f"background: {THEME['surface']}; border: 1px solid {THEME['border']}; min-width: 320px; padding: 8px;"
                            ):
                                _render_jobs_list()

            # v0.6.9: persistent security banner when auto-forge mode is on.
            # Rendered above page content so the operator sees it on every
            # route, not just in stdout where it scrolls away.
            _banner = _autoforge_banner_message()
            if _banner:
                with ui.row().classes("s-banner s-banner--danger w-full q-mb-md"):
                    ui.icon("warning").style("font-size: 24px;")
                    ui.label(_banner).style(
                        "font-weight: 700; font-size: 13px; line-height: 1.4;"
                    )

            # W11.5: the floating tour card (?tour=N) — rendered by the
            # layout so it follows the operator across every step's route.
            try:
                from systemu.interface.tour import maybe_render_tour
                maybe_render_tour(current_path)
            except Exception:
                logger.debug("[Dashboard] tour card failed", exc_info=True)

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
                ui.label("Capture Recording Active...").style(
                    f"font-size: 32px; font-weight: 800; color: {THEME['danger']};"
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
                
                    ui.button("Stop & Analyze", on_click=_btn_stop).style(
                        f"background: {THEME['success']}; color: white; border-radius: 8px; padding: 12px 24px; font-size: 16px; font-weight: 600;"
                    )
                    ui.button("Cancel & Trash", on_click=_btn_cancel).style(
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

        # ── Right rail (persistent) — sibling of main content, inside root row.
        #    Same on every page; rebuilt per route (the EventBus ring buffer
        #    replays history so "Live" repopulates seamlessly).
        with ui.column().classes("s-rail").style("position: sticky; top: 0;"):
            _render_persistent_right_rail()

        return content_area


def _render_persistent_right_rail() -> None:
    """Render the persistent right rail (Needs-you inbox glance + Live runs).

    Defensive: the rail must NEVER break the page shell — any failure to reach
    the vault or render a pane is swallowed (logged), exactly like the
    health-banner block.
    """
    try:
        from systemu.interface.dashboard_state import AppState
        from systemu.interface.components.right_rail import render_right_rail
        state = AppState.get()
        vault = getattr(state, "vault", None) if state else None
    except Exception:
        return
    if vault is None:
        return
    try:
        render_right_rail(vault)
    except Exception:
        logger.exception("[Dashboard] right-rail render error")


def plus_new_menu_items() -> list:
    """The global ＋New action's items (spec §4.2): a global capture-session
    recorder and a task submission.  Pure so the menu contents are testable.
    """
    return ["Record session", "Submit task"]


def needs_you_badge_model(vault) -> dict:
    """Pure model for the header "Needs you (N)" badge (Phase 5, amendment A1).

    W5.1: counts the COMPLETE pending-attention set — inbox gates AND non-gate
    asks (stuck-run ``structured_question``s, ``credential`` requests, …) via
    ``attention.needs_you_total``. The gate-only count left parked runs
    invisible: a stuck chat task showed badge 0 / "nothing needs you".

    The Phase-4 right rail hides below 1100px and the sidebar collapses at
    768px, so this header badge is the narrow-viewport path to parked work.

    Defensive: ANY failure (no vault, unreadable decision store, …) yields
    ``count 0 / hidden`` — the badge must never break the page shell.

    R-B4: the count now also includes the /table tray's `suggested` items, so the
    target is no longer a constant — it follows the breakdown (``/table`` when the
    tray is the ONLY thing waiting, ``/inbox`` otherwise). A badge that counted the
    tray but always linked to /inbox would land the operator on an empty Inbox.
    """
    try:
        from systemu.interface.components.attention import needs_you_breakdown
        b = needs_you_breakdown(vault)
        count, target = b["total"], b["target"]
    except Exception:
        count, target = 0, "/inbox"
    return {"count": count, "visible": count > 0, "target": target}


# ── Dashboard Global Job Management Buttons ──

# NOTE: _show_record_dialog is kept as a navigate-based fallback; the real
# dialog is now inlined inside _build_layout() for proper NiceGUI slot context.
def _show_record_dialog():
    """Fallback: open the record dialog via page navigate to ensure correct slot context."""
    from nicegui import ui
    ui.navigate.to("/")
    ui.notify("Click the Record Session button in the header.", type="info")


def _stop_capture(jm):
    import os
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
    
    # v0.8.0.3: ALWAYS run the refine launcher (was previously gated on
    # `captures_dir.exists()` which silently did nothing when the dir didn't
    # exist — every pip install hit this).  The launcher now surfaces a toast
    # for every outcome (dispatched / no captures dir / no session) so the
    # operator sees what happened.
    from nicegui import ui as _ui_for_toast
    # W13.4 / W7.1: capture the client on the UI thread — the refine
    # launcher below runs on a worker thread and must re-enter it for
    # notify/navigate to reach this operator's browser.
    try:
        _client = ui.context.client
    except Exception:
        _client = None
    for j in capture_jobs:
        job_name = j.name  # capture name BEFORE stopping (closure-safe)
        jm.stop_job_gracefully(j.id)

        def _launch_refine(captured_name=job_name):
            import time
            # Wait for the capture process to flush events.db + session.json
            time.sleep(2)
            if not captures_dir.exists():
                logger.warning("[Dashboard] captures dir does not exist: %s", captures_dir)
                try:
                    _ui_for_toast.notify(
                        f"No captures directory at {captures_dir} — "
                        f"refine not dispatched.",
                        type="negative",
                    )
                except Exception:
                    pass
                return
            dirs = sorted(
                [d for d in captures_dir.iterdir() if d.is_dir()],
                key=os.path.getmtime,
            )
            if not dirs:
                logger.warning("[Dashboard] No capture directory found in %s to refine.", captures_dir)
                try:
                    _ui_for_toast.notify(
                        "Capture stopped but no session directory was written. "
                        "Recording may have crashed — check daemon log.",
                        type="negative",
                    )
                except Exception:
                    pass
                return
            latest = dirs[-1]
            from systemu.interface.command.dispatch import dispatch
            # v0.6.1-b: --auto when running non-interactively (env var
            # SYSTEMU_NON_INTERACTIVE — renamed from the misleading
            # SYSTEMU_AUTO_APPROVE_SCROLLS).
            args = [str(latest)] + (["--auto"] if state.config.non_interactive else [])
            dispatch(
                "scrolls refine", args, cwd=cwd, stream=True,
                job_type="refine", dedup_key=f"refine:{latest}",
            )
            logger.info("[Dashboard] Refine job dispatched for: %s", latest)
            # W13.4: plain-language guidance + take the operator to Work,
            # where the new workflow row appears and (W12 F7) Needs-you
            # rings the moment analysis finishes. W7.1 pattern: re-enter the
            # captured client — this runs on a worker thread.
            try:
                if _client is not None:
                    with _client:
                        _ui_for_toast.notify(
                            "Recording saved — turning it into a workflow "
                            "now. You'll see it in Work, and Needs-you will "
                            "ring when it's ready to review.",
                            type="positive", timeout=8000,
                        )
                        _ui_for_toast.navigate.to("/work")
            except Exception:
                pass

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
#  Legacy URL redirects (Phase 6 Batch 2, 6d)
# ─────────────────────────────────────────────────────────────────────────────

def _legacy_redirect_routes() -> "list[tuple[str, str]]":
    """The single source of truth for the legacy → current URL redirects.

    Each ``(old_path, target)`` pair is registered on the ASGI app in
    ``register_routes`` as a true HTTP 3xx redirect.  Kept as a pure function
    (no NiceGUI import) so the mapping is testable headless.

    6h flipped the Shadows rename: ``/army`` -> ``/shadows`` (``/shadows`` is now
    the canonical route; ``/army`` is the legacy alias preserved for bookmarks).
    """
    return [
        ("/systemu-chat",  "/chat?tab=live"),
        ("/memory",        "/insights?tab=memory"),
        ("/flywheel",      "/insights?tab=flywheel"),
        ("/notifications", "/insights?tab=events"),
        ("/army",          "/shadows"),
    ]


def _make_redirect(target: str):
    """Build a Starlette route handler that returns a 307 redirect to ``target``.

    307 (Temporary Redirect) preserves the request method and is non-cacheable,
    so a future repoint of any legacy URL takes effect immediately (unlike a
    308/permanent which browsers may cache aggressively).
    """
    from starlette.responses import RedirectResponse

    def _redirect(request):  # noqa: ARG001 — Starlette passes the request
        return RedirectResponse(target, status_code=307)

    return _redirect


# ─────────────────────────────────────────────────────────────────────────────
#  Page route registrations
# ─────────────────────────────────────────────────────────────────────────────

def register_routes() -> None:
    """Register all NiceGUI @ui.page routes."""
    from nicegui import ui

    # Deferred imports to keep this module importable without nicegui installed
    from systemu.interface.pages.console                   import build_console_page
    from systemu.interface.pages.scrolls                   import build_scrolls_page
    from systemu.interface.pages.army                      import build_army_page
    from systemu.interface.pages.tools                     import build_tools_page
    from systemu.interface.pages.skills_page               import build_skills_page
    from systemu.interface.pages.evolutions                import build_evolutions_page
    from systemu.interface.pages.settings                  import build_settings_page
    from systemu.interface.pages.activities                import build_activities_page
    from systemu.interface.pages.shadow_memory_page        import build_shadow_memory_page
    from systemu.interface.pages.chat_page                 import build_chat_tabs
    from systemu.interface.pages.insights                  import build_insights_page
    from systemu.interface.pages.inbox_page                 import build_inbox_page
    from systemu.interface.pages.workflow_detail           import build_workflow_detail_page
    from systemu.interface.pages.work                      import build_work_page
    from systemu.interface.pages.table                     import build_table_page
    from systemu.interface.pages import recover as _recover_page_module  # noqa: F401  # registers /recover/<scope>/<id>
    from systemu.interface.pages import login as _login_page_module        # noqa: F401  # R-SEC1: registers /login

    def _redirect_to_welcome_if_needed() -> bool:
        """W11.4: funnel fresh installs to /welcome from EVERY route until
        the API key exists and the profile is saved (onboarding_gate carries
        the escape hatches: env flag, pre-W11 skip sentinel, defensive []).

        Returns True when a redirect was issued — the caller stops rendering.
        """
        try:
            from systemu.interface.dashboard_state import AppState as _ObState
            from systemu.interface.pages.welcome import onboarding_gate
            _st = _ObState.get()
            missing = onboarding_gate(getattr(_st, "vault", None),
                                      getattr(_st, "config", None))
        except Exception:
            return False
        if missing:
            ui.navigate.to("/welcome")
            return True
        return False

    @ui.page("/")
    def page_console():
        if _redirect_to_welcome_if_needed():
            return
        with _build_layout("Home", "/"):
            build_console_page()

    # ── OnTheTable (T1b: read-only inventory board) ───────────────────────
    # Spine-less like /inbox and /welcome — claims its own path, no nav
    # highlight. Reached via the header "On the table" link.
    @ui.page("/table")
    def page_table():
        if _redirect_to_welcome_if_needed():
            return
        with _build_layout("On the table", "/table"):
            build_table_page()

    # ── Welcome (W9.1: first-run onboarding wizard) ───────────────────────
    @ui.page("/welcome")
    def page_welcome():
        # Claims its own path (no spine highlight — like /inbox), so the
        # title↔nav-label contract on "/" stays single-owner.
        from systemu.interface.pages.welcome import build_welcome_page
        with _build_layout("Welcome", "/welcome"):
            build_welcome_page()

    # ── Work (Phase 5 Slice 2a: the workflow-centric Work spine page) ─────
    # /scrolls and /activities stay registered below — they fold into /work
    # via redirects in a later slice; for now only the nav repoints here.
    @ui.page("/work")
    def page_work():
        if _redirect_to_welcome_if_needed():
            return
        with _build_layout("Work", "/work"):
            build_work_page()

    @ui.page("/workflow/{workflow_id}")
    def page_workflow_detail(workflow_id: str):
        if _redirect_to_welcome_if_needed():
            return
        # Pass the REAL path so spine_of highlights the Work spine
        # (/scrolls) — this page used to claim "/" and lit Home.
        with _build_layout(f"Workflow — {workflow_id}", f"/workflow/{workflow_id}"):
            build_workflow_detail_page(workflow_id)

    @ui.page("/scrolls")
    def page_scrolls():
        if _redirect_to_welcome_if_needed():
            return
        with _build_layout("Scrolls", "/scrolls"):
            build_scrolls_page()

    @ui.page("/shadows")
    def page_shadows():
        if _redirect_to_welcome_if_needed():
            return
        # 6h: /shadows is canonical; /army now redirects here. The builder
        # (build_army_page) + the shadow_army storage key are unchanged — this
        # is a URL rename only.
        with _build_layout("Shadows", "/shadows"):
            build_army_page()

    # ── Insights (v0.7.2: tabbed parent for Memory / Flywheel / Events) ───
    @ui.page("/insights")
    def page_insights(tab: str = "memory"):
        if _redirect_to_welcome_if_needed():
            return
        # ?tab=memory|flywheel|events selects the active tab (invalid values
        # fall back to memory inside build_insights_page).
        with _build_layout("Insights", "/insights"):
            build_insights_page(default_tab=tab)

    # ── Health (R-UX1: self-diagnosis + the cross-OS capability profile) ──
    # Spine-less like /welcome — the "why is nothing happening?" page. Always
    # reachable (no welcome redirect: a broken install must be diagnosable).
    @ui.page("/health")
    def page_health():
        from systemu.interface.pages.health import build_health_page
        with _build_layout("Health", "/health"):
            build_health_page()

    @ui.page("/privacy")
    def page_privacy():
        if _redirect_to_welcome_if_needed():
            return
        # R-P3b — the honest "what leaves this machine" page (§15.7 interim rule).
        from systemu.interface.pages.privacy import build_privacy_page
        with _build_layout("Privacy", "/privacy"):
            build_privacy_page()

    @ui.page("/memory/{shadow_id}")
    def page_shadow_memory(shadow_id: str):
        if _redirect_to_welcome_if_needed():
            return
        # Per-shadow memory view stays its own page — it's deep-linked from
        # the Insights → Memory tab's "View memory" buttons.  Pass the REAL
        # path so spine_of highlights the Shadows spine (/shadows) — this page
        # used to claim "/insights".
        with _build_layout(f"Memory — {shadow_id}", f"/memory/{shadow_id}"):
            build_shadow_memory_page(shadow_id)

    @ui.page("/activities")
    def page_activities():
        if _redirect_to_welcome_if_needed():
            return
        with _build_layout("Activities", "/activities"):
            build_activities_page()

    @ui.page("/tools")
    def page_tools(forge: str = ""):
        if _redirect_to_welcome_if_needed():
            return
        # ?forge=<tool_id> deep-links to a proposed tool and auto-opens its
        # spec/code review dialog (precedent: page_insights(tab=...)).
        with _build_layout("Build", "/tools"):
            build_tools_page(forge_tool_id=forge or None)

    @ui.page("/skills")
    def page_skills():
        if _redirect_to_welcome_if_needed():
            return
        with _build_layout("Skills Registry", "/skills"):
            build_skills_page()

    # Phase 6 Slice 6f: the /workshop route is dissolved.  Its last surface —
    # the interactive Scrolls rebuild — is now an in-place dialog opened from
    # the Scrolls page (scroll_rebuild.open_scroll_rebuild_dialog).

    @ui.page("/evolutions")
    def page_evolutions():
        if _redirect_to_welcome_if_needed():
            return
        with _build_layout("Evolutions", "/evolutions"):
            build_evolutions_page()

    @ui.page("/settings")
    def page_settings():
        if _redirect_to_welcome_if_needed():
            return
        with _build_layout("Settings", "/settings"):
            build_settings_page()

    # ── Inbox (Phase 3 Batch 3: the one decisions surface — unified cards) ─
    @ui.page("/inbox")
    def page_inbox():
        if _redirect_to_welcome_if_needed():
            return
        with _build_layout("Inbox", "/inbox"):
            build_inbox_page()

    # ── Chat (v0.7.2: now tabbed — Compose + Live Events) ─────────────────
    @ui.page("/chat")
    def page_chat(tab: str = "compose", prefill: str = ""):
        if _redirect_to_welcome_if_needed():
            return
        # ?tab=compose|live selects the active tab.  /systemu-chat redirects
        # here with tab=live so legacy deep-links keep working.
        # W10.4: ?prefill= lands a starter prompt in the composer (the
        # operator still clicks Run — never auto-submitted).
        with _build_layout("Chat", "/chat"):
            build_chat_tabs(default_tab=tab, prefill=prefill)

    # ── Legacy URL redirects (Phase 6 Batch 2, 6d) ────────────────────────
    # Preserve every old top-level URL so bookmarks, notification emails, and
    # recovery panel "Fix URL" links continue to land in the right place after
    # the sidebar merges.  These are TRUE HTTP 3xx redirects (not 200 + a
    # client-side ui.navigate hop), so curl / bots / link-prefetch resolve them
    # without executing JS.  NiceGUI 3.x's ``app`` is a Starlette/FastAPI App,
    # so we register them straight on the ASGI router via ``app.add_route``.
    from nicegui import app as ng_app

    for _path, _target in _legacy_redirect_routes():
        ng_app.add_route(_path, _make_redirect(_target))

    # R-UTL1 / U-1a: the JSON task API. Registered the same way (plain Starlette
    # routes on the ASGI router) — these are NOT ui.page routes, so they carry
    # no spine and no onboarding gate; they authenticate per-request instead.
    try:
        from systemu.interface.task_api import register_task_api
        # A GETTER, not the object: the holder is re-pointed on a second
        # run_dashboard(), and capturing the state once would strand the API on
        # a stale vault. Reading the same holder the guard reads also means the
        # two fences can never disagree about which install they are protecting.
        register_task_api(ng_app, lambda: _ROUTE_GUARD_STATE[0])
    except Exception:
        logger.warning("[Dashboard] task API registration failed", exc_info=True)


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

    # W12 (audit F2): the sidebar footer displays SYSTEMU_DASHBOARD_PORT —
    # stamp the REAL serving port so custom ports don't show ":8765".
    os.environ["SYSTEMU_DASHBOARD_PORT"] = str(port)
    # v0.9.32 Item 2: stamp the canonical browser origin so spawned recorders
    # (via dispatch._dashboard_origin) drop captures of our own dashboard UI.
    # 0.0.0.0 (docker bind) is rewritten to localhost — the value the browser
    # actually loads — so the recorder's URL-origin match works.
    _origin_host = "localhost" if host in ("0.0.0.0", "::", "") else host
    os.environ["SYSTEMU_DASHBOARD_ORIGIN"] = f"http://{_origin_host}:{port}"

    # ── R-SEC1: fail-closed exposure gate + session secret ──────────────────
    # The rule (systemu.runtime.dashboard_auth.exposure_check):
    #   non-loopback bind + no passphrase  -> REFUSE to start (never bind);
    #   loopback bind + no passphrase       -> start, but WARN it's unauthenticated;
    #   any bind + passphrase configured    -> start.
    # The exposure check itself MUST run (its SystemExit must propagate);
    # only the TLS niceties below are best-effort. `_dash_storage_secret` is
    # always resolved (falls back to None) so ui.run always gets a secret when
    # the module is present.
    _vault = config.vault_dir
    _dash_storage_secret = None
    _dash_ssl_kwargs: dict = {}
    from systemu.runtime import dashboard_auth as _dash_auth
    _verdict = _dash_auth.exposure_check(host, _dash_auth.is_configured(config, _vault))
    if not _verdict.may_start:
        logger.critical("[Dashboard] %s", _verdict.reason)
        raise SystemExit(3)                         # fail-closed: never bind
    if _verdict.warn:
        logger.warning("[Dashboard] No dashboard passphrase set — the UI is UNAUTHENTICATED "
                        "(loopback only). Set SYSTEMU_DASHBOARD_PASSPHRASE_HASH or run "
                        "`systemu doctor --set-passphrase` before exposing beyond 127.0.0.1.")
    # A stable, persisted session secret so NiceGUI's per-user storage survives
    # restarts — always set, independent of the auth posture.
    try:
        _dash_storage_secret = _dash_auth.session_secret(_vault)
    except Exception:
        logger.warning("[Dashboard] could not resolve session secret", exc_info=True)
    # TLS passthrough (spec design 6): serve HTTPS when both env vars are set.
    try:
        _tls_cert = os.getenv("SYSTEMU_TLS_CERT")
        _tls_key = os.getenv("SYSTEMU_TLS_KEY")
        if _tls_cert and _tls_key:
            _dash_ssl_kwargs = {"ssl_certfile": _tls_cert, "ssl_keyfile": _tls_key}
        elif not _dash_auth.is_loopback(host):
            logger.warning(
                "[Dashboard] Binding %r over PLAINTEXT HTTP (no TLS). Set "
                "SYSTEMU_TLS_CERT and SYSTEMU_TLS_KEY to serve HTTPS, or put a "
                "TLS-terminating reverse proxy in front before exposing beyond "
                "loopback.", host,
            )
    except Exception:
        logger.debug("[Dashboard] TLS env passthrough skipped", exc_info=True)

    try:
        from nicegui import ui, app as ng_app
    except ImportError:
        logger.error(
            "[Dashboard] NiceGUI not installed. Run: pip install nicegui"
        )
        return

    # W3.1: suppress NiceGUI's benign post-navigation timer traceback spam
    # ('parent slot of the element has been deleted') — see log_filters.
    from systemu.interface.log_filters import install_nicegui_log_filters
    install_nicegui_log_filters()

    # W4.3: serve the locally-vendored fonts (Inter + JetBrains Mono, latin
    # woff2) at /assets/fonts so the @font-face rules in GLOBAL_CSS resolve
    # without a Google Fonts CDN round-trip. Registered once at startup.
    import mimetypes as _mimetypes
    import pathlib as _pathlib
    # Python's mimetypes table doesn't know woff2 → StaticFiles would serve it
    # as text/plain. Register the correct type so the Content-Type is font/woff2.
    _mimetypes.add_type("font/woff2", ".woff2")
    _fonts_dir = _pathlib.Path(__file__).parent / "assets" / "fonts"
    if _fonts_dir.is_dir():
        try:
            ng_app.add_static_files("/assets/fonts", str(_fonts_dir))
        except Exception:
            logger.warning("[Dashboard] could not register /assets/fonts static route",
                           exc_info=True)

    from systemu.interface.dashboard_state import AppState
    try:
        state = AppState.create(config)
    except Exception as exc:
        logger.error(
            "[Dashboard] AppState.create() failed — dashboard cannot start: %s",
            exc, exc_info=True,
        )
        return

    # R-SEC1: record the auth posture on the live state (advisory; never fatal).
    try:
        state.dashboard_auth_active = _verdict.require_auth
    except Exception:
        pass

    # R-SEC1: install the authentication route-guard middleware. When a
    # passphrase is configured (state.dashboard_auth_active True) EVERY route
    # requires an authenticated session; otherwise the guard is a strict no-op.
    # Registered here — after state + the fonts mount, before ui.run — so it
    # wraps the whole request chain. Never fatal: a wiring failure logs + falls
    # through (the exposure gate above already refused any unsafe bind).
    try:
        _install_route_guard(ng_app, state)
    except Exception:
        logger.warning("[Dashboard] could not install auth route guard", exc_info=True)

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
        favicon=(str(_BRAND_LOGO) if _BRAND_LOGO.exists() else "⚡"),
        dark=dark,
        reload=reload,
        show=False,          # Don't auto-open browser
        uvicorn_logging_level="warning",
        # R-SEC1: stable, persisted secret so NiceGUI's per-user storage
        # (app.storage.user) survives restarts and is signed against tampering.
        storage_secret=_dash_storage_secret,
        # v0.7.3 Bug #11 fix — default uvicorn WS max-message is 16MB but
        # starlette / NiceGUI's full-state sync can exceed this with a busy
        # vault (scrolls + tools + skills + activities all rendering). Raise
        # to 64MB so click events keep propagating after the initial sync.
        # NiceGUI passes unknown kwargs through to uvicorn.run().
        ws_max_size=64 * 1024 * 1024,
        # R-SEC1: TLS passthrough — ssl_certfile/ssl_keyfile when both env vars
        # are set (empty dict otherwise, so behaviour is unchanged by default).
        **_dash_ssl_kwargs,
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
