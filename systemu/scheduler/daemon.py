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
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# PID file lives in the vault's parent directory
_PID_FILE_NAME = ".systemu_daemon.pid"

# Sidecar runtime state (pid + the port the daemon was actually told to serve).
# `daemon status` / `doctor` carry no --port, so without this they could only
# GUESS which socket to witness.
_RUNTIME_FILE_NAME = ".systemu_daemon.json"

DEFAULT_DASHBOARD_PORT = 8765

# Bounded wait applied to `daemon start` before it is allowed to claim success.
_DEFAULT_START_TIMEOUT_S = 60.0

# Exit status used by every surface that refuses to boot onto a vault root
# inside the systemu package (EX_CONFIG). Nonzero, and distinct from the
# "spawned but never became ready" exit so the two cannot be confused.
VAULT_ROOT_REFUSED_EXIT = 78


def resolve_child_vault_dir(vault_dir: str) -> str:
    """THE CHILD-SIDE BOUNDARY for the operating vault root.

    Returns the ONE absolute root the daemon child may operate out of, or
    refuses -- loudly, on stderr, with a nonzero exit -- when that root lies
    inside the installed systemu package.

    DEC-32: the fence is the ``refused`` bit ON THE MINTED VALUE, read in the
    frame that decides to open the vault. The refusal path (this message + this
    exit code) is not producible by the success path, and there is no
    intermediate frame between the check and the decision that could swallow it.
    The child needs its own copy of this check because a child can be launched
    directly (`python -m systemu.scheduler.daemon --vault-dir ...`), not only by
    :func:`start_daemon`.
    """
    from systemu.runtime.vault_root import refusal_message, resolve_vault_root

    verdict = resolve_vault_root(explicit=vault_dir)
    if verdict.refused:
        print(refusal_message(verdict), file=sys.stderr, flush=True)
        raise SystemExit(VAULT_ROOT_REFUSED_EXIT)
    return verdict.root


def _pid_file_path(vault_dir: str) -> Path:
    return Path(vault_dir).parent / _PID_FILE_NAME


def _runtime_file_path(vault_dir: str) -> Path:
    return Path(vault_dir).parent / _RUNTIME_FILE_NAME


def _check_port_available(host: str, port: int) -> bool:
    """v0.8.0.2: return True iff (host, port) can be bound for listen.

    Prevents the silent-multi-daemon failure where a leftover daemon wins
    the port race and serves the new user's dashboard with stale config.
    """
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


# ─────────────────────────────────────────────────────────────────────────────
#  THE READINESS MINT  (DEC-41 / DEC-43 / GATE-7a)
# ─────────────────────────────────────────────────────────────────────────────
#  "Is the daemon ready?" is disclosed by THREE operator-facing surfaces:
#     • `sharing_on daemon start`   — the success claim + the exit code
#     • `sharing_on daemon status`  — the rendered verdict
#     • `sharing_on doctor`         — platform_profile._probe_daemon_running
#
#  Before v0.10.22 each of them derived that fact from PROXIES: a pidfile that
#  exists, and a PID that is alive. Neither is a listening socket. `daemon
#  start` therefore returned 0 saying "Daemon started in background" ~12s
#  BEFORE the dashboard accepted its first connection, and `daemon status`
#  agreed it was "Running" for that whole window while an independent TCP
#  connect was refused.
#
#  DEC-41: an exit code is the tool CLAIM, not the effect WITNESS.
#  DEC-43: the fact has exactly ONE mint — `probe_readiness()` below. Every
#          surface CONSUMES the minted value. No surface re-derives it, and no
#          surface substitutes a proxy.
#  GATE-7a: fixing one gate of a multi-gate fact manufactures a contradiction
#          that did not exist before, so all three move together or none do.
#
#  The witness is a real TCP connection to the port the daemon was told to
#  serve. `pid` / `process_alive` ride along as CONTEXT for an honest message
#  ("starting", "exited") — they never decide `ready`.

#  F13 rides THIS mint too (DEC-43 — no parallel derivation).
#
#  `start_daemon` sets the child's PYTHONPATH and the child imports whatever
#  that resolves to. In a real session the CLI ran systemu 0.10.22 out of a
#  worktree while the daemon it spawned loaded systemu 0.10.21 out of
#  site-packages (a stale editable install pointing at a different worktree).
#  Every surface said "Ready". The daemon served stale code for twenty minutes.
#  End users hit the same skew via a half-finished upgrade, a stale editable
#  install, a venv/system-python mix, or two systemu installs on one box.
#
#  So the verdict now also carries WHICH BUILD the daemon is executing:
#    • the daemon RECORDS its own (version + resolved package dir) into the
#      runtime sidecar — only the process that loaded the code may say what it
#      loaded, so the parent never writes that field; and
#    • the mint compares it with the build THIS process imported.
#
#  `build_match` is TRI-STATE and fail-closed (DEC-27): True agree, False skew,
#  None UNVERIFIED. A daemon that recorded nothing (i.e. an older build — which
#  is itself the skew case) is UNVERIFIED, never "agrees".
#
#  A mismatch is REPORTED, never fatal: a user mid-upgrade must still be able
#  to run `daemon stop`.

@dataclass(frozen=True)
class DaemonReadiness:
    """The minted readiness verdict. ``ready`` is TRUE only when a connection
    to (host, port) has been OBSERVED to succeed."""

    ready: bool
    pid: Optional[int]
    process_alive: bool
    host: str
    port: Optional[int]
    reason: str

    # -- F13: which systemu BUILD each side is executing ----------------------
    # ``cli_*`` is the build of the process HOLDING this value (the CLI for
    # `daemon start` / `daemon status` / `doctor`; the daemon itself for the
    # in-dashboard /health page). Defaults keep every existing construction
    # site valid; an absent build reads as UNVERIFIED, never as agreement.
    daemon_version: Optional[str] = None
    daemon_path: Optional[str] = None
    cli_version: Optional[str] = None
    cli_path: Optional[str] = None
    build_match: Optional[bool] = None
    build_note: str = ""

    # -- the vault-root fence (DEC-32) ---------------------------------------
    # True only when the boot was REFUSED because the operating vault root lies
    # inside the installed systemu package. Distinct from a plain not-ready:
    # nothing was spawned, nothing was written, and the remedy is different.
    refused: bool = False

    # -- D6: WHICH socket, and WHOSE number it is ----------------------------
    # `port` alone is not an honest answer. Four different facts can produce it
    # (see PORT_SOURCE_*), and three of them are the operator's own choice while
    # the fourth is a GUESS this process made. A stopped daemon deletes the
    # record of the port it really served, so the very next `status` falls to
    # the guess -- and printed it in exactly the same voice as a chosen number.
    # The provenance rides out WITH the port so no surface can lose it.
    port_source: str = ""
    # The operating vault root this verdict is ABOUT, taken from the one mint
    # (`systemu.runtime.vault_root.resolve_vault_root`). "Not running" is only
    # meaningful once the operator knows which vault it is not running for.
    vault_root: str = ""

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"


def _readiness_host() -> str:
    """The host an operator's client can actually reach. A 0.0.0.0 / :: bind is
    not a connectable address — witness it on loopback."""
    h = (os.getenv("SYSTEMU_DASHBOARD_HOST") or "").strip() or "127.0.0.1"
    return "127.0.0.1" if h in ("0.0.0.0", "::", "*") else h


