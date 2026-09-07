"""D2 (dogfood 0.10.28): the multi-instance DANGER banner counted DEAD records.

THE WITNESS
-----------
Dogfooding the shipped 0.10.28 wheel on Windows 11, the dashboard banner said
"5 systemu daemon processes are running" while exactly TWO were alive; a little
later it said "4" with ONE alive. The remedy it prescribed -- ``systemu daemon
stop --all`` -- would have stopped the healthy daemon.

The count came off a ``psutil.process_iter`` SNAPSHOT. A snapshot is a RECORD,
not an observation of a live process: by the time the row is read the process
may have exited, its pid may have been handed to something unrelated, or its
command line may no longer be the daemon's. Every one of those rows was counted,
so the banner cried wolf -- and trained the operator to ignore it right before
the one moment it was right.

THE PROPERTY
------------
    The count is of PID-VERIFIED-LIVE daemon processes, de-duplicated by pid:
    the marker is in the command line the OS reports NOW, ``is_running()`` is
    true NOW, and each pid is counted once. A record that fails any of those is
    STALE and is pruned from the only source it came from -- the in-process
    scan cache -- so it can never be counted again either.

    A single verified-live daemon plus any number of stale records is NOT a
    conflict and raises NO issue at all. The DANGER wording is reachable only
    when two or more VERIFIED-LIVE daemons contend for one port (or for a port
    we could not establish -- fail closed, DEC-27).

    And the banner NAMES what it counted: pid, port and interpreter path, so
    the operator can check the claim instead of taking it on faith.

Every shape below runs without a single daemon: ``psutil.process_iter`` is
faked at its own module seam, exactly as the production scan calls it.
"""
from __future__ import annotations

import pytest

psutil = pytest.importorskip("psutil")

from systemu.interface.components import health_banner as hb  # noqa: E402

_MARKER = "systemu.scheduler.daemon"


def _argv(vault: str, port, exe: str = r"C:\py\python.exe") -> list:
    argv = [exe, "-m", _MARKER, "--vault-dir", vault]
    if port is not None:
        argv += ["--port", str(port)]
    return argv


class _Gone(Exception):
    """Stands in for psutil.NoSuchProcess -- the scan must treat it as gone."""


class _FakeProc:
    """One row of a ``process_iter`` snapshot, plus what the OS says NOW.

    ``info`` is the SNAPSHOT (what the old count believed). ``is_running`` /
    ``cmdline`` / ``exe`` / ``cwd`` are the LIVE re-ask. Splitting the two is
    the whole point: a stale record is one where they disagree.
    """

    def __init__(self, pid, snapshot_cmdline, *, alive=True,
                 live_cmdline=None, cwd=None, exe=None):
        self.info = {"pid": pid, "cmdline": list(snapshot_cmdline)}
        self._alive = alive
        self._live = (list(live_cmdline) if live_cmdline is not None
                      else list(snapshot_cmdline))
        self._cwd = cwd
        self._exe = exe

    def is_running(self):
        return self._alive

    def cmdline(self):
        if not self._alive:
            raise _Gone("the process exited between the snapshot and this read")
        return list(self._live)

    def cwd(self):
        if self._cwd is None:
            raise _Gone("no cwd")
        return self._cwd

    def exe(self):
        if self._exe is None:
            raise _Gone("no exe")
        return self._exe


@pytest.fixture()
def scan(monkeypatch):
    """Fake ``psutil.process_iter`` at the module seam and reset the TTL caches.

    Returns a callable: give it the rows the OS would yield, get back the
    records the banner would count.
    """
    from systemu.runtime import provider_status as ps

    def _quiet(_url, _timeout=1.0):
        return (ps.STATE_UNREACHABLE, "nothing answered (test)")

    monkeypatch.setitem(ps._PROBES, "ollama", _quiet)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    ps.clear_probe_cache()

    def _install(rows, *, self_pid=999999):
        monkeypatch.setattr(psutil, "process_iter", lambda *a, **k: list(rows))
        monkeypatch.setattr(hb.os, "getpid", lambda: self_pid)
        hb._daemon_procs_cache["ts"] = -1e9
        hb._daemon_probe_cache["ts"] = -1e9
        return hb._scan_daemon_processes()

    yield _install
    ps.clear_probe_cache()


