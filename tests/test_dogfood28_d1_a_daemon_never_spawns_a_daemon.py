"""DOGFOOD-28 D1 (fix 3) -- a daemon may never spawn a daemon for its own socket.

THE HOLE
    ``start_daemon``'s single-instance guard reads the PIDFILE. The pidfile is
    written by the PARENT immediately after ``Popen`` returns, and then
    OVERWRITTEN by the child with its own pid seconds later, once the scheduler
    imports finish. Between the two writes a `daemon stop` (which unlinks it),
    a crash, or a vault whose pidfile has not been reached yet leaves the guard
    with nothing to read -- and a second ``start_daemon`` for the same vault and
    port spawns a rival that races for the socket and writes the same vault.

    Nothing in the tree calls ``start_daemon`` from inside a daemon today. That
    is the point of a fence: this project already shipped a dispatcher-restart
    mechanism, and the moment one of those learns to restart the daemon
    PROCESS, the loop is one review away from being real.

PROPERTY
    A process that IS the daemon for (vault, port) -- or a process the daemon
    started, which inherits its environment -- never spawns another daemon for
    that same (vault, port). It reports the readiness of the one already there.

    The guard is keyed the way the ruling says: the daemon's OWN record (the
    runtime sidecar, which only the executing process writes) plus PID
    liveness. Both halves are load-bearing:
      * without the identity, a legitimate second vault would be refused;
      * without the LIVENESS half, the marker outlives the daemon it describes
        and a `daemon stop` followed by a `daemon start` from the same shell
        would be refused forever. A fence that blocks the remedy is a defect,
        not a stricter fence.

WITNESS
    Delete the guard and ``test_a_process_that_is_already_this_daemon_never_
    spawns_another`` goes red (a Popen happens). Drop the liveness half and
    ``test_the_guard_never_blocks_a_restart_after_the_daemon_is_gone`` goes red.
    Drop the port from the key and ``test_the_guard_is_keyed_on_the_port_too``
    goes red.
"""
from __future__ import annotations

import inspect
import os
import socket
from pathlib import Path

import pytest

import systemu.scheduler.daemon as daemon_mod


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture()
def vault_dir(tmp_path) -> str:
    v = tmp_path / "home" / "vault"
    v.mkdir(parents=True)
    return str(v)


@pytest.fixture()
def spawns(monkeypatch):
    """Record every Popen `start_daemon` attempts, and never start one."""
    calls: list = []

    class _FakeProc:
        pid = 4242

        def poll(self):
            return None

    import subprocess

    def _fake_popen(cmd, *a, **kw):
        calls.append(list(cmd))
        return _FakeProc()

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    # `await_readiness` would otherwise poll a socket nobody is serving.
    monkeypatch.setattr(daemon_mod, "await_readiness",
                        lambda vault_dir, **kw: daemon_mod.DaemonReadiness(
                            ready=False, pid=None, process_alive=False,
                            host="127.0.0.1", port=int(kw.get("port") or 0),
                            reason="stubbed away from the socket"))
    return calls


def _mark_this_process_as_the_daemon(vault_dir: str, port: int) -> None:
    """Exactly what `_run_daemon_loop` does about itself: publish the identity
    and record the pid, both for the SAME socket."""
    os.environ[daemon_mod.DAEMON_IDENTITY_ENV] = daemon_mod.daemon_identity(
        vault_dir, port)
    daemon_mod._write_runtime_state(vault_dir, pid=os.getpid(), port=port,
                                    build=None)


@pytest.fixture(autouse=False)
def clean_identity():
    """Save/restore the marker so a test can never leak it into the next."""
    before = os.environ.get(daemon_mod.DAEMON_IDENTITY_ENV)
    yield
    if before is None:
        os.environ.pop(daemon_mod.DAEMON_IDENTITY_ENV, None)
    else:
        os.environ[daemon_mod.DAEMON_IDENTITY_ENV] = before


# ═════════════════════════════════════════════════════════════════════════════
#  the identity token
# ═════════════════════════════════════════════════════════════════════════════

def test_the_identity_names_a_vault_AND_a_port(tmp_path):
    a = daemon_mod.daemon_identity(str(tmp_path / "a" / "vault"), 8765)
    b = daemon_mod.daemon_identity(str(tmp_path / "a" / "vault"), 8766)
    c = daemon_mod.daemon_identity(str(tmp_path / "b" / "vault"), 8765)
    assert type(a) is str and a
    assert a.isascii(), "a token quoted in a refusal must survive a cp1252 console"
    assert a != b, "two ports on one vault are two different daemons"
    assert a != c, "two vaults on one port are two different daemons"