def _own_build() -> dict:
    """The systemu build THIS process actually imported: version + the resolved
    package directory.

    A statement about the CALLING interpreter and nothing else. The daemon calls
    it about itself; the CLI calls it about itself; :func:`probe_readiness`
    compares the two. Never raises — an undeterminable build reads as "unknown"
    and, being unequal to any real build, can only make the verdict stricter.
    """
    version = "unknown"
    path = "unknown"
    try:
        import systemu as _s
        _v = getattr(_s, "__version__", None)
        if type(_v) is str:
            version = _v
        _f = getattr(_s, "__file__", None)
        if type(_f) is str:
            path = str(Path(_f).resolve().parent)
    except Exception:
        logger.debug("[Daemon] own-build probe failed (ignored)", exc_info=True)
    return {"version": version, "path": path}


def _write_runtime_state_at(path: Path, *, pid: Optional[int], port: int,
                            host: Optional[str] = None,
                            build: Optional[dict] = None) -> None:
    """Write the runtime sidecar at an explicit path. Never raises.

    ``build`` is the F13 record of WHICH systemu the writing process imported.
    Only the daemon process itself may pass one — see the structural fence in
    tests/test_daemon_build_skew_witness.py.
    """
    try:
        record = {"pid": (int(pid) if pid is not None else None),
                  "port": int(port),
                  "host": host or _readiness_host(),
                  "build": (dict(build) if type(build) is dict else None)}
        path.write_text(json.dumps(record), encoding="utf-8")
    except Exception:
        logger.debug("[Daemon] runtime-state write failed (ignored)", exc_info=True)


def _write_runtime_state(vault_dir: str, *, pid: Optional[int], port: int,
                         host: Optional[str] = None,
                         build: Optional[dict] = None) -> None:
    """Record which socket THIS daemon was told to serve. Never raises."""
    _write_runtime_state_at(_runtime_file_path(vault_dir), pid=pid, port=port,
                            host=host, build=build)


def _recorded_build(state: dict) -> tuple[Optional[str], Optional[str]]:
    """(version, path) the DAEMON recorded, or (None, None).

    The sidecar is a file on disk that a half-written boot, an older daemon or
    an unrelated process may have left in any shape. Every value is pinned with
    ``type(x) is T`` in this frame before it is used (DEC-36) — anything else is
    UNVERIFIED, which is a distinct state from agreement.
    """
    if type(state) is not dict:
        return None, None
    b = state.get("build")
    if type(b) is not dict:
        return None, None
    v = b.get("version")
    p = b.get("path")
    if type(v) is not str or type(p) is not str:
        return None, None
    if not v or not p:
        return None, None
    return v, p


def _same_path(a: Optional[str], b: Optional[str]) -> bool:
    """Do two recorded package directories denote the same location?"""
    if type(a) is not str or type(b) is not str:
        return False
    try:
        return os.path.normcase(os.path.normpath(a)) == os.path.normcase(
            os.path.normpath(b))
    except Exception:
        return False


def _read_runtime_state(vault_dir: str) -> dict:
    """Read the sidecar runtime state. Never raises; {} when absent/corrupt."""
    try:
        raw = _runtime_file_path(vault_dir).read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _process_alive(pid: Optional[int]) -> bool:
    """Is `pid` a live process? A PROXY — never an operator-facing verdict."""
    if not pid:
        return False
    try:
        if sys.platform == "win32":
            import ctypes
            # 0x1000 = PROCESS_QUERY_LIMITED_INFORMATION
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if handle == 0:
                return False
            exit_code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            ctypes.windll.kernel32.CloseHandle(handle)
            return exit_code.value == 259           # 259 = STILL_ACTIVE
        os.kill(pid, 0)                             # signal 0 = no-op probe
        return True
    except (ProcessLookupError, PermissionError, OSError, ValueError):
        return False


def _pidfile_process(vault_dir: str) -> tuple[Optional[int], bool]:
    """(pid, alive) from the pidfile, cleaning up a stale file.

    INTERNAL single-instance bookkeeping only — this is the proxy the operator
    surfaces are forbidden to use as their verdict.
    """
    pid_file = _pid_file_path(vault_dir)
    if not pid_file.exists():
        return None, False
    try:
        pid = int(pid_file.read_text().strip())
    except (ValueError, OSError):
        pid_file.unlink(missing_ok=True)
        return None, False
    if _process_alive(pid):
        return pid, True
    pid_file.unlink(missing_ok=True)
    _runtime_file_path(vault_dir).unlink(missing_ok=True)
    return None, False


#: Where a witnessed port number came from. Three of these are the OPERATOR's
#: number; ``PORT_SOURCE_DEFAULT`` is this process's guess, and the whole point
#: of the distinction is that the two must never read alike (D6).
PORT_SOURCE_EXPLICIT = "explicit"      # a --port on the command line
PORT_SOURCE_RECORDED = "recorded"      # the port the daemon itself recorded
PORT_SOURCE_ENV = "env"                # SYSTEMU_DASHBOARD_PORT
PORT_SOURCE_DEFAULT = "default"        # DEFAULT_DASHBOARD_PORT -- a guess

#: ASCII only (DEC-32c): a verdict string a cp1252 console cannot encode is a
#: verdict the operator never gets to read.
_PORT_SOURCE_PHRASES = {
    PORT_SOURCE_EXPLICIT: "port {port} is the one you passed with --port",
    PORT_SOURCE_RECORDED: "port {port} is the one this vault's daemon recorded",
    PORT_SOURCE_ENV: "port {port} came from SYSTEMU_DASHBOARD_PORT",
    PORT_SOURCE_DEFAULT: ("port {port} is the built-in default -- nothing here "
                          "named a port, so this is a guess"),
}

#: The one thing the operator can do that this process cannot work out for
#: itself: name the socket a daemon was actually started on.
_PORT_REMEDY = "if the daemon was started on another port, pass --port <number>"


def port_provenance_note(port: int, source: str) -> str:
    """One ASCII clause saying WHERE the probed port number came from.

    An unknown source is reported as unknown rather than silently rendered as a
    chosen number -- the defect being closed is precisely a guess wearing the
    voice of a choice.
    """
    template = _PORT_SOURCE_PHRASES.get(
        source, "port {port} came from an unrecorded source")
    return template.format(port=port)


def vault_note(vault_root: str) -> str:
    """One ASCII clause naming the operating vault a verdict is ABOUT."""
    return "vault: {}".format(vault_root)


def _resolve_readiness_port(vault_dir: str, port: Optional[int],
                            recorded_state: Optional[dict] = None
                            ) -> tuple[int, str]:
    """Which socket to witness, and WHOSE number it is.

    Returns ``(port, source)``; ``source`` is one of the ``PORT_SOURCE_*``
    constants. The precedence is unchanged -- explicit > recorded > env >
    default -- but the answer no longer arrives stripped of the fact that makes
    it readable.
    """
    try:
        if port:
            return int(port), PORT_SOURCE_EXPLICIT
    except (TypeError, ValueError):
        pass
    state = recorded_state if type(recorded_state) is dict else _read_runtime_state(vault_dir)
    recorded = state.get("port")
    try:
        if recorded:
            return int(recorded), PORT_SOURCE_RECORDED
    except (TypeError, ValueError):
        pass
    try:
        env_port = (os.getenv("SYSTEMU_DASHBOARD_PORT") or "").strip()
        if env_port:
            return int(env_port), PORT_SOURCE_ENV
    except (TypeError, ValueError):
        pass
    return DEFAULT_DASHBOARD_PORT, PORT_SOURCE_DEFAULT


def _connection_succeeds(host: str, port: int, timeout: float) -> bool:
    """THE WITNESS: open a real TCP connection to (host, port)."""
    import socket
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except (OSError, TypeError, ValueError):
        return False


