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

    v0.10.28 (D2): and the COUNT itself was wrong. It came off a
    ``psutil.process_iter`` SNAPSHOT, which is a RECORD, not an observation of
    a live process -- so a daemon that had exited, a pid the OS had reissued to
    something unrelated, or a process that was no longer the daemon all still
    counted. Dogfooding, the banner said "5 daemon processes" with two alive,
    then "4" with one. Now every record is PID-VERIFIED-LIVE against the OS
    (``verify_live_daemon``) and de-duplicated by pid before it is counted,
    stale records are pruned from the scan cache they came from, and the banner
    NAMES each live pid, port and interpreter so the claim can be checked.
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
    exe:     Optional[str] = None   # the interpreter path, so the claim is checkable


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


def _marker_in(cmdline) -> bool:
    """Does this command line belong to a systemu daemon? Never raises."""
    if type(cmdline) is not list and type(cmdline) is not tuple:
        return False
    joined = " ".join(a for a in cmdline if type(a) is str)
    return _DAEMON_CMDLINE_MARKER in joined


def verify_live_daemon(proc):
    """Re-ask the OS about ``proc`` NOW -> its LIVE command line, or None.

    D2 (0.10.28 dogfood): the banner reported "5 systemu daemon processes are
    running" on a machine with two, then "4" with one, and prescribed
    ``systemu daemon stop --all`` -- which would have stopped the healthy
    daemon. It cried wolf often enough to train the operator to ignore it.

    THE DEFECT IS A CATEGORY ERROR. ``psutil.process_iter`` hands back a
    SNAPSHOT, and a snapshot is a RECORD, not an observation of a live process.
    By the time a row is read the process may have exited, its pid may have
    been reissued to something unrelated, or its command line may no longer be
    the daemon's. The old scan counted the record. This function counts only
    what the OS still stands behind:

      * ``is_running()`` must be True NOW -- and psutil compares the process
        CREATION TIME as well as the pid, so a reissued pid answers False;
      * the command line read NOW must still carry the daemon marker, which
        catches both a reissued pid whose creation time psutil could not read
        and a process that is simply no longer the daemon.

    Returns the live command line so the caller parses the port and vault the
    OS reports now -- never the snapshot's, which may describe a dead process.
    Never raises: a probe of somebody else's process may fail in any way, and a
    record we could not verify is NOT a live daemon.
    """
    try:
        if proc.is_running() is not True:
            return None
    except Exception:
        return None
    try:
        live = proc.cmdline()
    except Exception:
        return None
    if type(live) is not list:
        return None
    return live if _marker_in(live) else None


def _best_effort_str(fn) -> Optional[str]:
    """``fn()`` as a non-empty str, or None. Probes of another process fail."""
    try:
        value = fn()
    except Exception:
        return None
    return value if type(value) is str and value else None


#: How far up a launcher chain the scan walks. A venv redirector is one hop;
#: the bound is the rule's, not the tree's, so a cycle or a pathological chain
#: cannot spin here.
_MAX_LAUNCHER_DEPTH = 12


def _ancestor_pids(proc) -> set:
    """``proc``'s launcher chain, as pids. Never raises.

    A walk over somebody else's process tree: every hop may fail (the parent
    exited, access denied, a hostile object), and a hop we could not take ends
    the chain rather than inventing one.
    """
    out = set()
    current = proc
    for _ in range(_MAX_LAUNCHER_DEPTH):
        try:
            current = current.parent()
        except Exception:
            break
        if current is None:
            break
        try:
            pid = current.pid
        except Exception:
            break
        # DEC-36: pin the concrete type in the frame that uses it. A pid we
        # cannot read as an int is a link we cannot follow.
        if type(pid) is not int or pid in out:
            break
        out.add(pid)
    return out


