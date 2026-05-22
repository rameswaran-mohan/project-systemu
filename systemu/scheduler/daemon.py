"""Systemu background daemon — APScheduler-based.

Runs two recurring jobs:
  • Hourly  shadow sweep (re-evaluate unassigned activities)
  • Daily   evolution check (propose vault improvements)

Also serves as the host process for the NiceGUI web dashboard (Phase S5).

Usage:
  sharing_on daemon start [--port 8765]
  sharing_on daemon stop
  sharing_on daemon status
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# PID file lives in the vault's parent directory
_PID_FILE_NAME = ".systemu_daemon.pid"


def _pid_file_path(vault_dir: str) -> Path:
    return Path(vault_dir).parent / _PID_FILE_NAME


# ─────────────────────────────────────────────────────────────────────────────
#  Public commands
# ─────────────────────────────────────────────────────────────────────────────

def start_daemon(
    vault_dir: str,
    config,
    vault,
    *,
    port: int = 8765,
    foreground: bool = False,
) -> None:
    """Start the Systemu daemon.

    If foreground=True, runs in the current process (used for debugging).
    Otherwise spawns a detached subprocess.
    """
    pid_file = _pid_file_path(vault_dir)

    if foreground:
        # In foreground mode (Docker / direct supervisor) the process manager
        # guarantees single-instance semantics — the container will not start a
        # second copy.  PID files are unreliable across container restarts:
        # PIDs (especially PID 1) are reused in the new PID namespace, so a
        # stale file from a SIGKILL'd prior container always looks "live".
        # Remove any leftover file and start unconditionally.
        pid_file.unlink(missing_ok=True)
        _run_daemon_loop(config, vault, port, pid_file)
        return

    # ── Background mode: guard against a second instance ─────────────────────
    if pid_file.exists():
        existing_pid = pid_file.read_text().strip()
        status = get_status(vault_dir)   # also cleans up the PID file if dead
        if status["running"]:
            logger.warning("[Daemon] Already running (PID %s). Stop it first.", existing_pid)
            return
        logger.info(
            "[Daemon] Stale PID file (PID %s no longer running) — starting fresh",
            existing_pid,
        )
        # get_status() already removed the PID file; nothing more to do here.

    # Spawn as a detached subprocess
    cmd = [
        sys.executable, "-m", "systemu.scheduler.daemon",
        "--vault-dir", vault_dir,
        "--port", str(port),
    ]
    log_file = open(Path(vault_dir) / "daemon.log", "a", encoding="utf-8")
    import subprocess
    import os
    import systemu

    project_root = Path(systemu.__file__).parent.parent.absolute()
    env = os.environ.copy()
    env["PYTHONPATH"] = str(project_root)

    proc = subprocess.Popen(
        cmd,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        close_fds=True,
        start_new_session=True,
        cwd=str(project_root),
        env=env,
    )
    pid_file.write_text(str(proc.pid))
    logger.info("[Daemon] Started as background process PID %d", proc.pid)


def stop_daemon(vault_dir: str) -> bool:
    """Send SIGTERM to the running daemon. Returns True if stopped."""
    pid_file = _pid_file_path(vault_dir)
    if not pid_file.exists():
        return False

    pid = int(pid_file.read_text().strip())
    try:
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.kernel32.TerminateProcess(  # type: ignore[attr-defined]
                ctypes.windll.kernel32.OpenProcess(1, False, pid), 0  # type: ignore[attr-defined]
            )
        else:
            os.kill(pid, signal.SIGTERM)
        pid_file.unlink(missing_ok=True)
        logger.info("[Daemon] Stopped PID %d", pid)
        return True
    except (ProcessLookupError, PermissionError) as exc:
        logger.warning("[Daemon] Could not stop PID %d: %s", pid, exc)
        pid_file.unlink(missing_ok=True)
        return False


def get_status(vault_dir: str) -> dict:
    """Return daemon status dict: {running, pid}."""
    pid_file = _pid_file_path(vault_dir)
    if not pid_file.exists():
        return {"running": False, "pid": None}

    pid = int(pid_file.read_text().strip())
    # Check if process is actually alive
    try:
        if sys.platform == "win32":
            import ctypes
            # 0x1000 = PROCESS_QUERY_LIMITED_INFORMATION
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if handle == 0:
                raise ProcessLookupError()
            
            exit_code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            ctypes.windll.kernel32.CloseHandle(handle)
            
            # 259 = STILL_ACTIVE
            if exit_code.value != 259:
                raise ProcessLookupError()
        else:
            os.kill(pid, 0)   # signal 0 = no-op, raises if not running
            
        return {"running": True, "pid": pid}
    except (ProcessLookupError, PermissionError, OSError):
        pid_file.unlink(missing_ok=True)
        return {"running": False, "pid": None}


# ─────────────────────────────────────────────────────────────────────────────
#  Daemon loop
# ─────────────────────────────────────────────────────────────────────────────

def _run_daemon_loop(config, vault, port: int, pid_file: Path) -> None:
    """Main daemon loop — runs APScheduler jobs."""
    # Daemon runs headless — no TTY, no interactive prompts.
    # notify_user() checks this flag and auto-selects the first action
    # instead of blocking forever waiting for terminal input.
    os.environ["SYSTEMU_HEADLESS"] = "1"

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except ImportError:
        logger.error(
            "[Daemon] APScheduler not installed. Run: pip install apscheduler"
        )
        sys.exit(1)

    from systemu.scheduler.jobs import (
        init_jobs, set_scheduler,
        hourly_shadow_sweep, daily_evolution_check, consolidate_shadow_memory,
        startup_recovery_sweep,
    )

    # Write PID
    pid_file.write_text(str(os.getpid()))
    atexit.register(lambda: pid_file.unlink(missing_ok=True))

    # / v0.3.5 — Record interpreter invariant + optional pre-warm.
    # Both are best-effort: failures here must never crash daemon boot.
    try:
        from systemu.runtime.interpreter_check import record_interpreter
        record_interpreter(Path("data"), recorded_by="daemon")
    except Exception:
        logger.debug("[Daemon] interpreter record failed", exc_info=True)

    if getattr(config, "prewarm_tool_deps", False):
        try:
            _prewarm_tool_deps(config, vault)
        except Exception:
            logger.exception("[Daemon] tool-dep pre-warm failed — continuing boot")

    # Create AppState FIRST so the scheduler jobs and the dashboard both use
    # the same vault backend (selected by SYSTEMU_STORAGE).  Without this,
    # the CLI and scheduler would write to the file vault while the dashboard
    # reads from SQLite — producing an empty UI even when data exists.
    try:
        from systemu.interface.dashboard_state import AppState
        state = AppState.create(config)
        vault = state.vault   # override the file-based vault from __main__
        logger.info("[Daemon] Vault unified with AppState backend")
    except Exception as exc:
        logger.warning(
            "[Daemon] AppState pre-creation failed (%s) — using file vault for scheduler", exc
        )
        # vault remains the raw Vault(args.vault_dir) passed in — degraded mode

    # Initialise jobs (sets module-level config/vault globals)
    init_jobs(config, vault)

    # seed tool_dep_approvals from the baked requirements file.
    # Best-effort; never crash the daemon if the file is malformed or the
    # DB driver isn't installed for the configured backend.
    try:
        database_url = os.environ.get("SYSTEMU_DATABASE_URL")
        if database_url:
            from systemu.storage.sqlite.vault import seed_tool_dep_approvals
            reqs = Path(os.environ.get(
                "SYSTEMU_TOOLS_REQUIREMENTS", "tools/requirements-tools.txt"
            ))
            seeded = seed_tool_dep_approvals(
                database_url=database_url, requirements_path=reqs
            )
            if seeded:
                logger.info(
                    "[Daemon] Seeded %d tool dep approval(s) from %s", seeded, reqs
                )
    except Exception:
        logger.exception("[Daemon] tool dep approval seeding failed — continuing boot")

    # migrate skills to Anthropic Agent Skills spec-conformant layout.
    # Idempotent; best-effort.  Runs once at boot to fix legacy skill_skill_<hash>/
    # directories on operator upgrade.
    try:
        from systemu.storage.skill_migrator import migrate_skill_layout
        vault_dir = Path(os.environ.get("SYSTEMU_VAULT_DIR", "systemu/vault"))
        report = migrate_skill_layout(vault_dir)
        if report.migrated:
            logger.info(
                "[Daemon] migrated %d skill(s) to spec-conformant layout "
                "(skipped %d already-conformant, %d collisions)",
                report.migrated, report.skipped, report.collisions,
            )
        elif report.skipped:
            logger.info(
                "[Daemon] all %d skill(s) already spec-conformant",
                report.skipped,
            )
        if report.errors:
            for err in report.errors:
                logger.warning("[Daemon] skill migration error: %s", err)
    except Exception:
        logger.exception("[Daemon] skill_migrator failed — continuing boot")

    # Build scheduler
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        hourly_shadow_sweep,
        trigger="interval",
        hours=1,
        id="shadow_sweep",
        name="Hourly Shadow Sweep",
        replace_existing=True,
    )
    scheduler.add_job(
        consolidate_shadow_memory,
        trigger="cron",
        hour=2,     # 02:00 UTC — reflective pass before evolution
        minute=0,
        id="memory_consolidation",
        name="Daily Memory Consolidation",
        replace_existing=True,
    )
    scheduler.add_job(
        daily_evolution_check,
        trigger="cron",
        hour=3,     # 03:00 UTC — evolution runs after memory is consolidated
        minute=0,
        id="evolution_check",
        name="Daily Evolution Check",
        replace_existing=True,
    )

    scheduler.start()

    # One-shot recovery sweep — fires 5 seconds after startup to heal any
    # pipeline states left incomplete by a prior crash.
    from datetime import datetime, timedelta
    scheduler.add_job(
        startup_recovery_sweep,
        trigger="date",
        run_date=datetime.now() + timedelta(seconds=5),
        id="startup_recovery",
        name="Startup Recovery Sweep",
    )

    # Share the live scheduler instance with the dashboard page
    set_scheduler(scheduler)

    logger.info("[Daemon] Scheduler started. PID=%d | Port=%d", os.getpid(), port)
    logger.info(
        "[Daemon] Jobs: shadow sweep (hourly) | memory consolidation (02:00) | evolution check (03:00)",
    )

    # ── Start NiceGUI dashboard in a background thread ─────────────────────
    try:
        from systemu.interface.dashboard import run_dashboard_thread
        run_dashboard_thread(config, port=port)
        logger.info("[Daemon] Dashboard thread launched on http://127.0.0.1:%d", port)
    except Exception as exc:
        logger.warning("[Daemon] Dashboard launch failed (non-fatal): %s", exc)


    # Graceful shutdown on SIGTERM / SIGINT
    def _shutdown(signum, frame):
        logger.info("[Daemon] Received signal %d — shutting down ...", signum)
        scheduler.shutdown(wait=False)
        pid_file.unlink(missing_ok=True)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    if sys.platform != "win32":
        signal.signal(signal.SIGINT, _shutdown)

    # Keep-alive loop
    try:
        while True:
            time.sleep(30)
    except KeyboardInterrupt:
        scheduler.shutdown(wait=False)


def _prewarm_tool_deps(config, vault) -> None:
    """Install all approved tool deps at daemon start.

    Walks the vault's enabled tools, gathers each tool's manifest
    ``dependencies``, and asks the installer to satisfy every one.  The
    installer's per-package cache then short-circuits the first runtime
    call to each tool — turning a 1–30s first-use latency into ~0ms.

    Opt-in via ``config.prewarm_tool_deps`` (env var
    ``SYSTEMU_PREWARM_TOOL_DEPS=true``).  Off by default so cold-start
    stays fast in dev.

    Honours the resolved InstallMode: in OFF mode the installer
    no-ops; in PROMPT mode only approved deps install.  Either way
    the daemon boot continues even when pre-warm partially fails.
    """
    from systemu.runtime.dependency_installer import (
        ensure_satisfied,
        resolve_install_mode,
    )
    from systemu.runtime.dep_approvals import init_default_store
    from systemu.runtime.dep_conflicts import find_conflicts

    tools = vault.load_index("tools") or []
    enabled = [t for t in tools if t.get("enabled")]
    if not enabled:
        logger.info("[Daemon] pre-warm: no enabled tools — skipping")
        return

    conflicts = find_conflicts(enabled)
    if conflicts:
        logger.warning(
            "[Daemon] pre-warm: %d cross-tool dep conflict(s) detected — "
            "installs may produce unexpected versions. Run "
            "`sharing_on tools deps doctor` for details.",
            len(conflicts),
        )

    all_deps: list[str] = []
    seen: set[str] = set()
    for t in enabled:
        for dep in (t.get("dependencies") or []):
            if dep not in seen:
                seen.add(dep)
                all_deps.append(dep)
    if not all_deps:
        logger.info("[Daemon] pre-warm: no dependencies declared by enabled tools")
        return

    mode      = resolve_install_mode(
        config_mode=getattr(config, "tool_dep_install_mode", None),
        systemu_mode=getattr(config, "systemu_mode", None),
    )
    approvals = init_default_store(Path("data"))
    logger.info(
        "[Daemon] pre-warm: ensuring %d dep(s) for %d tool(s) (mode=%s)",
        len(all_deps), len(enabled), mode.value,
    )
    result = ensure_satisfied(
        all_deps,
        mode=mode,
        approvals=approvals,
        tool_name="<daemon-prewarm>",
    )
    if result.ok:
        if result.installed_now:
            logger.info("[Daemon] pre-warm: installed %s", result.installed_now)
        else:
            logger.info("[Daemon] pre-warm: all deps already satisfied")
    else:
        logger.warning(
            "[Daemon] pre-warm: did not complete (%s) — %s",
            result.status.value, result.error,
        )


# ─────────────────────────────────────────────────────────────────────────────
#  Entry point when run as __main__ (spawned by subprocess)
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    from sharing_on.config import Config
    from systemu.vault.vault import Vault

    parser = argparse.ArgumentParser(description="Systemu background daemon")
    parser.add_argument("--vault-dir", required=True)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    import logging
    import logging.handlers

    log_file_path = str(Path(args.vault_dir) / "systemu_exec.log")

    # ── Format ────────────────────────────────────────────────────────────────
    fmt = logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    # ── Stream handler (terminal) — INFO+ ─────────────────────────────────────
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.INFO)
    sh.setFormatter(fmt)
    root_logger.addHandler(sh)

    # ── Rotating file handler — DEBUG+ (full execution history) ───────────────
    fh = logging.handlers.RotatingFileHandler(
        log_file_path,
        maxBytes=10 * 1024 * 1024,   # 10 MB per file
        backupCount=5,                # Keep 5 rotated files
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    root_logger.addHandler(fh)

    # ── Module-level overrides ────────────────────────────────────────────────
    for mod in ("systemu", "sharing_on"):
        logging.getLogger(mod).setLevel(logging.DEBUG)

    # ── Suppress noisy third-party loggers ───────────────────────────────────
    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("nicegui").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    logging.getLogger("systemu").info(
        "[Daemon] Logging configured — stdout: INFO+ | file: DEBUG+ | log: %s", log_file_path
    )

    _config = Config.from_env()
    _vault = Vault(args.vault_dir)
    pid_file = _pid_file_path(args.vault_dir)

    _run_daemon_loop(_config, _vault, args.port, pid_file)