#  Wording note: the strings below are asserted on by three surfaces whose own
#  tests key off the readiness vocabulary. They deliberately contain neither
#  "ready" nor "running" so a build line can never be mistaken for — or mask —
#  a readiness verdict.
_BUILD_REMEDY = ("Restart it so both sides run the same code: "
                 "`systemu daemon stop` then `systemu daemon start`.")


def _compare_builds(*, tracked: bool, daemon_version: Optional[str],
                    daemon_path: Optional[str], mine: dict
                    ) -> tuple[Optional[bool], str]:
    """THE SOLE comparison of "which build is the daemon executing?".

    Returns ``(match, note)`` where ``match`` is TRI-STATE:
      * ``True``  — the daemon recorded exactly the build this process imported
      * ``False`` — it recorded a DIFFERENT version or a different package dir
      * ``None``  — UNVERIFIED: no daemon to ask, or it recorded nothing

    ``None`` is never rendered as agreement anywhere. A daemon that records no
    build predates this record, which means it is a different build — the very
    condition being reported (DEC-27: completeness is witnessed, not inferred).
    """
    if not tracked:
        # Nothing of ours is up: there is no build claim to make, and inventing
        # one would be noise on every `daemon status` of a stopped daemon.
        return None, ""

    if daemon_version is None or daemon_path is None:
        return None, (
            "UNVERIFIED build: the daemon process did not record which systemu "
            "it loaded, so it cannot be compared with this CLI "
            f"(systemu {mine['version']} from {mine['path']}). It is an older "
            f"daemon, or was started by a different systemu install. "
            + _BUILD_REMEDY)

    if daemon_version == mine["version"] and _same_path(daemon_path, mine["path"]):
        return True, (f"same build on both sides: systemu {daemon_version} "
                      f"from {daemon_path}")

    return False, (
        f"BUILD SKEW: the daemon process is executing systemu "
        f"{daemon_version} from {daemon_path}, but this CLI is systemu "
        f"{mine['version']} from {mine['path']}. The daemon is serving "
        f"different code than you are addressing it with. " + _BUILD_REMEDY)


def probe_readiness(vault_dir: str, *, port: Optional[int] = None,
                    host: Optional[str] = None,
                    timeout: float = 1.0) -> DaemonReadiness:
    """THE SOLE MINT of the "daemon is ready" fact (DEC-43).

    Attempts a real connection to the dashboard port. Never raises.

    ``ready`` is the CONJUNCTION of two facts:

      * a TCP connection to the recorded socket SUCCEEDED — the witness, and
        the one the required property makes NECESSARY; and
      * the daemon process this vault tracks is alive.

    The second conjunct can only ever make the verdict STRICTER, so it cannot
    resurrect the DEC-41 defect (a claim without an effect). It is there so a
    foreign program squatting on port 8765 is never disclosed as "the systemu
    daemon" — the pre-fix code could not make that mistake and neither may
    this one. Both conjuncts also ride out on the value as CONTEXT so every
    surface can render an honest reason instead of a bare boolean.
    """
    # Read the sidecar BEFORE _pidfile_process — that call DELETES the sidecar
    # when the recorded pid is dead, which would otherwise lose the very port
    # number the not-ready reason has to name.
    recorded = _read_runtime_state(vault_dir)
    pid, alive = _pidfile_process(vault_dir)
    resolved_port, port_source = _resolve_readiness_port(vault_dir, port, recorded)

    # D6: WHICH vault this verdict is about, from THE mint. Reporting only --
    # nothing here opens, creates or refuses anything, so a refused root still
    # gets an honest "not running" answer naming the root that was refused.
    from systemu.runtime.vault_root import resolve_vault_root as _resolve_vault_root
    vault_root = _resolve_vault_root(explicit=vault_dir).root

    _rec_host = recorded.get("host")
    if type(_rec_host) is not str:
        _rec_host = ""
    resolved_host = ((host if type(host) is str else "").strip()
                     or _rec_host.strip()
                     or _readiness_host())
    if resolved_host in ("0.0.0.0", "::", "*", ""):
        resolved_host = "127.0.0.1"

    connected = _connection_succeeds(resolved_host, resolved_port, timeout)
    ready = bool(connected and alive)

    # D6: every branch names the port's PROVENANCE and the vault, so no reading
    # of any status output can mistake a defaulted guess for the operator's own
    # number, or a true answer about one vault for an answer about theirs.
    provenance = port_provenance_note(resolved_port, port_source)
    where = vault_note(vault_root)

    if ready:
        reason = (f"accepting connections on http://{resolved_host}:{resolved_port}"
                  f" ({provenance}); {where}")
    elif connected:
        reason = (f"something is accepting connections on "
                  f"{resolved_host}:{resolved_port} but no systemu daemon is "
                  f"tracked for this vault ({provenance}); {where}")
    elif alive:
        reason = (f"process {pid} is alive but nothing is accepting connections "
                  f"on {resolved_host}:{resolved_port} yet ({provenance}); "
                  f"{where}")
    else:
        reason = (f"no live daemon process and nothing is accepting connections "
                  f"on {resolved_host}:{resolved_port} ({provenance}); "
                  f"{where}; {_PORT_REMEDY}")

    d_ver, d_path = _recorded_build(recorded)
    mine = _own_build()
    match, note = _compare_builds(tracked=alive, daemon_version=d_ver,
                                  daemon_path=d_path, mine=mine)

    return DaemonReadiness(
        ready=ready, pid=pid, process_alive=alive,
        host=resolved_host, port=resolved_port, reason=reason,
        daemon_version=d_ver, daemon_path=d_path,
        cli_version=mine["version"], cli_path=mine["path"],
        build_match=match, build_note=note,
        port_source=port_source, vault_root=vault_root,
    )


def _restate(v: DaemonReadiness, **changes) -> DaemonReadiness:
    """Re-word a MINTED verdict without dropping any fact it carries.

    Hand-rolled ``DaemonReadiness(...)`` re-constructions are how a field added
    to the mint silently stops reaching the surfaces: the constructor still
    type-checks, the new fact just quietly reverts to its default. Every
    re-statement of an already-minted verdict goes through here.
    """
    from dataclasses import replace as _replace
    return _replace(v, **changes)


def await_readiness(vault_dir: str, *, port: Optional[int] = None,
                    host: Optional[str] = None,
                    timeout_s: float = _DEFAULT_START_TIMEOUT_S,
                    poll_s: float = 0.25,
                    probe_timeout: float = 1.0) -> DaemonReadiness:
    """Poll the MINT until it says ready, or the bound expires.

    Bounded on purpose: a genuinely broken daemon must not hang the CLI — it
    must be reported honestly instead. Returns the last minted verdict.
    """
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    verdict = probe_readiness(vault_dir, port=port, host=host, timeout=probe_timeout)
    saw_process = verdict.process_alive

    while not verdict.ready and time.monotonic() < deadline:
        if saw_process and not verdict.process_alive:
            return _restate(
                verdict, process_alive=False,
                reason=("the daemon process exited during startup without ever "
                        f"accepting a connection on {verdict.host}:{verdict.port}"),
            )
        time.sleep(max(0.0, float(poll_s)))
        verdict = probe_readiness(vault_dir, port=port, host=host, timeout=probe_timeout)
        saw_process = saw_process or verdict.process_alive

    if not verdict.ready:
        return _restate(
            verdict,
            reason=(f"timed out after {float(timeout_s):.0f}s — {verdict.reason}"),
        )
    return verdict


def _start_timeout_s() -> float:
    try:
        return max(0.0, float(os.getenv("SYSTEMU_DAEMON_START_TIMEOUT",
                                        str(_DEFAULT_START_TIMEOUT_S))))
    except (TypeError, ValueError):
        return _DEFAULT_START_TIMEOUT_S