def launcher_stub_pids(rows) -> frozenset:
    """Which of the same-argv ``(pid, proc)`` rows are LAUNCHERS, not daemons.

    P0 (0.10.30 dogfood). On a Windows venv install ``python -m venv`` writes
    ``Scripts/python.exe`` as ``venvlauncher.exe``: a stub that
    CreateProcess-es the base interpreter with the SAME argv and waits on it in
    a job object. Two process rows, ONE daemon -- the fact ``daemon start`` and
    ``daemon status`` now say out loud.

    The banner did not know it. The stub carries the daemon's command line, is
    genuinely alive, and has no port of its own -- the runtime sidecar records
    the CHILD's pid, so ``_recorded_port`` rightly refuses to lend it the
    child's. Two counted processes, one with an unknowable port, is exactly the
    fail-closed shape ``daemon_conflict_issue`` answers with DANGER and
    ``stop --all``. So the banner cried wolf on every healthy single-daemon
    venv install on Windows, and its remedy would have stopped the only daemon.

    THE RULE IS THE D1b FENCE'S, CONSUMED AND NOT RE-DERIVED.
    ``tests/test_dogfood28_d1_one_daemon_per_vault_and_port.py`` (b.1) asserts
    that for one vault and one port exactly one process EXECUTES the daemon and
    every other process carrying that argv is an ANCESTOR of it -- the launcher
    on the way in. A SIBLING or a DESCENDANT is a rival for the port writing the
    same vault, and stays counted.

    Decided over the SNAPSHOT set, BEFORE liveness verification. That ordering
    is load-bearing in one direction: a stub whose child has already exited is a
    stub on its way out of a job object, not a daemon that is up, and it is the
    dead child's row that identifies it as one.

    WHAT THIS DOES NOT COVER, said plainly (DEC-34: a false assertion of
    enforcement is itself a defect). A daemon that spawned a second daemon is an
    ancestor of it too, and this rule reads the first one as a launcher. That
    hole is named and closed by that same fence's b.5, whose witness is the
    vault's append-only exec log rather than parentage -- parentage cannot see
    it, so widening this rule to try would be the manufactured second layer
    DEC-34 forbids. The banner's job here is the launcher pair.
    """
    pids = {pid for pid, _proc in rows if type(pid) is int}
    stubs = set()
    for pid, proc in rows:
        if type(pid) is not int:
            continue
        for ancestor in _ancestor_pids(proc):
            if ancestor in pids and ancestor != pid:
                stubs.add(ancestor)
    return frozenset(stubs)


def _scan_daemon_processes() -> tuple:
    """The psutil scan, PID-VERIFIED-LIVE — best-effort, empty on error. SLOW
    on Windows (cmdline for every process ~ 1 s); only ever call via the cache
    below.

    Returns one :class:`DaemonProcess` per VERIFIED-LIVE daemon, de-duplicated
    by pid, carrying whatever of (port, vault, cwd, exe) that daemon's own
    LIVE launch line or sidecar could establish. The pids of the records that
    did not survive verification are kept in the scan cache (see
    ``pruned_daemon_pids``) so the prune is visible rather than silent.

    P0: two passes, and the order matters. The first collects every snapshot row
    carrying the daemon marker; ``launcher_stub_pids`` then decides which of
    them are venv launcher stubs, from the parentage among those rows and BEFORE
    liveness is verified. Only the survivors are probed and counted. Launchers
    are visible too (``excluded_launcher_pids``) and are kept distinct from
    pruned records: a pruned pid FAILED liveness, a launcher passed it and
    simply is not a daemon.
    """
    try:
        import psutil
    except Exception:
        return ()
    self_pid = os.getpid()
    found = []
    pruned = []
    rows = []
    seen = set()
    try:
        for proc in psutil.process_iter(["pid", "cmdline"]):
            try:
                info = getattr(proc, "info", None)
                info = info if type(info) is dict else {}
                if not _marker_in(info.get("cmdline")):
                    continue
                raw_pid = info.get("pid")
                # DEC-36: pin the concrete type in this frame. A pid we cannot
                # read is a record we cannot verify, and an unverifiable record
                # is not a live daemon (DEC-27: completeness is witnessed).
                if type(raw_pid) is not int:
                    continue
                if raw_pid in seen:
                    continue          # the same process, counted once
                seen.add(raw_pid)
                rows.append((raw_pid, proc))
            except Exception:
                continue
    except Exception:
        return ()

    launchers = launcher_stub_pids(rows)
    for raw_pid, proc in rows:
        try:
            if raw_pid in launchers:
                continue      # a launcher on the way in, never a daemon
            live_cmdline = verify_live_daemon(proc)
            if live_cmdline is None:
                pruned.append(raw_pid)
                continue
            port, vault = parse_daemon_argv(live_cmdline)
            cwd = _best_effort_str(proc.cwd)
            exe = _best_effort_str(proc.exe)
            if port is None:
                port = _recorded_port(vault, cwd, raw_pid)
            found.append(DaemonProcess(
                pid=raw_pid, port=port, vault=vault, cwd=cwd, exe=exe,
                # build_health_state runs inside the daemon thread that is
                # serving this very page (daemon.py -> run_dashboard_thread),
                # so our own pid names the daemon the operator must KEEP.
                is_self=(raw_pid == self_pid),
            ))
        except Exception:
            continue
    _daemon_procs_cache["pruned"] = tuple(pruned)
    _daemon_procs_cache["launchers"] = tuple(sorted(launchers))
    return tuple(found)