# -- (a) one live daemon among stale records ---------------------------------

def _one_live_and_three_stale():
    return [
        # THE ONE LIVE DAEMON.
        _FakeProc(100, _argv(r"C:\work\systemu\vault", 8765),
                  cwd=r"C:\work", exe=r"C:\py\python.exe"),
        # STALE 1: it was in the snapshot and has since exited.
        _FakeProc(201, _argv(r"C:\old\systemu\vault", 8901), alive=False),
        # STALE 2: the pid is live, but the OS handed it to something else.
        _FakeProc(202, _argv(r"C:\old\systemu\vault", 8902),
                  live_cmdline=[r"C:\Windows\notepad.exe", "notes.txt"]),
        # STALE 3: still python, no longer the daemon module.
        _FakeProc(203, _argv(r"C:\old\systemu\vault", 8903),
                  live_cmdline=[r"C:\py\python.exe", "-m", "pip", "list"]),
    ]


def test_one_live_daemon_among_three_stale_records_counts_one(scan):
    """THE REPRO. Four snapshot rows, one live daemon -> the count is 1."""
    records = scan(_one_live_and_three_stale())
    assert [d.pid for d in records] == [100], records
    assert hb._count_systemu_daemons() == 1


def test_one_live_daemon_among_stale_records_raises_no_issue(scan):
    """The operator-facing half: no DANGER, no warning, nothing at all."""
    scan(_one_live_and_three_stale())
    state = hb.build_health_state(vault_dir=None)
    assert [i for i in state.issues if "systemu daemon processes" in i.message] == []


def test_the_stale_records_are_pruned_from_the_scan_cache(scan):
    """Pruned from the source they came from: the in-process scan cache holds
    only verified-live records, and names what it dropped."""
    scan(_one_live_and_three_stale())
    assert [d.pid for d in hb._daemon_processes()] == [100]
    assert set(hb.pruned_daemon_pids()) == {201, 202, 203}


# -- (b) two live daemons on one port ----------------------------------------

def test_two_live_daemons_on_one_port_still_raise_danger(scan):
    scan([
        _FakeProc(100, _argv(r"C:\work\systemu\vault", 8765),
                  cwd=r"C:\work", exe=r"C:\py\python.exe"),
        _FakeProc(200, _argv(r"C:\iso\systemu\vault", 8765),
                  cwd=r"C:\iso", exe=r"C:\other\python.exe"),
        _FakeProc(301, _argv(r"C:\dead\systemu\vault", 8765), alive=False),
    ])
    state = hb.build_health_state(vault_dir=None)
    hits = [i for i in state.issues if "systemu daemon processes" in i.message]
    assert len(hits) == 1
    assert hits[0].severity == "danger"
    assert hits[0].message.startswith("2 systemu daemon processes are running.")


def test_the_danger_banner_names_every_live_pid_port_and_interpreter(scan):
    """The operator could not check the old claim -- it named nothing."""
    scan([
        _FakeProc(100, _argv(r"C:\work\systemu\vault", 8765),
                  cwd=r"C:\work", exe=r"C:\py\python.exe"),
        _FakeProc(200, _argv(r"C:\iso\systemu\vault", 8765),
                  cwd=r"C:\iso", exe=r"C:\other\python.exe"),
    ])
    state = hb.build_health_state(vault_dir=None)
    msg = [i for i in state.issues
           if "systemu daemon processes" in i.message][0].message
    assert "PID 100" in msg and "PID 200" in msg, msg
    assert "8765" in msg, msg
    assert r"C:\py\python.exe" in msg and r"C:\other\python.exe" in msg, msg