def _find_listening_pid(port: int):
    """Best-effort: return PID of the process holding `port`, or None.

    psutil is already a project dependency.
    """
    try:
        import psutil
        for conn in psutil.net_connections(kind="inet"):
            if conn.laddr and conn.laddr.port == port and conn.status == psutil.CONN_LISTEN:
                return conn.pid
    except Exception:
        return None
    return None


# ─────────────────────────────────────────────────────────────────────────────
#  Public commands
# ─────────────────────────────────────────────────────────────────────────────

def _dashboard_port_free(port: int, host: str = "") -> bool:
    """W13.7: pre-bind probe — True when the dashboard port is free.

    The loser of a port race used to keep running headless while recordings
    and decisions landed in whichever vault won (5 daemons seen racing one
    port in the field). Never raises.
    """
    import socket
    h = host or os.getenv("SYSTEMU_DASHBOARD_HOST", "127.0.0.1")
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind((h, int(port)))
        finally:
            s.close()
        return True
    except OSError:
        return False
    except Exception:
        return True  # probe failure must not block a legitimate boot


def start_daemon(
    vault_dir: str,
    config,
    vault,
    *,
    port: int = DEFAULT_DASHBOARD_PORT,
    foreground: bool = False,
    wait_timeout_s: Optional[float] = None,
) -> Optional[DaemonReadiness]:
    """Start the Systemu daemon.

    If foreground=True, runs in the current process (used for debugging) and
    returns None when that blocking loop ends.

    In background mode the spawn is followed by a BOUNDED wait on the readiness
    mint, and the minted verdict is RETURNED so the caller's success claim can
    be gated on the witness rather than on the spawn (DEC-41).
    """
    # ── THE VAULT-ROOT BOUNDARY (DEC-32) ────────────────────────────────────
    # FIRST statement in the function, ahead of every write: the pidfile, the
    # log file and the runtime sidecar all hang off this path. `vault_dir` used
    # to travel onward as the caller's own string -- typically the RELATIVE
    # `systemu/vault` -- and the child then resolved it against a DIFFERENT cwd.
    # The mint turns it into ONE absolute path and hands back the fence bit
    # alongside it; from here down nothing reads `vault_dir` again.
    from systemu.runtime.vault_root import refusal_message as _refusal_message
    from systemu.runtime.vault_root import resolve_vault_root as _resolve_vault_root

    _root_verdict = _resolve_vault_root(explicit=vault_dir)
    if _root_verdict.refused:
        logger.error("[Daemon] %s", _root_verdict.reason)
        return DaemonReadiness(
            ready=False, pid=None, process_alive=False,
            host=_readiness_host(), port=int(port),
            reason=_refusal_message(_root_verdict),
            refused=True,
        )
    vault_dir = _root_verdict.root

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
    # NOTE: this guard deliberately uses the PROCESS proxy, not the readiness
    # mint. "Should I spawn a second daemon?" is a DIFFERENT fact from "is the
    # daemon ready?" — a daemon that is still booting has not bound its port
    # yet, and spawning a rival at that moment is exactly the port race W13.7
    # closed. The proxy is correct here and is never disclosed to the operator
    # as a readiness verdict.
    if pid_file.exists():
        existing_pid = pid_file.read_text().strip()
        _pid, _alive = _pidfile_process(vault_dir)   # also cleans up if dead
        if _alive:
            logger.warning("[Daemon] Already running (PID %s). Stop it first.", existing_pid)
            return await_readiness(vault_dir, port=port,
                                   timeout_s=(_start_timeout_s()
                                              if wait_timeout_s is None
                                              else wait_timeout_s))
        logger.info(
            "[Daemon] Stale PID file (PID %s no longer running) — starting fresh",
            existing_pid,
        )
        # _pidfile_process() already removed the PID file; nothing more to do.

    # A stale or foreign process already holding the port makes the readiness
    # witness AMBIGUOUS, and that ambiguity resolved the WRONG way in the live
    # run of this fix: the connection succeeded against THAT server on the very
    # first probe, while the child we had just spawned was still alive and one
    # instant from dying on its own W13.7 port-in-use check — so `daemon start`
    # printed "Daemon ready." and exited 0 for a daemon that never ran.
    # Refuse here, where we still know why. Reached only when our own pidfile
    # says nothing of ours is up, so an idempotent restart is unaffected.
    if not _check_port_available("127.0.0.1", port):
        holder = _find_listening_pid(port)
        who = f"PID {holder}" if holder else "an unknown process"
        logger.error("[Daemon] Port %d is already in use by %s — refusing to start.",
                     port, who)
        return DaemonReadiness(
            ready=False, pid=None, process_alive=False,
            host=_readiness_host(), port=int(port),
            reason=(f"port {port} is already in use by {who}; refusing to start a "
                    f"second daemon whose recordings would land in another vault "
                    f"(`systemu daemon stop --all`, or pick another --port)"),
        )

    # Spawn as a detached subprocess.
    #
    # `vault_dir` is the MINTED ABSOLUTE root by now, so the child cannot
    # re-resolve it against its own cwd. This argv used to carry the caller's
    # relative string.
    cmd = [
        sys.executable, "-m", "systemu.scheduler.daemon",
        "--vault-dir", vault_dir,
        "--port", str(port),
    ]
    Path(vault_dir).mkdir(parents=True, exist_ok=True)
    log_file = open(Path(vault_dir) / "daemon.log", "a", encoding="utf-8")
    import subprocess
    import os
    import systemu

    # The OPERATING HOME — the directory the operator is standing in. It is the
    # child's cwd, so every relative path the child touches lands where the
    # parent's would have.
    #
    # This used to be conditional: cwd only when it "looked like" a systemu
    # working dir (a `.env` or `.systemu_mode` present), else
    # `Path(systemu.__file__).parent.parent`. Launching from an empty directory
    # therefore ran the whole daemon inside the package tree — that is the
    # v0.10.23 defect this file's `resolve_child_vault_dir` fences. The shape of
    # the cwd no longer decides anything.
    operating_home = Path(_root_verdict.home)

    # project_root stays derived exactly as before, and ONLY feeds PYTHONPATH:
    # it answers "where is the code", which is a different question from "where
    # is the operator standing" and must keep its F13 behaviour byte-for-byte.
    _cwd = Path.cwd().absolute()
    if (_cwd / ".env").exists() or (_cwd / ".systemu_mode").exists():
        project_root = _cwd
    else:
        project_root = Path(systemu.__file__).parent.parent.absolute()
    env = os.environ.copy()
    # F13 — PREPEND, never CLOBBER. This line used to be a bare assignment, and
    # discarding the parent's PYTHONPATH is precisely how the CLI and the daemon
    # came to run different systemu builds: the operator's PYTHONPATH said "run
    # THIS checkout", `project_root` resolved to the working directory instead
    # (it holds a .env), and the child — no longer able to see the checkout —
    # fell through to a stale editable install in site-packages.
    #
    # The project root still WINS (first entry), so the packaged-install case
    # (PYTHONPATH unset ⇒ the string is byte-for-byte what it was before) and
    # the dev case are both unchanged; the parent's entries are merely no longer
    # thrown away. Deduplicated so a repeated start cannot grow the variable.
    _path_parts = [str(project_root)]
    _path_parts += [p for p in (env.get("PYTHONPATH") or "").split(os.pathsep) if p]
    _seen: set = set()
    _ordered = []
    for _p in _path_parts:
        _key = os.path.normcase(_p)
        if _key in _seen:
            continue
        _seen.add(_key)
        _ordered.append(_p)
    env["PYTHONPATH"] = os.pathsep.join(_ordered)
    # v0.8.0.3: propagate the resolved project root into the daemon's
    # environment so AppState._resolve_project_root() picks it up via Tier 1
    # instead of recomputing (broken on pip installs) or walking the vault.
    # setdefault so an operator override on the parent shell still wins.
    env.setdefault("SYSTEMU_PROJECT_ROOT", str(operating_home))

    # The child re-reads SYSTEMU_VAULT_DIR in several places (skill migrator,
    # credential store, memory backends, Config.from_env). Pin it to the SAME
    # absolute value carried in argv so argv and env cannot disagree — an
    # unset/relative value here is exactly how the child's own reads used to
    # land in a different directory than the one it was told to serve. Assigned,
    # not setdefault: the mint already honoured any operator-set value when it
    # produced this root, so this IS the operator's choice, absolutised.
    env["SYSTEMU_VAULT_DIR"] = vault_dir

    # F13: written BEFORE the spawn, and with NO build.
    #   * no build — the parent knows the build IT imported, and nothing at all
    #     about the one the child will resolve. Stamping its own version here
    #     would manufacture agreement for exactly the skew this reports.
    #   * before the spawn — this write also CLEARS a previous daemon's build
    #     record, and the child's own write (the only one that carries a build)
    #     must be the last word. A post-spawn parent write could race it.
    _write_runtime_state(vault_dir, pid=None, port=port, build=None)

    # D10: the child this line is about will, on a fresh install, start pulling
    # down Playwright's Chromium -- roughly 150 MB -- and its stdout is
    # redirected into daemon.log, so the notice it prints for itself reaches no
    # console. THIS is the copy the operator sees, and it goes out BEFORE the
    # spawn: a download disclosed after it started is not disclosed. Best
    # effort; the announcement must never be what stops a daemon from booting.
    try:
        from systemu.runtime.web import provision as _provision
        _provision.announce_browser_autoinstall()
    except Exception:
        logger.debug("[Daemon] browser auto-install announcement failed",
                     exc_info=True)

    proc = subprocess.Popen(
        cmd,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        close_fds=True,
        start_new_session=True,
        cwd=str(operating_home),
        env=env,
    )
    pid_file.write_text(str(proc.pid))
    logger.info("[Daemon] Spawned background process PID %d — waiting for it to "
                "accept connections on port %d", proc.pid, port)

    # DEC-41: the spawn is a CLAIM. Only the connection is a WITNESS. Wait for
    # it (bounded) and hand the caller the minted verdict.
    return await_readiness(
        vault_dir, port=port,
        timeout_s=_start_timeout_s() if wait_timeout_s is None else wait_timeout_s,
    )