def pruned_daemon_pids() -> tuple:
    """The pids the last scan REFUSED to count: dead, reissued, or no longer
    the daemon. Evidence that the prune happened, for tests and for support."""
    value = _daemon_procs_cache.get("pruned")
    return value if type(value) is tuple else ()


def excluded_launcher_pids() -> tuple:
    """The pids the last scan excluded as LAUNCHER STUBS, not as stale records.

    Kept apart from ``pruned_daemon_pids`` because the two mean different
    things: a pruned pid failed liveness verification, a launcher passed it and
    is simply not a daemon. A support conversation that cannot tell them apart
    is back to arguing about a row count.
    """
    value = _daemon_procs_cache.get("launchers")
    return value if type(value) is tuple else ()


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
#: The ONE source the per-process records come from. Stale records are pruned
#: HERE -- they never enter it -- so nothing downstream has to know they existed.
#: No durable file is written or deleted by this module (see docs/CONC-MAP.md:
#: the daemon runtime sidecar keeps its single registered writer, daemon.py).
_daemon_procs_cache: dict = {"ts": -1e9, "procs": (), "pruned": (), "launchers": ()}


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


#: The hedge the copy carries while the keyless witness has NOT been spent.
#: "We have not asked" is a third answer, distinct from "it is down".
PROVIDER_CHECKING_PREFIX = "Checking providers... "


def _provider_view(config=None, probe=None) -> tuple:
    """``(statuses, observed, age_s_or_None)`` -- and NEVER a socket.

    F19: this was ``_openrouter_key_present()`` -- ``bool(os.environ[
    "OPENROUTER_API_KEY"])`` -- so the banner nagged an operator who had a
    perfectly good Google key, or a running Ollama, to add an OpenRouter one.
    That is one of the SIX private copies of the satisfaction recipe F19 closed,
    and ``provider_status`` is still THE ONE MINT for every verdict below.

    D4 (0.10.28 dogfood): what it must NOT do is spend the keyless witness
    HERE. ``build_health_state`` runs on EVERY route render, and the witness is
    a bounded but real loopback connect (1.7 s with Ollama running on the
    shipped dual-stack default, ~2 s with nothing listening) -- a blocking
    socket inside layout, which is what froze the renderer. So:

    * an EXPLICIT ``probe`` means a non-render caller (doctor, a unit test)
      asked for a synchronous verdict with a witness of its own -- honoured;
    * otherwise the published :class:`~systemu.runtime.provider_snapshot.
      HealthSnapshot` is read, and a refresh is NUDGED (never awaited);
    * with no snapshot yet, the mint is asked with its OWN NULL WITNESS
      (``unprobed``): zero network, and ``STATE_UNKNOWN`` is not in
      ``SATISFIED_STATES``, so declining to pay for the witness can only ever
      WITHHOLD a claim, never manufacture one. ``observed`` is False, and the
      copy says so.
    """
    from systemu.runtime import provider_status as _ps
    from systemu.runtime import provider_snapshot as _snap

    if probe is not None:
        try:
            view = _ps.env_overlay(config) if config is not None else \
                _snap.resolve_config(None)
            return (_ps.all_provider_statuses(view, probe=probe), True, None)
        except Exception:
            return ({}, True, None)

    try:
        view = _snap.resolve_config(config)
    except Exception:
        view = None
    if view is None:
        return ({}, False, None)

    try:
        snap = _snap.current_snapshot(_snap.config_key(view))
    except Exception:
        snap = None
    fresh = snap is not None and bool(snap.statuses) and not snap.stale
    if not fresh:
        # Nudge ONLY when the answer we have is missing or past its interval.
        # Nudging on every render would keep the refresher probing back to back
        # on a busy dashboard -- the render would have stopped paying for the
        # socket and started paying for it in someone else's thread instead.
        _snap.request_refresh()      # non-blocking; False if none is running
    if snap is not None and snap.statuses:
        return (snap.statuses, True, snap.age_s() if snap.stale else None)

    try:
        return (_ps.all_provider_statuses(view, probe=_ps.unprobed), False, None)
    except Exception:
        return ({}, False, None)


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