def test_the_named_listing_is_ascii(scan):
    """DEC-32c: verdict-carrying operator copy is ASCII-only."""
    scan([
        _FakeProc(100, _argv(r"C:\work\systemu\vault", 8765),
                  cwd=r"C:\work", exe=r"C:\py\python.exe"),
        _FakeProc(200, _argv(r"C:\iso\systemu\vault", 8901),
                  cwd=r"C:\iso", exe=r"C:\other\python.exe"),
    ])
    state = hb.build_health_state(vault_dir=None)
    for issue in state.issues:
        (issue.message + (issue.cta or "")).encode("ascii")


# -- (c) pid reuse -----------------------------------------------------------

def test_a_dead_pid_reused_by_an_unrelated_process_is_not_counted(scan):
    """The pid is live. The process is not ours. It is not a daemon."""
    records = scan([
        _FakeProc(100, _argv(r"C:\work\systemu\vault", 8765),
                  cwd=r"C:\work", exe=r"C:\py\python.exe"),
        _FakeProc(777, _argv(r"C:\gone\systemu\vault", 8765),
                  live_cmdline=[r"C:\Windows\System32\svchost.exe", "-k",
                                "netsvcs"]),
    ])
    assert [d.pid for d in records] == [100], records


def test_the_same_pid_twice_is_counted_once(scan):
    records = scan([
        _FakeProc(100, _argv(r"C:\work\systemu\vault", 8765),
                  cwd=r"C:\work", exe=r"C:\py\python.exe"),
        _FakeProc(100, _argv(r"C:\work\systemu\vault", 8765),
                  cwd=r"C:\work", exe=r"C:\py\python.exe"),
    ])
    assert [d.pid for d in records] == [100], records


def test_a_record_with_no_usable_pid_is_not_counted(scan):
    """Unverifiable is not live (DEC-27: completeness is witnessed)."""
    records = scan([
        _FakeProc(None, _argv(r"C:\work\systemu\vault", 8765)),
        _FakeProc("100", _argv(r"C:\work\systemu\vault", 8765)),
    ])
    assert records == (), records


# -- the live-verification helper is its own seam ----------------------------

def test_verify_live_daemon_returns_the_live_command_line():
    proc = _FakeProc(100, _argv("/v", 8765))
    assert hb.verify_live_daemon(proc) == _argv("/v", 8765)


def test_verify_live_daemon_refuses_a_dead_process():
    assert hb.verify_live_daemon(_FakeProc(100, _argv("/v", 8765),
                                           alive=False)) is None


def test_verify_live_daemon_refuses_a_process_that_is_no_longer_the_daemon():
    assert hb.verify_live_daemon(
        _FakeProc(100, _argv("/v", 8765),
                  live_cmdline=["notepad.exe"])) is None


def test_verify_live_daemon_never_raises_on_a_hostile_process_object():
    class _Hostile:
        def is_running(self):
            raise RuntimeError("access denied")

    assert hb.verify_live_daemon(_Hostile()) is None
    assert hb.verify_live_daemon(None) is None


def test_the_port_is_read_from_the_live_command_line_not_the_snapshot(scan):
    """A snapshot argv can belong to a process that no longer exists; the port
    the banner reports must come from the line the OS reports NOW."""
    records = scan([
        _FakeProc(100, _argv(r"C:\work\systemu\vault", 8765),
                  live_cmdline=_argv(r"C:\work\systemu\vault", 8901),
                  cwd=r"C:\work", exe=r"C:\py\python.exe"),
    ])
    assert [d.port for d in records] == [8901], records


def test_a_process_iter_that_raises_yields_no_records(monkeypatch):
    monkeypatch.setattr(psutil, "process_iter",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("psutil denied")))
    hb._daemon_procs_cache["ts"] = -1e9
    assert hb._scan_daemon_processes() == ()