def stop_daemon(vault_dir: str) -> bool:
    """Send SIGTERM to the running daemon. Returns True if stopped."""
    pid_file = _pid_file_path(vault_dir)
    if not pid_file.exists():
        _runtime_file_path(vault_dir).unlink(missing_ok=True)
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
        _runtime_file_path(vault_dir).unlink(missing_ok=True)
        logger.info("[Daemon] Stopped PID %d", pid)
        return True
    except (ProcessLookupError, PermissionError) as exc:
        logger.warning("[Daemon] Could not stop PID %d: %s", pid, exc)
        pid_file.unlink(missing_ok=True)
        _runtime_file_path(vault_dir).unlink(missing_ok=True)
        return False


def get_status(vault_dir: str, *, port: Optional[int] = None,
               host: Optional[str] = None, timeout: float = 1.0) -> dict:
    """The daemon status dict — a pure PROJECTION of the readiness mint.

    DEC-43: this function does NOT re-derive the fact. Before v0.10.23 it read
    the pidfile and probed process liveness, so `daemon status` printed
    "Running (PID 22424)" for the ~12 s the daemon spent migrating before its
    dashboard bound the port, while an independent TCP connect was refused.

    ``running`` is now an ALIAS of ``ready`` (its historical callers — the CLI
    panel and doctor's ``_probe_daemon_running`` — asked "can I use it?", which
    was always the readiness question). ``process_alive`` carries the old proxy
    for an honest "still starting" message; no caller may treat it as a verdict.
    """
    v = probe_readiness(vault_dir, port=port, host=host, timeout=timeout)
    return {
        "running": v.ready,          # alias — never a proxy (DEC-43)
        "ready": v.ready,
        "pid": v.pid,
        "process_alive": v.process_alive,
        "host": v.host,
        "port": v.port,
        "url": v.url,
        "reason": v.reason,
        # D6 -- the port's provenance and the vault the verdict is about ride
        # the projection too: a fact the mint carries but the dict drops is a
        # fact no operator surface can reach.
        "port_source": v.port_source,
        "vault_root": v.vault_root,
        # F13 — WHICH build the daemon is executing rides the same projection,
        # so no consumer has to (or may) derive it a second way.
        "daemon_version": v.daemon_version,
        "daemon_path": v.daemon_path,
        "cli_version": v.cli_version,
        "cli_path": v.cli_path,
        "build_match": v.build_match,
        "build_note": v.build_note,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Daemon loop
# ─────────────────────────────────────────────────────────────────────────────

def _v0822_run_vault_migrator(vault, *, logger_=None) -> None:
    """v0.8.22: idempotent vault seed upgrade. Reads installed __version__ vs
    <vault>/.seed_version; on diff, deploys new/updated seed tools and wires
    Wild Card. Silent on fast path; one INFO line per upgrade. Never raises."""
    log = logger_ or logger
    try:
        from systemu.runtime.vault_migrator import run as _migrate_vault
        from pathlib import Path
        summary = _migrate_vault(Path(vault.root), logger_=log)
        log.info("[Daemon] v0.8.22 vault migrator: %s", summary)
    except Exception:
        log.exception("[Daemon] v0.8.22 vault migrator crashed — continuing boot")

    # IMPL-4 USED TO POST THE BULK FIRST-GATE REVIEW CARD HERE. It does not any more,
    # and re-adding it is a regression - pinned by
    # `tests/test_impl4_bulk_first_gate.py::test_daemon_boot_enqueues_nothing`.
    #
    # Operator ruling, 2026-08-12: boot is the wrong moment. The backfill above really
    # does classify the whole inventory here, but posting the review here meant a fresh
    # operator met a HIGH-risk consent demand at minute zero, before submitting anything.
    # The card now posts at the first TASK SUBMISSION
    # (`first_gate_review.maybe_post_on_task_submission`, called from both chat lanes),
    # and the Build page can request it on demand.
    #
    # Nothing runs ungated in the meantime: the per-tool first-use gate is the floor, and
    # it asks on a card showing the actual arguments. The bulk card is convenience over
    # that floor, never the floor itself.


def _advise_if_vault_empty(vault, *, logger_=None) -> bool:
    """v0.7.4 Pattern 4: tell the operator to seed a vault that has no content.

    Returns True when the advice was actually emitted. Never raises.

    F5: this is ADVICE, so it is only honest once the system has finished doing
    whatever it was going to do about the condition itself. Call it from
    :func:`_seed_and_advise` and nowhere else.
    """
    log = logger_ or logger
    try:
        tools = vault.load_index("tools") or []
        skills = vault.load_index("skills") or []
        if tools or skills:
            return False
        log.warning(
            "[Daemon] Vault is empty (0 tools, 0 skills). Run "
            "`systemu init` to seed the bundled starter catalog, "
            "or `systemu record` to capture a workflow that will "
            "trigger auto-forge."
        )
        try:
            from systemu.interface.notifications import log_event as _le
            _le(
                "WARNING", "runtime",
                "Empty vault on daemon startup — run `systemu init`",
                {"action": "init"},
            )
        except Exception:
            pass
        return True
    except Exception:
        log.debug("[Daemon] vault-empty check failed (ignored)", exc_info=True)
        return False


def _seed_and_advise(vault, *, logger_=None) -> None:
    """DEFECT F5 — the ORDER is the whole fix.

    On a fresh vault the boot used to WARN "Vault is empty (0 tools, 0 skills).
    Run `sharing_on init` ..." — and post it to the operator notification feed —
    and then, milliseconds later in the same startup, the seed migrator added 41
    tools by itself ("VaultMigrator 0.0.0 -> 0.10.21: added=41"). The operator
    was instructed to run a command that was neither needed nor useful.

    Emptiness is therefore observed only AFTER the migrator has had its chance
    to seed. The advice is not deleted — a vault the migrator seeds NOTHING into
    is genuinely empty and still gets it.
    """
    _v0822_run_vault_migrator(vault, logger_=logger_)
    _advise_if_vault_empty(vault, logger_=logger_)


def provider_banner_lines(config, *, probe=None) -> list:
    """The startup banner's provider rows, MINTED (F19 / DEC-43).

    The banner said ``OPENROUTER_API_KEY: set|MISSING`` — a sixth private copy
    of the satisfaction recipe, and the first thing an operator reads in the
    log. On a machine with a Google key it said MISSING while the dashboard
    said the opposite, and it never mentioned the keyless provider at all.

    ASCII only (DEC-32c): this line goes to a log file and a Windows console.
    Never prints a credential VALUE — only the minted state and its detail,
    neither of which contains one. Never raises: the banner must not be able to
    stop a daemon from booting.
    """
    try:
        from systemu.runtime import provider_status as _ps
        statuses = _ps.all_provider_statuses(
            config, probe=probe, cache_ttl_s=_ps.PROBE_CACHE_TTL_S)
        rows = [f"  Provider {_ps.status_line(statuses[s.provider])}"
                for s in _ps.PROVIDER_SPECS if s.provider in statuses]
        if not _ps.any_satisfied(statuses):
            rows.append("  Provider WARNING: none usable - "
                        + _ps.configure_hint(statuses))
        return rows
    except Exception as exc:  # pragma: no cover - defensive
        return [f"  Provider status unavailable ({type(exc).__name__})"]


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
        curator_review_job,
        startup_recovery_sweep,
    )

    # Write PID
    pid_file.write_text(str(os.getpid()))
    # The readiness sidecar records WHICH socket this daemon was told to serve,
    # next to the pidfile, so `daemon status` / doctor (which carry no --port)
    # witness the right one. Written HERE rather than only in start_daemon() so
    # a --foreground daemon is witnessable too. Never fatal.
    # F13: this is ALSO the one and only place the systemu build is recorded —
    # by the process that actually imported it, about itself. Written before the
    # dashboard binds its port, so by the time any surface can witness readiness
    # the build is already on record.
    _runtime_state_file = pid_file.parent / _RUNTIME_FILE_NAME
    _write_runtime_state_at(_runtime_state_file, pid=os.getpid(), port=port,
                            build=_own_build())
    logger.info("[Daemon] running systemu %s from %s",
                _own_build()["version"], _own_build()["path"])

    def _cleanup_runtime_files() -> None:
        pid_file.unlink(missing_ok=True)
        try:
            _runtime_state_file.unlink(missing_ok=True)
        except Exception:
            pass

    atexit.register(_cleanup_runtime_files)

    # v0.3.3 / v0.3.5 — Record interpreter invariant + optional pre-warm.
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

    # v0.8.10: ensure a headless browser is available (background, non-blocking).
    # T0 fetch + T1 search work immediately; T2 browser tools come online once
    # chromium finishes installing.
    try:
        from systemu.runtime.web.provision import ensure_chromium_async
        ensure_chromium_async()
    except Exception:
        logger.exception("[Daemon] browser provision probe failed — continuing boot")

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
        state = None
        # vault remains the raw Vault(args.vault_dir) passed in — degraded mode

    # v0.8.0.3: startup banner — make misconfiguration visible immediately
    # instead of letting users hit silent failures hours later (recordings
    # going to wrong dir, refine not dispatching, etc).
    try:
        _raw_vault = getattr(vault, "_v", vault)
        _vault_root_abs = getattr(_raw_vault, "root", None) \
                          or getattr(_raw_vault, "vault_root", None) \
                          or "(unknown)"
        _proj_root = state.project_root if state else "(state init failed)"
        _mode = (os.environ.get("SYSTEMU_MODE") or getattr(config, "systemu_mode", None) or "local")
        _storage = (os.environ.get("SYSTEMU_STORAGE") or getattr(config, "storage_backend", None) or "file")
        _banner_lines = [
            "=" * 70,
            "Systemu daemon configured:",
            f"  Project root:       {_proj_root}",
            f"  Vault:              {_vault_root_abs}",
            *provider_banner_lines(config),
            f"  Storage backend:    {_storage}",
            f"  Mode:               {_mode}",
            f"  Listening on:       http://127.0.0.1:{port}",
            "=" * 70,
        ]
        for line in _banner_lines:
            logger.info("[Daemon] %s", line)
        # Also print to stdout for foreground daemons so operators see it
        # without tail-ing the log file.
        for line in _banner_lines:
            print(line, flush=True)
    except Exception:
        logger.exception("[Daemon] startup banner emit failed (non-fatal)")

    # F5: the "vault is empty — run `sharing_on init`" advice used to be emitted
    # HERE, ~70 lines before the seed migrator that fixes the very condition it
    # complains about. It now lives in _seed_and_advise(), immediately after the
    # migrator.

    # Initialise jobs (sets module-level config/vault globals)
    init_jobs(config, vault)

    # v0.6.8-d: seed tool_dep_approvals from the baked requirements file.
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

    # v0.7-c: migrate skills to Anthropic Agent Skills spec-conformant layout.
    # Idempotent; best-effort.  Runs once at boot to fix legacy skill_skill_<hash>/
    # directories on operator upgrade.
    try:
        from systemu.storage.skill_migrator import migrate_skill_layout
        # The mint, not a second relative default: this migrator REWRITES the
        # skill layout on disk, and a `systemu/vault` resolved against the
        # wrong cwd is how it came to rewrite the PACKAGED seed catalog.
        from systemu.runtime.vault_root import resolve_vault_root as _rvr
        vault_dir = Path(_rvr().root)
        report = migrate_skill_layout(vault_dir)
        if report.migrated:
            logger.info(
                "[Daemon] v0.7-c: migrated %d skill(s) to spec-conformant layout "
                "(skipped %d already-conformant, %d collisions)",
                report.migrated, report.skipped, report.collisions,
            )
        elif report.skipped:
            logger.info(
                "[Daemon] v0.7-c: all %d skill(s) already spec-conformant",
                report.skipped,
            )
        if report.errors:
            for err in report.errors:
                logger.warning("[Daemon] v0.7-c: skill migration error: %s", err)
    except Exception:
        logger.exception("[Daemon] v0.7-c: skill_migrator failed — continuing boot")

    # v0.8.22 (A): silent vault upgrade migrator. Mirrors v0.7-c skill_migrator
    # pattern — best-effort; daemon boots even on migrator failure.
    # F5: seeding and the "vault is empty" advice are ONE ordered step now.
    _seed_and_advise(vault, logger_=logger)

    # v0.9.51: re-validate tools whose dry-run failed under an OLDER version — a
    # fix in this upgrade may now make them pass (e.g. the deterministic param-synth
    # no longer injects a junk `dry_run` key). Bounded: a current-version failure is
    # left alone. Best-effort; never blocks boot.
    try:
        from systemu.scheduler.tool_reconciler import recover_stale_dry_run_failures
        recover_stale_dry_run_failures(vault)
    except Exception:
        logger.exception("[Daemon] v0.9.51: stale dry-run recovery failed — continuing boot")

    # W12 (ship-blocker, audit-caught live): under the daemon there IS a
    # decision queue and a dashboard — operator asks must route there, not
    # silently auto-skip headless. Without this, the first recorded
    # workflow's "New Shadow Recommended" ask auto-skipped and the pipeline
    # dead-ended at an unassigned activity on EVERY default install.
    # setdefault: an operator's explicit SYSTEMU_DECISION_QUEUE=false wins.
    os.environ.setdefault("SYSTEMU_DECISION_QUEUE", "true")

    # W11.3: ensure the install is actually set up — auto-fix what's safe
    # (directories), log loudly what the operator still has to do. Boot never
    # fails on this.
    try:
        from systemu.runtime.first_run import auto_setup, setup_status
        for _fix in auto_setup(config, vault):
            logger.info("[FirstRun] %s", _fix)
        _missing = [c for c in setup_status(config, vault)
                    if c["required"] and not c["ok"]]
        if _missing:
            # F3: this used to say "the dashboard will walk the operator
            # through it", which is false in a container — the dashboard
            # redirects to the welcome screen until these are satisfied, and
            # a headless box has no browser to satisfy them in. Log the
            # argv-only remedy each gate declares instead.
            logger.warning(
                "[FirstRun] setup incomplete — %s",
                ", ".join(c["id"] for c in _missing))
            for _c in _missing:
                _hint = (_c.get("headless") or {}).get("hint")
                logger.warning("[FirstRun]   %s: %s", _c["id"],
                               _hint or "complete it in the dashboard")
            logger.warning("[FirstRun]   full checklist: "
                           "`systemu onboarding status`")
    except Exception:
        logger.exception("[Daemon] first-run check failed — continuing boot")

    # v0.8.22.1 (R5): resume chat tasks when their stuck-loop decision is resolved.
    try:
        from systemu.runtime.resume_on_decision import register as _register_resume
        from systemu.runtime.supervisor import Supervisor
        _register_resume(vault, Supervisor.get(), data_dir=Path("data"))
    except Exception:
        logger.exception("[Daemon] v0.8.22.1: resume-on-decision registration failed — continuing boot")

    # W10.1: Telegram reach — the gateway + EventBus pusher shipped complete
    # in v0.8.x and were started by NOTHING. With the bot token + allowlist
    # env set, needs-you items and task outcomes now reach the operator's
    # phone, and /status answers from there. Best-effort: boot never fails
    # on messaging.
    try:
        from systemu.messaging.telegram_gateway import build_from_env as _tg_build
        from systemu.messaging.handlers import build_status_handler, default_handlers
        from systemu.messaging.event_pusher import EventPusher
        from systemu.interface.event_bus import EventBus
        _tg_handlers = {**default_handlers(),
                        "status": build_status_handler(vault)}
        _tg_gateway = _tg_build(command_handlers=_tg_handlers)
        if _tg_gateway is not None:
            _tg_gateway.start()
            _tg_pusher = EventPusher(_tg_gateway)
            _tg_pusher.subscribe(EventBus.get())
            logger.info("[Daemon] Telegram gateway + event pusher started")
        else:
            logger.info("[Daemon] Telegram not configured — messaging dormant")
    except Exception:
        logger.exception("[Daemon] messaging startup failed — continuing boot")

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
    # v0.9 Phase-5 3f: cadence is operator-tunable via SYSTEMU_EVOLUTION_HOUR
    # (Settings → Evolution schedule). Default 03:00 UTC — after memory
    # consolidation. The cron trigger is fixed at boot, so a changed hour needs
    # a daemon restart (the Settings card says so).
    try:
        _evolution_hour = int(os.environ.get("SYSTEMU_EVOLUTION_HOUR", "3"))
        if not (0 <= _evolution_hour <= 23):
            _evolution_hour = 3
    except (TypeError, ValueError):
        _evolution_hour = 3
    scheduler.add_job(
        daily_evolution_check,
        trigger="cron",
        hour=_evolution_hour,   # default 03:00 UTC — after memory consolidation
        minute=0,
        id="evolution_check",
        name="Daily Evolution Check",
        replace_existing=True,
    )
    # v0.9.6 L7: inactivity-triggered curator. Checked hourly; the heavy
    # lifecycle pass only fires when curator.should_run() says the configured
    # interval (default weekly) has elapsed AND curator is enabled + unpaused.
    scheduler.add_job(
        curator_review_job,
        trigger="interval",
        hours=1,
        id="curator_review",
        name="Idle-Triggered Curator Review",
        replace_existing=True,
    )

    # v0.7.4 Pattern 2: tool lifecycle reconciler — advance FORGED tools
    # to DEPLOYED via dry-run on a short interval. Closes the Bug #22 gap
    # where forged tools never reached the runtime.
    from systemu.scheduler.tool_reconciler import reconcile_once as _reconcile_tools
    def _reconcile_tools_job():
        try:
            _reconcile_tools(vault, config)
        except Exception:
            logger.exception("[Daemon] tool reconciler job crashed")

    scheduler.add_job(
        _reconcile_tools_job,
        trigger="interval",
        seconds=30,
        id="tool_reconciler",
        name="Tool Lifecycle Reconciler",
        replace_existing=True,
    )

    # T1b (spec §5.10): OnTheTable projection reconciler — keeps the read-only
    # /table snapshot warm by projecting the operational stores (MCP servers,
    # tool catalog, credential names) into <vault>/table/items.json. Read-only,
    # deterministic, idempotent, never raises (a bad tick leaves the last
    # snapshot in place). The page also projects on demand, so this is warmth,
    # not correctness.
    from systemu.runtime.table_reconciler import reconcile_once as _reconcile_table
    def _reconcile_table_job():
        try:
            _reconcile_table(vault)
        except Exception:
            logger.exception("[Daemon] table reconciler job crashed")

    scheduler.add_job(
        _reconcile_table_job,
        trigger="interval",
        seconds=60,
        id="table_reconciler",
        name="OnTheTable Projection Reconciler",
        replace_existing=True,
    )

    # R-CAP1 · CAP-2 — the capability index reconciler: derives
    # capabilities/capability_index.json from {Tool catalog ∪ mcp enabled_tools}
    # (usage read from capability_ledger). SOLE writer of that store (CAP-0.1) —
    # derive-only + idempotent + never raises, so it's warmth not correctness
    # (find_tools(live=True) reads fresh without writing).
    from systemu.runtime import capability_index as _capidx
    def _reconcile_capability_index_job():
        try:
            _capidx.reconcile_index(vault)          # SOLE writer of the index (CAP-0.1)
        except Exception:
            logger.exception("[Daemon] capability index reconciler job crashed")

    scheduler.add_job(
        _reconcile_capability_index_job,
        trigger="interval",
        seconds=60,
        id="capability_reconciler",
        name="Capability Slots Index Reconciler",
        replace_existing=True,
    )

    # Recovery scan-to-queue + stale-gate reconciler: recovery has no persisted
    # producer (diagnoses are on-demand scans), so this daemon job IS the
    # producer — it scans the vault, enqueues a recovery gate for every current
    # diagnosed action (idempotent via dedup) and expires pending recovery gates
    # whose action has self-healed. Best-effort (never crashes the tick).
    from systemu.scheduler.jobs import _recovery_gate_reconciler_job
    scheduler.add_job(
        _recovery_gate_reconciler_job,
        trigger="interval",
        seconds=45,
        id="recovery_gate_reconciler",
        name="Recovery Gate Scan-to-Queue Reconciler",
        replace_existing=True,
    )

    # v0.8.6: scheduled execute dispatcher — fires due schedules through JobManager
    from systemu.scheduler.jobs import _scheduled_execute_job
    scheduler.add_job(
        _scheduled_execute_job,
        trigger="interval",
        minutes=1,
        id="scheduled_execute",
        name="Scheduled Execute Dispatch",
        replace_existing=True,
    )

    # v0.8.22.1 follow-up: cross-process safety net for resume-after-decision.
    # The EventBus subscriber registered above only fires for in-daemon
    # resolutions. CLI `sharing_on decisions resolve` runs in a separate
    # process — its EventBus publish never reaches the daemon. This poll
    # catches those out-of-process resolutions and triggers the same
    # re-dispatch. Cheap (one index read per tick) and idempotent
    # (persisted decision.context["resume_dispatched"] flag prevents
    # double-dispatch across both paths and across restarts).
    from systemu.scheduler.jobs import _resume_on_decision_reconciler_job
    scheduler.add_job(
        _resume_on_decision_reconciler_job,
        trigger="interval",
        seconds=15,
        id="resume_on_decision_reconciler",
        name="Resume-on-Decision Cross-Process Reconciler",
        replace_existing=True,
    )

    # Harness grant-resume executor (Task 5/6): resolve_gate keeps an operator-
    # resolved harness ESCALATE QUEUED — this poll picks it up, materialises the
    # granted capability ONCE via the Governor (on Approve/Edit spec), and calls
    # Supervisor.resume_after_grant. Idempotent via a DISTINCT persisted flag
    # (decision.context["harness_grant_dispatched"]) so it never collides with
    # the resume-on-decision reconciler above.
    from systemu.scheduler.jobs import _harness_grant_reconciler_job
    scheduler.add_job(
        _harness_grant_reconciler_job,
        trigger="interval",
        seconds=15,
        id="harness_grant_reconciler",
        name="Harness Grant Reconciler",
        replace_existing=True,
    )

    # P4 MCP OAuth follow-up reconciler: a URL-mode OAuth handoff parks the run
    # ASSIGNED until the operator finishes out-of-band. This poll picks up the
    # resolved follow-up gate and calls Supervisor.resume_after_grant. Idempotent
    # via its OWN persisted flag (decision.context["mcp_oauth_dispatched"]) — it
    # NEVER stamps harness_grant_dispatched, so the original escalation can still
    # complete on its own gate.
    from systemu.scheduler.jobs import _mcp_oauth_reconciler_job
    scheduler.add_job(
        _mcp_oauth_reconciler_job,
        trigger="interval",
        seconds=15,
        id="mcp_oauth_reconciler",
        name="MCP OAuth Follow-up Reconciler",
        replace_existing=True,
    )

    # R-A12a external-event wait reconciler: fires durable retry timers persisted in
    # ExecutionSnapshot.pending_waits (armed by the supervisor's retry path instead of
    # an in-process threading.Timer, so a retry armed before a restart still fires). It
    # is the 4th CONC-MAP-allowed write_snapshot writer (DEC-10 reviewed) and mutates a
    # run's snapshot ONLY while that run is PARKED (per-execution_id invariant).
    from systemu.scheduler.jobs import _external_wait_reconciler_job
    scheduler.add_job(
        _external_wait_reconciler_job,
        trigger="interval",
        seconds=15,
        id="external_wait_reconciler",
        name="External-Event Wait Reconciler",
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

    # v0.8.6: manual event bridge — surface subprocess events to dashboard
    from pathlib import Path as _Path
    try:
        from systemu.interface.manual_event_bridge import ManualEventBridge
        ManualEventBridge.start(str(_Path(config.vault_dir).resolve()))
    except Exception:
        logger.exception("[Daemon] manual event bridge failed to start — non-fatal")

    logger.info("[Daemon] Scheduler started. PID=%d | Port=%d", os.getpid(), port)
    logger.info(
        "[Daemon] Jobs: shadow sweep (hourly) | memory consolidation (02:00) | "
        "evolution check (03:00) | curator review (hourly check, weekly pass) | "
        "tool reconciler (every 30s) | "
        "scheduled execute (every 1min) | "
        "resume-on-decision reconciler (every 15s)",
    )

    # ── Start NiceGUI dashboard in a background thread ─────────────────────
    # W13.7 (field hazard: 5 daemons racing one port): fail FAST when the
    # port is taken — the loser of the race used to keep running headless
    # while recordings/decisions landed in whichever vault won.
    if not _dashboard_port_free(port):
        logger.critical(
            "[Daemon] Port %d is already in use — another systemu daemon? "
            "Refusing to start so recordings can't land in the wrong vault. "
            "Stop the other instance (`systemu daemon stop --all`) or use "
            "a different --port.", port)
        return

    # F21: check BEFORE spawning. `run_dashboard_thread` returns as soon as the
    # thread starts, so an exception raised inside `run_dashboard` cannot reach
    # the `except` below — the daemon would log "Dashboard thread launched on
    # http://127.0.0.1:8765" and then serve nothing. The honest state is known
    # here, synchronously, and is announced instead of a URL that will not load.
    from systemu.runtime import optional_deps as _od
    _dash_missing = _od.missing_groups(("nicegui",))
    if _dash_missing:
        logger.warning(
            "[Daemon] NO DASHBOARD — %s The daemon itself is running normally; "
            "scheduled jobs, recordings and the CLI all work. Nothing will "
            "serve http://127.0.0.1:%d until you install it.",
            _od.unavailable_reason(("nicegui",)), port,
        )
    else:
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
        _cleanup_runtime_files()
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
            "`systemu tools deps doctor` for details.",
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

    # ── THE VAULT-ROOT BOUNDARY (DEC-32) ────────────────────────────────────
    # Before the first byte is written anywhere: mint the absolute operating
    # root and refuse if it lands inside the installed package. Everything below
    # — the log file, the pidfile, the Vault itself — hangs off `_vault_root`,
    # never off the raw argument.
    _vault_root = resolve_child_vault_dir(args.vault_dir)

    import logging
    import logging.handlers

    Path(_vault_root).mkdir(parents=True, exist_ok=True)
    log_file_path = str(Path(_vault_root) / "systemu_exec.log")

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

    # The child's own env now names the SAME absolute root the parent minted
    # (start_daemon assigns it), so Config.from_env() cannot pick a different
    # vault than the one this process was told to serve.
    os.environ["SYSTEMU_VAULT_DIR"] = _vault_root
    _config = Config.from_env()
    _vault = Vault(_vault_root)
    pid_file = _pid_file_path(_vault_root)

    # ── v0.8.0.2: refuse to start when port is already in use ───────────────
    _bind_host = "127.0.0.1"
    if not _check_port_available(_bind_host, args.port):
        _existing_pid = _find_listening_pid(args.port)
        _pid_info = f"PID {_existing_pid}" if _existing_pid else "an unknown process"
        print(
            f"\n[ERROR] Port {args.port} on {_bind_host} is already in use by {_pid_info}.\n"
            f"\n"
            f"  Another systemu daemon (or another app) is bound to this port.\n"
            f"  Running two daemons on the same port creates a silent failure\n"
            f"  where whichever wins the race serves your dashboard with the\n"
            f"  wrong vault. Recordings + decisions land in the wrong place.\n"
            f"\n"
            f"  Fix one of:\n"
            f"    systemu daemon stop --all              # kill ALL systemu daemons\n"
            f"    SYSTEMU_DASHBOARD_PORT=8766 systemu daemon start   # use a different port\n",
            file=sys.stderr,
        )
        sys.exit(1)

    _run_daemon_loop(_config, _vault, args.port, pid_file)