def _named(value, unknown: str = "(not recorded)") -> str:
    """A field of somebody else's process, or a plain admission it is unknown."""
    if type(value) is int:
        return str(value)
    return value if type(value) is str and value else unknown


def daemon_listing(daemons: Sequence["DaemonProcess"]) -> str:
    """"PID p - port n - vault v - python exe", one per VERIFIED-LIVE daemon.

    D2: the banner used to assert a count and name nothing, so an operator who
    doubted it had no way to check it -- and this one was wrong often enough to
    be worth doubting. Every field is the daemon's OWN (its live argv, its own
    sidecar, its own interpreter); a field we could not read says so rather
    than being filled in with a plausible default.
    """
    parts = []
    for d in daemons:
        if type(d) is not DaemonProcess:
            continue
        parts.append(
            "PID {} - port {} - vault {} - python {}".format(
                _named(d.pid, "unknown"),
                _named(_known_port(d.port), "unknown"),
                _named(d.vault), _named(d.exe)))
    return "; ".join(parts)


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
        named = daemon_listing(sorted(records,
                                      key=lambda d: _known_port(d.port) or 0))
        return HealthIssue(
            severity="danger",
            message=("{} systemu daemon processes are running. ".format(count)
                     + _PORT_RACE_MESSAGE_TAIL
                     + (" Verified live: {}.".format(named) if named else "")),
            cta=_PORT_RACE_CTA,
        )

    ordered = sorted(records, key=lambda d: _known_port(d.port) or 0)
    listing = daemon_listing(ordered)

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
    _statuses, _observed, _age = _provider_view(config, provider_probe)
    if not _ps.any_satisfied(_statuses):
        if not _observed:
            # The keyless witness has NOT been spent on this render (D4). The
            # credentials genuinely are absent -- that costs no network to
            # establish -- but whether Ollama answers is not yet known, and the
            # banner must not assert what it declined to look at.
            message = (
                PROVIDER_CHECKING_PREFIX
                + "No LLM provider is usable from the daemon's environment so "
                "far: no credential is set, and whether the keyless provider "
                "answers has not been checked yet. Until one is usable, "
                "LLM-driven steps (capture analysis, scroll refinement) will "
                "fail silently and you'll only get raw captured events."
            )
        else:
            _as_of = ("" if _age is None
                      else " (as of {} s ago)".format(int(_age)))
            message = (
                "No LLM provider is usable from the daemon's environment"
                + _as_of + ". LLM-driven steps (capture analysis, scroll "
                "refinement) will fail silently and you'll only get raw "
                "captured events."
            )
        state.issues.append(HealthIssue(
            severity="warning",
            message=message,
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

    # D4: declining to probe on the render path is only honest if something
    # else does. This is idempotent and costs a lock and a flag read once the
    # thread is up; it is the production start point for the refresher, on the
    # one function every dashboard route reaches.
    try:
        from systemu.runtime import provider_snapshot as _snap
        _snap.start_refresher()
    except Exception:
        pass  # a refresher we could not start leaves the banner at "checking"

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
