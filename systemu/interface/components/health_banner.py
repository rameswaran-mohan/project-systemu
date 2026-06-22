"""v0.8.0.2: top-of-page health banner.

Runs a passive self-check on every dashboard page render and surfaces
operator-actionable warnings.  Designed to catch the four silent-failure
modes that bit us in v0.8.0.1 UAT:

  - Multiple systemu daemon processes bound to port 8765 (port race wins
    the dashboard for a leftover daemon with stale config).
  - OPENROUTER_API_KEY missing from the daemon's environment (LLM steps
    silently fail with raw-event output).
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


def _openrouter_key_present() -> bool:
    return bool(os.environ.get("OPENROUTER_API_KEY", "").strip())


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


def _storage_degraded() -> Optional[dict]:
    """The storage-degradation marker set by AppState._degraded_fallback (W3.3),
    or None. Best-effort — never raises if AppState isn't ready."""
    try:
        from systemu.interface.dashboard_state import AppState
        return getattr(AppState.get(), "storage_degraded", None)
    except Exception:
        return None


# -- Pure-data state builder (testable) --------------------------------------

def build_health_state(vault_dir: Optional[Path] = None) -> HealthState:
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
            cta="sharing_on daemon stop --all",
        ))

    if not _openrouter_key_present():
        state.issues.append(HealthIssue(
            severity="warning",
            message=(
                "OPENROUTER_API_KEY is not set in the daemon's environment. "
                "LLM-driven steps (capture analysis, scroll refinement) will "
                "fail silently and you'll only get raw captured events."
            ),
            cta="Add OPENROUTER_API_KEY=... to .env and restart the daemon.",
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
