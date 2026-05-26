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

def _count_systemu_daemons() -> int:
    """Best-effort count of running daemon processes.  Returns 0 on error."""
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
