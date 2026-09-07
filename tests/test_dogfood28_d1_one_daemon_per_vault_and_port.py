"""DOGFOOD-28 D1 (fence b) -- one EXECUTING daemon per vault+port, witnessed.

THE OPERATOR'S WITNESS
    On Windows 11, fresh venv, shipped wheel: every daemon start produced two
    live processes carrying identical daemon argv -- one under the venv
    interpreter, one under the base interpreter -- parent and child, and killing
    either killed both. It reads exactly like a second spawn racing the first
    for the port.

WHAT IT ACTUALLY IS
    The CPython venv redirector. On Windows ``python -m venv`` installs
    ``Scripts/python.exe`` as ``venvlauncher.exe``, a stub that CreateProcess-es
    the base interpreter with the SAME argv and waits on it inside a job object.
    Two rows, one daemon: only the child ever imports systemu, only the child
    binds the port, and the pair lives and dies together because that is what a
    job object does.

    So "count the rows" is not the property. The row count is a fact about the
    operator's Python installation, not about this program.

PROPERTY (the one that is really about us)
    For one vault and one port, exactly ONE process ever EXECUTES the daemon,
    and every other process carrying that argv is a launcher on the way in.

    Reachability, not just shape: the daemon is started through the REAL
    production argv (``start_daemon``'s ``cmd``), launched through a REAL venv
    interpreter, and every witness below is something the daemon wrote ABOUT
    ITSELF.

TWO WITNESSES, AND WHY IT TAKES TWO
    b.1--b.4 read the vault's runtime SIDECAR: the recorded pid names the
    executing process, and every other same-argv process must be an ancestor of
    it (a launcher stub is one). That is necessary and it is not sufficient,
    and the gap was measured rather than argued: with the identity guard
    disarmed and a re-exec hop on the boot path, a rival daemon for the same
    vault and the same port reached the loop within two seconds, OVERWROTE the
    sidecar with its own pid, and thereby turned the original into "an ancestor
    of the recorded pid" -- all four passed with two daemons live.

    The sidecar is one slot with no author, so the last writer wins and the
    earlier one leaves no trace there. b.5 therefore reads the vault's exec LOG,
    which is appended to and never rewritten: each process that runs the loop
    announces itself there once, and a launcher stub -- which never imports
    systemu -- announces nothing. The count of announcements is the count of
    daemons, whatever the parentage between them.

WITNESS
    Add a second spawn anywhere on the boot path for this vault and port and
    b.5 goes red naming how many processes announced themselves. Let the
    parent's pid stand in for the child's and the recorded pid stops matching
    any executing process -- b.1 red.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

import systemu.scheduler.daemon as daemon_mod

psutil = pytest.importorskip("psutil")

#: How long the daemon gets to reach the point where it records its own pid.
#: `_run_daemon_loop` writes the sidecar near the TOP of the loop -- after the
#: scheduler imports, before the migrators and before the dashboard binds -- so
#: this is an import budget, not a boot budget.
_RECORD_TIMEOUT_S = 150.0

#: How long the vault's record is WATCHED after the daemon first writes it.
#: A rival spawned on the boot path reaches `_run_daemon_loop` and overwrites
#: the record within a couple of seconds on a warm box; this window is an order
#: of magnitude wider than that, so the witness does not depend on losing a
#: race it was written to detect.
_RECORD_WATCH_S = 20.0


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _repo_root() -> Path:
    return Path(daemon_mod.__file__).resolve().parent.parent.parent


@pytest.fixture(scope="module")
def venv_python(tmp_path_factory) -> str:
    """A REAL virtual environment, because the redirector only exists in one.

    ``--system-site-packages`` so the daemon's third-party imports resolve
    without a multi-minute install; the systemu under test still comes from the
    worktree via PYTHONPATH, so this venv can never shadow it.
    """
    root = tmp_path_factory.mktemp("d1venv") / "venv"
    proc = subprocess.run(
        [sys.executable, "-m", "venv", "--system-site-packages", str(root)],
        capture_output=True, text=True, errors="backslashreplace", timeout=300,
    )
    if proc.returncode != 0:
        pytest.skip(f"could not build a venv here: {proc.stderr[-400:]}")
    exe = root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not exe.exists():
        pytest.skip(f"venv has no interpreter at {exe}")
    return str(exe)


class _Daemon:
    """A started daemon plus everything needed to witness and to stop it.

    ``argv_tag`` is a per-run UUID baked into the vault directory NAME. It is
    not decoration: this class both selects processes to assert about and
    selects processes to kill, and a tag as generic as ``vault`` matches every
    other daemon on the box. A witness that cannot tell my process from
    somebody else's is not a witness, and a teardown built on one is a weapon.
    """

    def __init__(self, launcher: subprocess.Popen, vault: Path, port: int) -> None:
        self.launcher = launcher
        self.vault = vault
        self.port = port

    @property
    def argv_tag(self) -> str:
        return self.vault.name

    def _mine(self) -> set:
        """PIDs this test is responsible for: the launcher and its descendants.

        Membership is by PARENTAGE, never by argv text. Nothing outside this
        set is ever killed, whatever its command line says.
        """
        mine = {self.launcher.pid}
        try:
            for child in psutil.Process(self.launcher.pid).children(recursive=True):
                mine.add(child.pid)
        except Exception:
            pass
        return mine

    def recorded_pid(self):
        path = Path(daemon_mod._runtime_file_path(str(self.vault)))
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        pid = rec.get("pid") if type(rec) is dict else None
        return pid if type(pid) is int else None

    def same_argv_processes(self) -> list:
        """Every live process whose argv names THIS vault's daemon."""
        out = []
        for proc in psutil.process_iter(["pid", "ppid", "exe", "cmdline"]):
            try:
                argv = " ".join(proc.info["cmdline"] or [])
            except Exception:
                continue
            if "systemu.scheduler.daemon" in argv and self.argv_tag in argv:
                out.append(proc)
        return out

    def stop(self) -> None:
        """Kill only what this test started, then WITNESS that it is gone."""
        for pid in sorted(self._mine(), reverse=True):
            try:
                psutil.Process(pid).kill()
            except Exception:
                pass
        try:
            self.launcher.kill()
        except Exception:
            pass
        deadline = time.time() + 30
        while time.time() < deadline and self.same_argv_processes():
            time.sleep(0.4)
        left = [p.pid for p in self.same_argv_processes()]
        assert left == [], f"test left daemon processes running: {left}"


