"""v0.8.0.2: top-of-page health banner.

Runs a passive self-check on every dashboard page render and surfaces
operator-actionable warnings.  Designed to catch the four silent-failure
modes that bit us in v0.8.0.1 UAT:

  - Multiple systemu daemon processes bound to THE SAME port (port race wins
    the dashboard for a leftover daemon with stale config).

    v0.10.26: that check fired on a machine-wide COUNT, so two daemons on
    DIFFERENT ports -- the operator's on 8765, an isolated test daemon on 8901,
    each with its own vault root -- were reported as a port race whose
    "recordings or decisions may land in the wrong vault", with `stop --all` as
    the remedy. The count was true; the diagnosis was not, and the remedy would
    have stopped the healthy daemon too. Detection stays machine-wide; the
    DIAGNOSIS is now port-aware, and fail-closed: the softer WARNING is
    reachable only when every sibling's port is KNOWN and all are DISTINCT.
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

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


@dataclass
class HealthIssue:
    severity: str          # "warning" | "danger"
    message:  str          # short human description
    cta:      Optional[str] = None  # one-line remediation


@dataclass(frozen=True)
class DaemonProcess:
    """What we could establish about ONE running systemu daemon.

    Every field is optional because every field comes from a best-effort probe
    of somebody else's process. ``port is None`` is a first-class answer -- it
    means "we could not determine it", which the banner treats as the dangerous
    case rather than assuming the default.
    """

    pid:     Optional[int] = None
    port:    Optional[int] = None
    vault:   Optional[str] = None
    cwd:     Optional[str] = None   # the folder `systemu daemon stop` must run in
    is_self: bool = False           # this process: the one serving this page


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

#: The marker that identifies a systemu daemon in a process command line.
_DAEMON_CMDLINE_MARKER = "systemu.scheduler.daemon"


def _port_from_text(text) -> Optional[int]:
    """A port as an int, or None when the text does not spell one.

    DEC-36: the concrete type is pinned in THIS frame before anything is done
    with it. The value arrives from another process's command line or from a
    file on disk, so ``int(text)`` on trust is how a nonsense port becomes a
    confident claim.
    """
    if type(text) is not str:
        return None
    stripped = text.strip()
    if not stripped.isdigit():
        return None
    try:
        value = int(stripped)
    except ValueError:
        return None
    return value if 0 < value <= 65535 else None


def parse_daemon_argv(cmdline) -> Tuple[Optional[int], Optional[str]]:
    """``(port, vault_root)`` as a daemon was LAUNCHED, or None for either.

    ``start_daemon`` spawns ``python -m systemu.scheduler.daemon --vault-dir X
    --port N`` and the child's own argparse makes ``--vault-dir`` required, so a
    daemon started the supported way carries both facts in its own argv -- which
    is readable for every process on the machine, not just ours.

    It NEVER guesses. A missing or unparseable ``--port`` comes back None, and
    the banner treats that as the dangerous case. Falling back to
    ``DEFAULT_DASHBOARD_PORT`` here would manufacture the very collision this
    function exists to rule out.
    """
    if type(cmdline) is not list and type(cmdline) is not tuple:
        return None, None
    args = [a for a in cmdline if type(a) is str]
    port: Optional[int] = None
    vault: Optional[str] = None
    for idx, arg in enumerate(args):
        following = args[idx + 1] if idx + 1 < len(args) else None
        if arg == "--port":
            port = _port_from_text(following)
        elif arg.startswith("--port="):
            port = _port_from_text(arg[len("--port="):])
        elif arg == "--vault-dir":
            vault = following if type(following) is str and following else None
        elif arg.startswith("--vault-dir="):
            tail = arg[len("--vault-dir="):]
            vault = tail if tail else None
    return port, vault


def _runtime_sidecar_name() -> str:
    """The daemon runtime sidecar's filename, from the module that writes it."""
    try:
        from systemu.scheduler.daemon import _RUNTIME_FILE_NAME
        if type(_RUNTIME_FILE_NAME) is str and _RUNTIME_FILE_NAME:
            return _RUNTIME_FILE_NAME
    except Exception:
        pass
    return ".systemu_daemon.json"