def test_the_identity_is_never_coarser_than_the_record_it_protects(tmp_path):
    """Two vaults share an identity exactly when they share a daemon record.

    ``_pid_file_path`` and ``_runtime_file_path`` both put the record in the
    vault's PARENT, so two SIBLING vaults have always shared one
    single-instance record -- the pidfile guard already treats them as one
    daemon, which is a pre-existing property of that layout and not something
    this guard introduces. Keying the identity on the same record makes it
    exactly as fine-grained as the bookkeeping it protects: never coarser, and
    never finer in a way that would wave through a rival the pidfile guard
    would have caught. Pinned as a biconditional so the two cannot drift.
    """
    pairs = [
        (str(tmp_path / "v"), str(tmp_path / "w")),                  # siblings
        (str(tmp_path / "a" / "vault"), str(tmp_path / "b" / "vault")),
    ]
    for left, right in pairs:
        share_record = (daemon_mod._runtime_file_path(left)
                        == daemon_mod._runtime_file_path(right))
        share_identity = (daemon_mod.daemon_identity(left, 8765)
                          == daemon_mod.daemon_identity(right, 8765))
        assert share_record is share_identity, (
            f"{left} and {right} share a daemon record: {share_record}, but "
            f"share an identity: {share_identity} -- the guard and the "
            "single-instance record no longer agree on what one daemon is")


def test_the_identity_survives_the_ways_one_path_gets_written(tmp_path):
    """A daemon told `C:/x/v` and one told `C:\\X\\v` are the same daemon."""
    plain = daemon_mod.daemon_identity(str(tmp_path / "v"), 8765)
    noisy = daemon_mod.daemon_identity(str(tmp_path / "v") + os.sep, 8765)
    assert plain == noisy


# ═════════════════════════════════════════════════════════════════════════════
#  the guard
# ═════════════════════════════════════════════════════════════════════════════

def test_a_process_that_is_already_this_daemon_never_spawns_another(
        vault_dir, spawns, clean_identity):
    """THE DEFECT: no pidfile (the parent's write is gone or not yet made), so
    the pidfile guard sees nothing and a rival is spawned for the same socket."""
    port = _free_port()
    _mark_this_process_as_the_daemon(vault_dir, port)
    assert not daemon_mod._pid_file_path(vault_dir).exists(), (
        "this test must exercise the window where the pidfile says nothing")

    verdict = daemon_mod.start_daemon(vault_dir, config=None, vault=None,
                                      port=port, wait_timeout_s=0.1)

    assert spawns == [], (
        "the daemon spawned a SECOND daemon for its own vault and port: "
        f"{spawns}")
    assert verdict is not None, (
        "refusing to spawn is not the same as refusing to answer -- the caller "
        "still needs the readiness verdict of the daemon that IS there")


def test_the_guard_survives_a_child_process_inheriting_the_marker(
        vault_dir, spawns, clean_identity):
    """A tool the daemon starts inherits the environment. It is not the daemon,
    but it addresses the same socket, and a re-entry from there doubles up just
    as thoroughly."""
    port = _free_port()
    _mark_this_process_as_the_daemon(vault_dir, port)
    # The in-process global is what `_run_daemon_loop` sets about itself; a
    # child has only the inherited env. Clear the global to model the child.
    daemon_mod._ACTIVE_DAEMON_IDENTITY = None

    daemon_mod.start_daemon(vault_dir, config=None, vault=None, port=port,
                            wait_timeout_s=0.1)
    assert spawns == [], f"a child of the daemon spawned a rival daemon: {spawns}"


def test_the_guard_is_keyed_on_the_port_too(vault_dir, spawns, clean_identity):
    """A daemon on 8765 must not stop an operator starting one on 8766."""
    served = _free_port()
    other = _free_port()
    _mark_this_process_as_the_daemon(vault_dir, served)

    daemon_mod.start_daemon(vault_dir, config=None, vault=None, port=other,
                            wait_timeout_s=0.1)
    assert len(spawns) == 1, (
        "a daemon serving one port refused a start on a DIFFERENT port -- the "
        f"guard is not keyed on the socket it claims to protect: {spawns}")


def test_the_guard_is_keyed_on_the_vault_too(tmp_path, spawns, clean_identity):
    served = str(tmp_path / "a" / "vault")
    other = str(tmp_path / "b" / "vault")
    Path(served).mkdir(parents=True)
    Path(other).mkdir(parents=True)
    port = _free_port()
    _mark_this_process_as_the_daemon(served, port)

    daemon_mod.start_daemon(other, config=None, vault=None, port=port,
                            wait_timeout_s=0.1)
    assert len(spawns) == 1, (
        f"a daemon for one vault refused a start for another vault: {spawns}")