@pytest.fixture()
def started_daemon(venv_python, tmp_path):
    """Start the daemon exactly the way `start_daemon` does, and stop it."""
    home = tmp_path / "home"
    # A UUID in the vault NAME, so the argv of this daemon is unmistakable on a
    # box that is running several. `home` is also the pidfile's directory
    # (`_pid_file_path` puts it beside the vault), so a per-test `home` is what
    # keeps two vaults from sharing one single-instance record.
    vault = home / ("vault_" + uuid.uuid4().hex)
    vault.mkdir(parents=True)
    port = _free_port()

    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(_repo_root())] + [p for p in (env.get("PYTHONPATH") or "").split(os.pathsep) if p])
    env["SYSTEMU_VAULT_DIR"] = str(vault)
    env["SYSTEMU_DASHBOARD_PORT"] = str(port)
    env["SYSTEMU_HEADLESS"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # Never let a test start a 150 MB browser download.
    env["SYSTEMU_SKIP_BROWSER_AUTOINSTALL"] = "true"

    # THE PRODUCTION ARGV, byte for byte -- `start_daemon` builds exactly this
    # list. Launched through the venv interpreter so the redirector is in play.
    cmd = [venv_python, "-m", "systemu.scheduler.daemon",
           "--vault-dir", str(vault), "--port", str(port)]

    log = open(home / "daemon.log", "w", encoding="utf-8", errors="backslashreplace")
    launcher = subprocess.Popen(cmd, cwd=str(home), env=env,
                                stdout=log, stderr=subprocess.STDOUT)
    daemon = _Daemon(launcher, vault, port)
    try:
        deadline = time.time() + _RECORD_TIMEOUT_S
        while time.time() < deadline and daemon.recorded_pid() is None:
            if launcher.poll() is not None and daemon.recorded_pid() is None:
                log.close()
                tail = (home / "daemon.log").read_text(
                    encoding="utf-8", errors="backslashreplace")[-1500:]
                pytest.skip(f"daemon exited rc={launcher.returncode} before "
                            f"recording itself:\n{tail}")
            time.sleep(0.5)
        yield daemon
    finally:
        daemon.stop()
        try:
            log.close()
        except Exception:
            pass


def _ancestors(pid: int) -> set:
    out = set()
    try:
        proc = psutil.Process(pid)
    except Exception:
        return out
    for _ in range(12):
        try:
            proc = proc.parent()
        except Exception:
            break
        if proc is None:
            break
        out.add(proc.pid)
    return out


# ═════════════════════════════════════════════════════════════════════════════
#  b.1 -- exactly one process EXECUTES the daemon; the rest are launchers
# ═════════════════════════════════════════════════════════════════════════════

def test_exactly_one_process_executes_the_daemon_for_this_vault_and_port(started_daemon):
    recorded = started_daemon.recorded_pid()
    assert recorded is not None, (
        "the daemon never recorded its own pid -- nothing downstream can tell "
        "which of the processes carrying this argv is the one that is running")

    same_argv = {p.pid for p in started_daemon.same_argv_processes()}
    assert recorded in same_argv, (
        f"the recorded pid {recorded} is not among the processes carrying this "
        f"daemon's argv {sorted(same_argv)} -- the pid on record is not the "
        "process that is executing")

    rivals = same_argv - {recorded} - _ancestors(recorded)
    assert rivals == set(), (
        "a SECOND daemon is running for this vault and port. Processes carrying "
        f"the argv: {sorted(same_argv)}; the one executing: {recorded}; "
        f"neither it nor its launcher chain: {sorted(rivals)}. "
        "An ancestor is a launcher on the way in; a sibling or descendant is a "
        "rival for the port writing the same vault.")


def test_the_executing_daemon_runs_under_the_interpreter_we_launched(started_daemon,
                                                                     venv_python):
    """Not "a python" -- the one this launch named.

    The executing process may legitimately be the BASE interpreter (the venv
    redirector hands off to it), so the pin is on the chain: the interpreter we
    named must be the executing process or one of its ancestors. A child that
    PATH picked would be neither.
    """
    recorded = started_daemon.recorded_pid()
    assert recorded is not None

    chain = [recorded] + sorted(_ancestors(recorded))
    exes = []
    for pid in chain:
        try:
            exes.append(psutil.Process(pid).exe() or "")
        except Exception:
            exes.append("")
    launched = os.path.normcase(os.path.realpath(venv_python))
    normalised = [os.path.normcase(os.path.realpath(e)) if e else "" for e in exes]
    assert launched in normalised, (
        f"the interpreter this test launched ({venv_python}) is nowhere in the "
        f"executing daemon's process chain {list(zip(chain, exes))} -- the "
        "daemon is running under an interpreter nobody here chose")


def test_every_extra_row_is_a_launcher_that_never_imported_systemu(started_daemon):
    """The twin an operator sees in Task Manager holds no vault and no port.

    Only the recorded process may be listening on the daemon's port. A launcher
    stub is a row in a process list and nothing else.
    """
    recorded = started_daemon.recorded_pid()
    assert recorded is not None
    extras = {p.pid for p in started_daemon.same_argv_processes()} - {recorded}

    listeners = set()
    for conn in psutil.net_connections(kind="inet"):
        if conn.laddr and conn.laddr.port == started_daemon.port \
                and conn.status == psutil.CONN_LISTEN and conn.pid:
            listeners.add(conn.pid)
    assert not (listeners & extras), (
        f"a process that is not the recorded daemon ({sorted(listeners & extras)}) "
        f"is listening on port {started_daemon.port} -- two servers, one vault")


def test_the_pid_on_record_is_the_process_that_imported_this_systemu(started_daemon):
    """DEC-27: completeness witnessed, not inferred. The sidecar's build record
    is written only by the process that loaded the code, so a recorded build
    proves the recorded pid is the executing one and not a launcher stub."""
    path = Path(daemon_mod._runtime_file_path(str(started_daemon.vault)))
    rec = json.loads(path.read_text(encoding="utf-8"))
    assert type(rec.get("build")) is dict, (
        "the runtime sidecar carries no build record, so nothing witnesses that "
        "the recorded pid belongs to a process that actually imported systemu")
    assert rec["build"].get("version"), "the recorded build has no version"
    assert Path(rec["build"]["path"]).resolve() == \
        Path(daemon_mod.__file__).resolve().parent.parent, (
        "the daemon recorded a systemu package directory other than the one "
        "under test -- the venv is shadowing the worktree")


# ═════════════════════════════════════════════════════════════════════════════
#  b.5 -- the hole in b.1: "ancestor" is not proof of "launcher"
# ═════════════════════════════════════════════════════════════════════════════

#: The line `_run_daemon_loop` writes about ITSELF, once, from inside the loop.
#: A launcher stub never imports systemu, so it never writes this line.
_ANNOUNCE = "[Daemon] running systemu "


def _executing_daemon_count(vault: Path) -> int:
    """How many processes have EXECUTED the daemon loop for this vault.

    Every daemon process configures logging to ``<vault>/systemu_exec.log``
    before entering the loop and then announces itself there. The file is
    APPENDED to, never rewritten, so unlike the pid in the runtime sidecar a
    second daemon cannot erase the first one's evidence by arriving after it.
    """
    try:
        text = (vault / "systemu_exec.log").read_text(
            encoding="utf-8", errors="backslashreplace")
    except Exception:
        return 0
    return text.count(_ANNOUNCE)


def test_only_one_process_ever_announces_itself_as_this_vaults_daemon(started_daemon):
    """THE HOLE IN b.1, closed.

    b.1 lets a same-argv process through when it is an ANCESTOR of the recorded
    pid, because a venv launcher stub is one. A daemon that spawns a daemon for
    its own vault is an ancestor too -- and the rival, running the same loop,
    overwrites the vault's runtime sidecar with its OWN pid. From that instant
    the original is "an ancestor of the recorded pid" and b.1 reports one
    healthy daemon while two are live on one vault and one port.

    Measured, not reasoned about: with the identity guard disarmed and a
    re-exec hop on the boot path, the rival reached the loop and took the
    record inside two seconds, and all four tests above stayed green.

    The record cannot settle this, because it is one slot with no author: the
    last writer wins and the earlier one leaves no trace. The log can. Every
    process that actually runs the loop appends one announcement to the vault's
    exec log; a launcher stub, which never imports systemu, appends nothing. So
    the number of announcements IS the number of daemons that ran for this
    vault, whatever the parentage between them, and it is watched over a window
    an order of magnitude longer than a rival takes to boot -- a witness must
    not depend on winning a race it exists to detect.
    """
    deadline = time.time() + _RECORD_WATCH_S
    while time.time() < deadline:
        count = _executing_daemon_count(started_daemon.vault)
        assert count <= 1, (
            f"{count} processes have announced themselves as the daemon for "
            f"{started_daemon.vault.name} on port {started_daemon.port}. Only a "
            "process that imported systemu writes that line, so this is two "
            "daemons racing for one port and writing one vault -- not a "
            "launcher stub, whichever of them currently holds the sidecar.")
        time.sleep(0.5)

    assert _executing_daemon_count(started_daemon.vault) == 1, (
        "no process announced itself as this vault's daemon, so this test "
        "witnessed nothing at all -- an absent log is not a clean bill of "
        "health (DEC-27)")