def _sidecar_candidates(vault: Optional[str], cwd: Optional[str]) -> list:
    """Where a daemon's runtime sidecar would live, given what we know of it."""
    name = _runtime_sidecar_name()
    out = []
    try:
        if type(vault) is str and vault:
            # daemon._runtime_file_path: the sidecar sits beside the vault dir
            out.append(Path(vault).parent / name)
        if type(cwd) is str and cwd:
            # the default layout: <operating home>/systemu/vault
            from systemu.runtime.vault_root import DEFAULT_RELATIVE_VAULT
            out.append((Path(cwd) / DEFAULT_RELATIVE_VAULT).parent / name)
    except Exception:
        return out
    return out


def _recorded_port(vault: Optional[str], cwd: Optional[str],
                   pid: Optional[int]) -> Optional[int]:
    """A daemon's port from its OWN runtime sidecar, or None.

    Accepted only when the sidecar's recorded pid IS the process we are asking
    about. A sidecar is a file any process may have left in any shape, and a
    stale one from a dead daemon that used the same vault would otherwise lend
    its port to a live stranger -- inventing a collision, or hiding one.
    """
    if type(pid) is not int:
        return None
    for candidate in _sidecar_candidates(vault, cwd):
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except Exception:
            continue
        if type(data) is not dict:
            continue
        recorded_pid = data.get("pid")
        if type(recorded_pid) is not int or recorded_pid != pid:
            continue
        recorded_port = data.get("port")
        if type(recorded_port) is int and 0 < recorded_port <= 65535:
            return recorded_port
    return None


def _scan_daemon_processes() -> tuple:
    """The raw psutil scan — best-effort, empty on error. SLOW on Windows
    (cmdline for every process ≈ 1 s); only ever call via the cache below.

    Returns one :class:`DaemonProcess` per daemon found, carrying whatever of
    (port, vault, cwd) that daemon's own launch line or sidecar could establish.
    """
    try:
        import psutil
    except Exception:
        return ()
    self_pid = os.getpid()
    found = []
    try:
        for proc in psutil.process_iter(["pid", "cmdline"]):
            try:
                info = proc.info if type(proc.info) is dict else {}
                cmdline = info.get("cmdline")
                if type(cmdline) is not list:
                    continue
                joined = " ".join(a for a in cmdline if type(a) is str)
                if _DAEMON_CMDLINE_MARKER not in joined:
                    continue
                raw_pid = info.get("pid")
                pid = raw_pid if type(raw_pid) is int else None
                port, vault = parse_daemon_argv(cmdline)
                try:
                    cwd = proc.cwd()
                except Exception:
                    cwd = None
                if type(cwd) is not str or not cwd:
                    cwd = None
                if port is None:
                    port = _recorded_port(vault, cwd, pid)
                found.append(DaemonProcess(
                    pid=pid, port=port, vault=vault, cwd=cwd,
                    # build_health_state runs inside the daemon thread that is
                    # serving this very page (daemon.py -> run_dashboard_thread),
                    # so our own pid names the daemon the operator must KEEP.
                    is_self=(pid is not None and pid == self_pid),
                ))
            except Exception:
                continue
    except Exception:
        return ()
    return tuple(found)


def _scan_daemon_count() -> int:
    """How many daemons are running. Kept as its own seam: machine-wide
    detection is what the banner has always rested on, and it is UNCHANGED.

    Derived from the same records the diagnosis uses so one psutil pass answers
    both -- and so the count and the records can never disagree about a daemon
    that started or died between two scans.
    """
    return len(_daemon_processes())


_PROBE_TTL_S = 20.0
_daemon_probe_cache = {"ts": -1e9, "count": 0}
_daemon_procs_cache: dict = {"ts": -1e9, "procs": ()}


def _daemon_processes(_now: Optional[float] = None) -> tuple:
    """The per-daemon records, TTL-cached alongside the count (W12-B5).

    Production reaches the psutil scan ONCE per TTL: ``_count_systemu_daemons``
    misses first, its ``_scan_daemon_count`` fills this cache on the way
    through, and ``build_health_state``'s call below is then a cache hit.

    Never raises. A probe that blew up returns no records, which the model reads
    as "ports unknowable" and answers with the stronger warning.
    """
    import time
    now = time.monotonic() if _now is None else _now
    if now - _daemon_procs_cache["ts"] < _PROBE_TTL_S:
        return _daemon_procs_cache["procs"]
    try:
        procs = _scan_daemon_processes()
    except Exception:
        procs = ()
    if type(procs) is not tuple:
        procs = tuple(procs or ())
    _daemon_procs_cache["ts"] = now
    _daemon_procs_cache["procs"] = procs
    return procs


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


