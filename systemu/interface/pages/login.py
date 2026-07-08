"""R-SEC1 — dashboard login page (/login).

A brand-styled passphrase form. Rendered whenever the route guard redirects an
unauthenticated browser (see ``dashboard._install_route_guard``). This page is
on the guard's allowlist so it is always reachable pre-auth.

Auth flow (all against the pure ``dashboard_auth`` core):
  * resolve the client IP (``ui.context.client.request.client.host``);
  * build a per-vault ``LockoutStore``;
  * if the IP is locked out → refuse WITHOUT checking the passphrase (so a
    brute-forcer cannot slip a correct guess in during the window);
  * else ``verify(pw, stored_hash)``: on success clear the lockout, mark the
    session ``authed`` + rotate a session marker, and navigate home; on failure
    record the failure, write an audit log line, and show a generic error.

The decision logic lives in the pure ``_attempt_login`` helper so it is unit-
testable without a live NiceGUI client (see tests/test_rsec1_route_guard.py).
"""
from __future__ import annotations

import logging
import secrets
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

_LOCKOUT_FILENAME = "dashboard_lockout.json"


# --------------------------------------------------------------------------- #
# pure auth-attempt core (no NiceGUI — unit-testable)
# --------------------------------------------------------------------------- #

def _attempt_login(pw: str, stored_hash, lockout, ip: str) -> Tuple[bool, str]:
    """Decide the outcome of one login attempt.

    Returns ``(ok, reason)`` where ``reason`` is one of:
      * ``"locked"``   — the IP is locked out; the passphrase was NOT checked;
      * ``"ok"``       — verified; the lockout counter was cleared;
      * ``"incorrect"``— wrong passphrase (or none configured); failure recorded.

    Lockout is checked BEFORE the passphrase so brute-force cannot land a hit
    inside the lockout window. Both the per-IP lockout AND the global lockout
    (a distributed spray across many IPs) short-circuit the attempt.
    """
    from systemu.runtime import dashboard_auth as da

    if lockout.is_locked(ip) or lockout.is_globally_locked():
        return False, "locked"
    if stored_hash and da.verify(pw, stored_hash):
        lockout.record_success(ip)
        return True, "ok"
    lockout.record_failure(ip)
    return False, "incorrect"


# --------------------------------------------------------------------------- #
# NiceGUI helpers
# --------------------------------------------------------------------------- #

def _vault():
    from systemu.interface.dashboard_state import AppState
    return AppState.get().vault


def _lockout_store(vault):
    from systemu.runtime.dashboard_auth import LockoutStore
    return LockoutStore(Path(vault) / "secrets" / _LOCKOUT_FILENAME)


def _client_ip() -> str:
    """Best-effort client IP for the current request. Falls back to a stable
    sentinel so lockout still functions (per-sentinel) if the IP is unavailable."""
    try:
        from nicegui import ui
        req = ui.context.client.request
        host = getattr(getattr(req, "client", None), "host", None)
        if host:
            return str(host)
    except Exception:
        pass
    return "unknown"


def logout() -> None:
    """Clear the authenticated session. Wired into the sidebar user menu (see
    dashboard._build_layout); also callable directly."""
    try:
        from nicegui import app as ng_app, ui
        ng_app.storage.user["authed"] = False
        ng_app.storage.user.pop("_sid", None)
        ui.navigate.to("/login")
    except Exception:
        logger.warning("[Dashboard] logout failed", exc_info=True)


def _register_login_page() -> None:
    """Register the ``/login`` @ui.page. Idempotent-ish: NiceGUI tolerates a
    single registration per path; call once from run_dashboard/register_routes."""
    from nicegui import ui, app as ng_app

    @ui.page("/login")
    def login_page():
        from systemu.interface.dashboard_state import GLOBAL_CSS
        ui.add_css(GLOBAL_CSS)

        vault = _vault()
        # Finding A: resolve the ACTIVE hash — env var (the Docker path) takes
        # precedence, else the vault-stored hash. Resolving ONLY via the vault
        # made env-only config (guard armed, no vault file) permanently
        # unloginable: the gate and the login page must agree on WHAT to verify.
        from systemu.runtime.dashboard_auth import get_active_passphrase_hash
        stored_hash = get_active_passphrase_hash(None, vault)
        lockout = _lockout_store(vault)

        # Centered brand card.
        with ui.column().style(
            "width: 100vw; min-height: 100vh; align-items: center; "
            "justify-content: center; background: transparent;"
        ):
            with ui.card().classes("s-card").style(
                "width: min(92vw, 380px); padding: 32px; gap: 16px;"
            ):
                ui.label("Systemu").classes("s-page-title").style("text-align: center;")
                ui.label("Enter your dashboard passphrase").classes("s-muted").style(
                    "text-align: center; font-size: 13px;"
                )

                pw_input = ui.input(
                    "Passphrase", password=True, password_toggle_button=True
                ).props("outlined dense autofocus").style("width: 100%;")

                msg = ui.label("").classes("s-text-danger").style(
                    "min-height: 18px; font-size: 13px; text-align: center;"
                )

                def _submit() -> None:
                    ip = _client_ip()
                    pw = pw_input.value or ""
                    ok, reason = _attempt_login(pw, stored_hash, lockout, ip)
                    if ok:
                        ng_app.storage.user["authed"] = True
                        # Rotate a session marker so a stale cookie can't be replayed
                        # after a re-auth.
                        ng_app.storage.user["_sid"] = secrets.token_hex(8)
                        ui.navigate.to("/")
                        return
                    if reason == "locked":
                        msg.set_text("Too many attempts — locked out. Try again later.")
                    else:
                        # Generic message (never reveal whether the IP is the
                        # issue vs. the passphrase). Audit the failed attempt.
                        logger.warning(
                            "[Dashboard] failed login attempt from %s", ip
                        )
                        msg.set_text("Incorrect passphrase.")
                    pw_input.set_value("")

                pw_input.on("keydown.enter", lambda _e: _submit())
                ui.button("Sign in", on_click=_submit).classes(
                    "s-btn s-btn--primary"
                ).props("unelevated").style("width: 100%;")


# Register on import so simply importing this module wires the /login route
# (mirrors pages/recover.py, which registers via a module-level @ui.page).
try:  # pragma: no cover - only skips when nicegui is genuinely absent
    _register_login_page()
except Exception:  # pragma: no cover
    logger.debug("[Dashboard] /login page registration deferred", exc_info=True)