def test_the_guard_never_blocks_a_restart_after_the_daemon_is_gone(
        vault_dir, spawns, clean_identity):
    """THE FALSE-REFUSAL HALF. `daemon stop` removes the record; the marker in
    the operator's shell environment does not go with it. A guard that reads
    only the marker would refuse every restart from that shell, forever --
    fail-closed onto the remedy is a defect, not a stricter fence."""
    port = _free_port()
    _mark_this_process_as_the_daemon(vault_dir, port)
    # Exactly what `stop_daemon` does.
    daemon_mod._pid_file_path(vault_dir).unlink(missing_ok=True)
    daemon_mod._runtime_file_path(vault_dir).unlink(missing_ok=True)

    daemon_mod.start_daemon(vault_dir, config=None, vault=None, port=port,
                            wait_timeout_s=0.1)
    assert len(spawns) == 1, (
        "the identity marker outlived the daemon and blocked a legitimate "
        f"restart: {spawns}")


def test_a_dead_pid_on_record_does_not_arm_the_guard(vault_dir, spawns,
                                                     clean_identity):
    """Liveness is witnessed, never inferred from the record's existence."""
    port = _free_port()
    os.environ[daemon_mod.DAEMON_IDENTITY_ENV] = daemon_mod.daemon_identity(
        vault_dir, port)
    daemon_mod._write_runtime_state(vault_dir, pid=999999, port=port, build=None)

    daemon_mod.start_daemon(vault_dir, config=None, vault=None, port=port,
                            wait_timeout_s=0.1)
    assert len(spawns) == 1, (
        f"a record naming a DEAD pid armed the guard: {spawns}")


def test_a_forged_marker_alone_cannot_stop_a_start(vault_dir, spawns,
                                                   clean_identity):
    """The environment is inherited by everything; it is not a witness. With no
    live record for this socket the marker decides nothing."""
    port = _free_port()
    os.environ[daemon_mod.DAEMON_IDENTITY_ENV] = daemon_mod.daemon_identity(
        vault_dir, port)

    daemon_mod.start_daemon(vault_dir, config=None, vault=None, port=port,
                            wait_timeout_s=0.1)
    assert len(spawns) == 1, f"an unwitnessed marker refused a start: {spawns}"


def test_a_non_string_marker_is_not_compared(vault_dir, spawns, clean_identity,
                                             monkeypatch):
    """DEC-36: the concrete type is pinned in the frame that compares, because
    `==` dispatches to the operand. A marker that is not a `str` decides
    nothing rather than deciding whatever it wants to."""
    port = _free_port()
    _mark_this_process_as_the_daemon(vault_dir, port)

    class _AlwaysEqual(str):
        def __eq__(self, other):    # pragma: no cover - never reached
            return True

        def __hash__(self):
            return 0

    monkeypatch.setattr(daemon_mod, "_ACTIVE_DAEMON_IDENTITY",
                        _AlwaysEqual("not the identity"))
    os.environ[daemon_mod.DAEMON_IDENTITY_ENV] = "not the identity"

    daemon_mod.start_daemon(vault_dir, config=None, vault=None, port=port,
                            wait_timeout_s=0.1)
    assert len(spawns) == 1, (
        "a subclass that answers True to every comparison armed the guard: "
        f"{spawns}")


# ═════════════════════════════════════════════════════════════════════════════
#  the daemon publishes its own identity
# ═════════════════════════════════════════════════════════════════════════════

def test_the_daemon_loop_publishes_the_identity_it_serves():
    """Only the process that IS the daemon may say so, and it must -- an
    unpublished identity leaves the guard with nothing to compare."""
    src = inspect.getsource(daemon_mod._run_daemon_loop)
    assert "_publish_daemon_identity(" in src, (
        "`_run_daemon_loop` no longer publishes the (vault, port) it serves, "
        "so nothing downstream can tell that it IS the daemon")


def test_the_guard_is_reachable_from_start_daemon():
    """Reachability, not shape: remove the consumption and this goes red."""
    src = inspect.getsource(daemon_mod.start_daemon)
    assert "_already_serving_this_socket(" in src, (
        "`start_daemon` no longer consults the idempotency guard -- the guard "
        "exists but nothing calls it, which is the half-built shape this "
        "project keeps shipping")