# -- The multi-daemon diagnosis (pure: records in, one issue out) ------------

#: The DANGER copy, unchanged since v0.8.0.2. Correct whenever the daemons
#: really are contending for one socket, and kept VERBATIM for that case.
_PORT_RACE_MESSAGE_TAIL = (
    "Whichever wins the port race will serve this dashboard, "
    "and recordings or decisions may land in the wrong vault."
)
_PORT_RACE_CTA = "systemu daemon stop --all"


def _known_port(value) -> Optional[int]:
    """``value`` as a real port, or None. ``type(x) is int`` in this frame:
    ``True`` is an int subclass and ``"8765"`` is a plausible-looking string,
    and either one silently becoming a port is how an unknown gets counted as
    a known one."""
    if type(value) is not int:
        return None
    return value if 0 < value <= 65535 else None


def daemon_conflict_issue(daemon_count,
                          daemons: Sequence["DaemonProcess"] = ()) -> Optional[HealthIssue]:
    """THE MODEL: N daemons plus what we know about them -> one banner issue.

    Pure. No probing, no I/O, no clock -- every shape is decided from the
    arguments, which is why all three of them are tested without a daemon.

    FAIL-CLOSED (DEC-27: completeness is witnessed, never inferred). The softer
    WARNING requires a COMPLETE and DISTINCT set of ports: one record per
    counted process, every port known, no two the same. Anything else -- a
    shared port, a port we could not read, a process the records do not account
    for -- keeps the port-race DANGER and its ``stop --all`` remedy. The banner
    is never allowed to talk itself down from a fact it could not establish.
    """
    try:
        count = int(daemon_count)
    except (TypeError, ValueError):
        return None
    if count <= 1:
        return None

    records = tuple(d for d in daemons if type(d) is DaemonProcess)
    ports = [_known_port(d.port) for d in records]
    complete = len(records) == count and all(p is not None for p in ports)
    distinct = complete and len(set(ports)) == len(ports)

    if not distinct:
        return HealthIssue(
            severity="danger",
            message=("{} systemu daemon processes are running. ".format(count)
                     + _PORT_RACE_MESSAGE_TAIL),
            cta=_PORT_RACE_CTA,
        )

    ordered = sorted(records, key=lambda d: _known_port(d.port) or 0)
    listing = "; ".join(
        "port {} - vault {}".format(
            _known_port(d.port),
            d.vault if type(d.vault) is str and d.vault else "(not recorded)")
        for d in ordered)

    # The daemon serving THIS page is the one to keep; the remedy must aim at
    # the others. When no record matched us, we do not guess -- every daemon is
    # offered and the operator picks.
    others = [d for d in ordered if d.is_self is not True] or list(ordered)
    targets = "; ".join(
        ("cd {} && systemu daemon stop".format(d.cwd)
         if type(d.cwd) is str and d.cwd else
         "from the working folder of the daemon on port {}, run: "
         "systemu daemon stop".format(_known_port(d.port)))
        for d in others)

    return HealthIssue(
        severity="warning",
        message=(
            "{count} systemu daemon processes are running, on different ports: "
            "{listing}. They are not racing for one port: this dashboard is "
            "served by the daemon on the port in your browser address bar, and "
            "what you do here is recorded in that daemon's vault."
        ).format(count=count, listing=listing),
        cta=(
            "Stop the one you did not mean to leave running, from its OWN "
            "working folder: {targets}. Do not use stop --all - it would also "
            "stop the daemon serving this dashboard."
        ).format(targets=targets),
    )


# -- Pure-data state builder (testable) --------------------------------------

def build_health_state(vault_dir: Optional[Path] = None, *, config=None,
                       provider_probe=None) -> HealthState:
    """Compute the current health state.  No UI, no side-effects."""
    state = HealthState()

    # Detection is machine-wide (the count); the DIAGNOSIS is port-aware. The
    # model decides which of the two messages is honest for this machine --
    # this frame must not re-derive it (DEC-43: one mint per fact).
    daemon_count = _count_systemu_daemons()
    if daemon_count > 1:
        conflict = daemon_conflict_issue(daemon_count, _daemon_processes())
        if conflict is not None:
            state.issues.append(conflict)

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
