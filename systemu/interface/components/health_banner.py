"""v0.8.0.2: top-of-page health banner.

Runs a passive self-check on every dashboard page render and surfaces
operator-actionable warnings.  Designed to catch the four silent-failure
modes that bit us in v0.8.0.1 UAT:

  - Multiple systemu daemon processes bound to port 8765 (port race wins
    the dashboard for a leftover daemon with stale config).
  - No LLM provider usable from the daemon's environment (LLM steps
    silently fail with raw-event output). F19: this check consumes
    ``systemu.runtime.provider_status``, THE ONE MINT, so the banner cannot
    nag an operator whose Google key or running Ollama the Settings page is
    simultaneously reporting as fine.
  - Vault directory read-only (writes silently fail, dashboard goes empty).

Architecture: a pure-data helper ``build_health_state()`` that returns a
dataclass (testable without NiceGUI) plus a thin renderer
``render_health_banner()`` that paints the result.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass
class HealthIssue:
    severity: str          # "warning" | "danger"
    message:  str          # short human description
    cta:      Optional[str] = None  # one-line remediation


@dataclass
class HealthState:
    issues: List[HealthIssue] = field(default_factory=list)

    @property
    def has_any(self) -> bool:
        return bool(self.issues)

    @property
    def worst_severity(self) -> str:
        if any(i.severity == "danger" for i in self.issues):
            return "danger"
        if self.issues:
            return "warning"
        return "ok"


# -- Probes (each best-effort, never raises) ---------------------------------

def _scan_daemon_count() -> int:
    """The raw psutil scan — best-effort, 0 on error. SLOW on Windows
    (cmdline for every process ≈ 1 s); only ever call via the cache below."""
    try:
        import psutil
        count = 0
        for proc in psutil.process_iter(["cmdline"]):
            try:
                cmdline = " ".join(proc.info.get("cmdline") or [])
                if "systemu.scheduler.daemon" in cmdline:
                    count += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return count
    except Exception:
        return 0


_PROBE_TTL_S = 20.0
_daemon_probe_cache = {"ts": -1e9, "count": 0}


def _count_systemu_daemons(_now: Optional[float] = None) -> int:
    """Best-effort count of running daemon processes, TTL-cached.

    W12-B5 (audit F1): the full process scan ran on EVERY page build and was
    the single biggest page-latency cost — every route took 1.2–2.0 s
    server-side. The daemon count changes rarely; 20 s of staleness is
    harmless for a warning banner.
    """
    import time
    now = time.monotonic() if _now is None else _now
    if now - _daemon_probe_cache["ts"] < _PROBE_TTL_S:
        return _daemon_probe_cache["count"]
    count = _scan_daemon_count()
    _daemon_probe_cache["ts"] = now
    _daemon_probe_cache["count"] = count
    return count


def _provider_statuses(config=None, probe=None) -> dict:
    """Every provider's MINTED verdict (DEC-43), memoised for the render path.

    F19: this was ``_openrouter_key_present()`` — ``bool(os.environ[
    "OPENROUTER_API_KEY"])`` — so the banner nagged an operator who had a
    perfectly good Google key, or a running Ollama, to add an OpenRouter one.
    That is one of the SIX private copies of the satisfaction recipe F19 closed.

    ``build_health_state`` runs on EVERY route render, and the keyless witness
    costs ~1-2 s of loopback, so the mint's short-TTL memo is used here for the
    same reason ``_count_systemu_daemons`` has one. A memo replays a verdict
    that was genuinely observed; it never invents one, and an expired entry
    re-probes rather than decaying to green.
    """
    from systemu.runtime import provider_status as _ps
    try:
        if config is None:
            # Its OWN try: before F19 an AppState that is not up yet (early
            # boot, a unit call) would abort the whole lookup and the banner
            # would report "no provider" for a machine that plainly had one.
            try:
                from systemu.interface.dashboard_state import AppState
                config = getattr(AppState.get(), "config", None)
            except Exception:
                config = None
        if config is None:
            from sharing_on.config import Config
            config = Config.from_env()
        # THE DAEMON'S ENVIRONMENT is what this banner is about, and the
        # predicate it replaces read `os.environ` directly. `env_overlay` keeps
        # that reach: a long-lived AppState config snapshot can be older than
        # the .env the operator just edited, and the banner must not report a
        # provider as absent because the snapshot predates it.
        return _ps.all_provider_statuses(_ps.env_overlay(config), probe=probe,
                                         cache_ttl_s=_ps.PROBE_CACHE_TTL_S)
    except Exception:
        return {}


def _vault_writable(vault_dir: Optional[Path]) -> bool:
    if vault_dir is None:
        return True
    try:
        test_file = vault_dir / ".health_write_check"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink()
        return True
    except Exception:
        return False


def _lockout_degraded() -> tuple:
    """F7: lockout stores that cannot persist (dashboard_auth's registry).

    Best-effort -- never raises. An empty tuple means brute-force protection is
    fully durable.
    """
    try:
        from systemu.runtime.dashboard_auth import lockout_degradations
        return lockout_degradations()
    except Exception:
        return ()


def _storage_degraded() -> Optional[dict]:
    """The storage-degradation marker set by AppState._degraded_fallback (W3.3),
    or None. Best-effort — never raises if AppState isn't ready."""
    try:
        from systemu.interface.dashboard_state import AppState
        return getattr(AppState.get(), "storage_degraded", None)
    except Exception:
        return None


# -- Pure-data state builder (testable) --------------------------------------

def build_health_state(vault_dir: Optional[Path] = None, *, config=None,
                       provider_probe=None) -> HealthState:
    """Compute the current health state.  No UI, no side-effects."""
    state = HealthState()

    daemon_count = _count_systemu_daemons()
    if daemon_count > 1:
        state.issues.append(HealthIssue(
            severity="danger",
            message=(
                f"{daemon_count} systemu daemon processes are running. "
                "Whichever wins the port race will serve this dashboard, "
                "and recordings or decisions may land in the wrong vault."
            ),
            cta="systemu daemon stop --all",
        ))

    from systemu.runtime import provider_status as _ps
    _statuses = _provider_statuses(config, provider_probe)
    if not _ps.any_satisfied(_statuses):
        state.issues.append(HealthIssue(
            severity="warning",
            message=(
                "No LLM provider is usable from the daemon's environment. "
                "LLM-driven steps (capture analysis, scroll refinement) will "
                "fail silently and you'll only get raw captured events."
            ),
            cta=_ps.configure_hint(_statuses) + " Then restart the daemon.",
        ))

    if vault_dir is not None and not _vault_writable(vault_dir):
        state.issues.append(HealthIssue(
            severity="danger",
            message=f"Vault directory {vault_dir} is not writable.",
            cta="Check disk space and file permissions on the vault directory.",
        ))

    # W3.3: a requested non-file backend that silently downgraded to the file
    # vault is a data-split hazard — surface it loudly, never just in the log.
    deg = _storage_degraded()
    if deg:
        req = deg.get("requested", "configured")
        state.issues.append(HealthIssue(
            severity="danger",
            message=(f"Storage DEGRADED: the {req} backend was unavailable "
                     f"({deg.get('reason', 'unknown')}) — running on the local file "
                     f"vault. Records written now will NOT be in your {req} store."),
            cta=f"Fix the {req} connection/config and restart the daemon.",
        ))

    # F7: a lockout store that cannot be written counts failed logins in memory
    # only. Protection is still ENFORCED, but it resets on restart -- and the
    # operator must learn that here, not from a buried warning in the daemon log
    # (which is exactly where the old silent fail-open went).
    for deg in _lockout_degraded():
        state.issues.append(HealthIssue(
            severity="danger",
            message=(
                "Dashboard brute-force protection is DEGRADED: the login lockout "
                f"store {deg.get('path', '?')} cannot be written "
                f"({deg.get('reason', 'unknown')}). Failed logins are still being "
                "counted and lockouts still apply, but the counter is in memory "
                "only and resets if the dashboard restarts."
            ),
            cta="Fix permissions/disk space on the vault secrets directory, "
                "then restart the dashboard.",
        ))

    return state


# -- NiceGUI renderer (thin wrapper) -----------------------------------------

def render_health_banner(vault_dir: Optional[Path] = None) -> None:
    """Paint the banner if there are any health issues.  Silent when healthy."""
    from nicegui import ui
    from systemu.interface.dashboard_state import THEME

    state = build_health_state(vault_dir)
    if not state.has_any:
        return  # quiet when healthy

    color = THEME.get("danger", "#ef4444") if state.worst_severity == "danger" else THEME.get("warning", "#f59e0b")
    with ui.row().style(
        f"background: {color}22; "                  # ~13% opacity tint
        f"border-left: 4px solid {color}; "
        f"padding: 12px 20px; margin-bottom: 16px; "
        f"border-radius: 8px; width: 100%;"
    ):
        with ui.column().style("gap: 8px; width: 100%;"):
            for issue in state.issues:
                with ui.row().style("align-items: flex-start; gap: 10px;"):
                    icon = "WARNING" if issue.severity == "warning" else "DANGER"
                    ui.label(icon).style(
                        f"color: {color}; font-size: 11px; font-weight: 700; "
                        f"letter-spacing: 0.08em; padding-top: 2px;"
                    )
                    with ui.column().style("gap: 2px;"):
                        ui.label(issue.message).style(
                            f"color: {THEME['text']}; font-size: 13px; font-weight: 500; line-height: 1.4;"
                        )
                        if issue.cta:
                            ui.label(issue.cta).style(
                                f"color: {THEME['text_muted']}; font-size: 12px; "
                                f"font-family: monospace; line-height: 1.4;"
                            )
