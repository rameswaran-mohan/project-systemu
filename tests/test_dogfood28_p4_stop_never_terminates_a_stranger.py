"""P4 -- `daemon stop` never terminates a PID that is not a systemu daemon.

THE DEFECT
----------
The default (pidfile) path read an integer out of `.systemu_daemon.pid` and
called `TerminateProcess` / `SIGTERM` on it. Nothing checked what that pid WAS.

A pidfile outlives the daemon it names in every direction: a crash leaves it
behind, a kill -9 leaves it behind, a copied project folder brings someone
else's along. Operating systems reissue pids. So `systemu daemon stop` in a
stale tree could terminate an unrelated process of the operator's -- an editor,
a database, another user's job -- and report "OK Daemon stopped."

The `--all` sweep in the CLI has always been careful here: it only kills
processes whose command line names `systemu.scheduler.daemon`. The path an
operator actually runs was the unguarded one.

THE RULING
----------
    The default path verifies the pid's command line names the daemon module
    AND, when it can be read, that the process did not start AFTER the pidfile
    that names it was written. Anything else is REFUSED -- named, non-zero, and
    with nothing signalled.

ON THE TIME COMPARISON, because the direction is counter-intuitive and getting
it backwards would refuse every healthy stop. `_run_daemon_loop` rewrites the
pidfile with its OWN pid seconds after the child starts, so for a genuine
daemon the process is always OLDER than the pidfile. The failure this catches
is the reverse: a pid the OS reissued to a process that started LATER than the
record naming it, which therefore cannot be the process that record is about.

FAIL-CLOSED (DEC-27: completeness is witnessed, never inferred). A command line
that could not be read is not a command line that matched. An unverifiable
identity refuses; it does not terminate on the benefit of the doubt.

NOTHING IS SIGNALLED IN THIS FILE. `psutil.Process` is faked at its own module
seam and the termination call is replaced with a recorder, so no test here can
reach a real process -- which is the property under test, and would be a poor
thing to establish by trying it.
"""
from __future__ import annotations

import os
import time

import pytest

psutil = pytest.importorskip("psutil")

import systemu.interface.cli_commands as cc  # noqa: E402
import systemu.scheduler.daemon as daemon_mod  # noqa: E402

_MARKER = "systemu.scheduler.daemon"


class _Gone(Exception):
    """Stands in for psutil.NoSuchProcess."""


class _Denied(Exception):
    """Stands in for psutil.AccessDenied -- a real answer on Windows."""


class _FakeProc:
    def __init__(self, pid, cmdline, *, alive=True, created=None,
                 cmdline_error=None):
        self.pid = pid
        self._cmdline = list(cmdline)
        self._alive = alive
        self._created = created
        self._cmdline_error = cmdline_error

    def is_running(self):
        return self._alive

    def cmdline(self):
        if self._cmdline_error is not None:
            raise self._cmdline_error
        return list(self._cmdline)

    def create_time(self):
        if self._created is None:
            raise _Denied("no create_time")
        return self._created


@pytest.fixture()
def stop_bench(monkeypatch, tmp_path):
    """A vault with a pidfile, a faked process table, and a kill recorder.

    Returns a callable: describe the process the pidfile names, get back a
    helper that runs `stop_daemon` and can say whether anything was signalled.
    """
    vault = tmp_path / "home" / "vault"
    vault.mkdir(parents=True)
    monkeypatch.setenv("SYSTEMU_VAULT_DIR", str(vault))

    signalled = []
    monkeypatch.setattr(daemon_mod, "_terminate_pid",
                        lambda pid: signalled.append(pid))

    class _Bench:
        def __init__(self):
            self.vault_dir = str(vault)
            self.pid_file = daemon_mod._pid_file_path(str(vault))
            self.signalled = signalled

        def install(self, *, pid_text, proc):
            self.pid_file.write_text(str(pid_text))
            table = {} if proc is None else {proc.pid: proc}

            def _process(pid):
                if pid not in table:
                    raise _Gone(f"no process {pid}")
                return table[pid]

            monkeypatch.setattr(psutil, "Process", _process)
            return self

        def stop(self):
            return daemon_mod.stop_daemon(self.vault_dir)

    return _Bench()


def _daemon_argv(vault_dir: str, port: int = 8765) -> list:
    return [r"C:\py\python.exe", "-m", _MARKER, "--vault-dir", vault_dir,
            "--port", str(port)]


def _older_than_the_pidfile(pid_file) -> float:
    """A creation time a GENUINE daemon would have: the pidfile is rewritten by
    the child seconds after it starts, so the process is the older of the two."""
    return pid_file.stat().st_mtime - 30.0


# --------------------------------------------------------------------------- #
# the daemon itself is still stopped
# --------------------------------------------------------------------------- #

def test_a_matching_daemon_pid_is_terminated(stop_bench):
    """The daemon is still stopped -- the guard must cost the operator nothing
    on the path they take every day."""
    bench = stop_bench.install(
        pid_text=4321,
        proc=_FakeProc(4321, _daemon_argv(stop_bench.vault_dir)))

    assert bench.stop() is True
    assert bench.signalled == [4321], bench.signalled
    assert not bench.pid_file.exists()


def test_a_daemon_older_than_its_own_pidfile_is_still_stopped(stop_bench):
    """The direction pin. `_run_daemon_loop` rewrites the pidfile with its own
    pid well after the process started, so create_time < mtime is the HEALTHY
    ordering -- reading the comparison the other way round would refuse every
    stop on every machine."""
    bench = stop_bench.install(pid_text=4321, proc=None)
    created = _older_than_the_pidfile(bench.pid_file)
    bench.install(pid_text=4321,
                  proc=_FakeProc(4321, _daemon_argv(bench.vault_dir),
                                 created=created))

    assert bench.stop() is True
    assert bench.signalled == [4321]


def test_a_daemon_whose_start_time_cannot_be_read_is_still_stopped(stop_bench):
    """"When available" is part of the ruling: `create_time` is AccessDenied for
    plenty of processes, and the command-line check is the load-bearing half."""
    bench = stop_bench.install(
        pid_text=4321,
        proc=_FakeProc(4321, _daemon_argv(stop_bench.vault_dir), created=None))

    assert bench.stop() is True
    assert bench.signalled == [4321]


# --------------------------------------------------------------------------- #
# a stranger is REFUSED, and nothing is signalled
# --------------------------------------------------------------------------- #

def test_a_pid_whose_cmdline_is_not_the_daemon_is_refused(stop_bench):
    """THE DEFECT. A stale pidfile plus a reissued pid used to be a kill."""
    bench = stop_bench.install(
        pid_text=4321,
        proc=_FakeProc(4321, [r"C:\Program Files\db\postgres.exe", "-D", "data"],
                       created=time.time()))

    with pytest.raises(daemon_mod.DaemonStopRefused) as excinfo:
        bench.stop()

    assert bench.signalled == [], (
        "an unrelated process was signalled: " + repr(bench.signalled))
    message = excinfo.value.message
    assert "4321" in message, message
    assert "not a systemu daemon" in message, message
    assert "not terminating" in message, message
    assert "postgres.exe" in message, (
        "the refusal does not say what it saw, so the operator cannot judge "
        "it: " + message)


def test_the_refusal_leaves_the_pidfile_alone(stop_bench):
    """Deleting it would destroy the only evidence, and the remedy the message
    names is the operator's to run once they have looked."""
    bench = stop_bench.install(
        pid_text=4321,
        proc=_FakeProc(4321, ["notepad.exe"], created=time.time()))

    with pytest.raises(daemon_mod.DaemonStopRefused):
        bench.stop()

    assert bench.pid_file.exists()
    assert bench.pid_file.read_text().strip() == "4321"


def test_a_process_that_started_after_the_pidfile_is_refused(stop_bench):
    """A reissued pid running the daemon module -- another vault's daemon, or a
    pid the OS handed on -- cannot be the process this record was written
    about, whatever its command line says."""
    bench = stop_bench.install(pid_text=4321, proc=None)
    later = bench.pid_file.stat().st_mtime + 600.0
    bench.install(pid_text=4321,
                  proc=_FakeProc(4321, _daemon_argv(bench.vault_dir),
                                 created=later))

    with pytest.raises(daemon_mod.DaemonStopRefused) as excinfo:
        bench.stop()

    assert bench.signalled == []
    assert "4321" in excinfo.value.message


def test_a_cmdline_that_cannot_be_read_is_refused(stop_bench):
    """DEC-27: an identity we could not witness is not an identity we
    established. AccessDenied is a routine answer on Windows."""
    bench = stop_bench.install(
        pid_text=4321,
        proc=_FakeProc(4321, _daemon_argv(stop_bench.vault_dir),
                       cmdline_error=_Denied("access is denied"),
                       created=time.time()))

    with pytest.raises(daemon_mod.DaemonStopRefused):
        bench.stop()

    assert bench.signalled == []


def test_a_pidfile_that_does_not_spell_a_pid_is_refused(stop_bench):
    """It used to raise ValueError out of `stop_daemon` and take the CLI with
    it. A file that names no pid names no daemon."""
    bench = stop_bench.install(pid_text="not-a-pid", proc=None)

    with pytest.raises(daemon_mod.DaemonStopRefused):
        bench.stop()

    assert bench.signalled == []


# --------------------------------------------------------------------------- #
# a pid that is simply gone is "not running", not a refusal
# --------------------------------------------------------------------------- #

def test_a_pid_that_no_longer_exists_is_reported_as_not_running(stop_bench):
    """There is nothing to terminate and nothing to warn about: clean up and
    say so. A refusal here would train the operator to ignore refusals."""
    bench = stop_bench.install(pid_text=4321, proc=None)

    assert bench.stop() is False
    assert bench.signalled == []
    assert not bench.pid_file.exists()


# --------------------------------------------------------------------------- #
# the operator surface
# --------------------------------------------------------------------------- #

def test_the_cli_reports_the_refusal_and_exits_non_zero(stop_bench,
                                                        monkeypatch):
    """DEC-41's shape: not stopped != success. A zero exit here tells a script
    the daemon is down when an unrelated process is still up."""
    from types import SimpleNamespace

    from click.testing import CliRunner

    bench = stop_bench.install(
        pid_text=4321,
        proc=_FakeProc(4321, [r"C:\Program Files\db\postgres.exe"],
                       created=time.time()))

    res = CliRunner().invoke(
        cc.daemon_stop, [],
        obj={"config": SimpleNamespace(vault_dir=bench.vault_dir),
             "vault": SimpleNamespace()},
        env={"COLUMNS": "80"},
    )

    assert res.exit_code != 0, res.output
    assert "4321" in res.output, res.output
    assert "not a systemu daemon" in res.output, res.output
    assert "OK Daemon stopped." not in res.output, res.output
    res.output.encode("ascii")          # DEC-32c
    assert bench.signalled == []


def test_the_refusal_names_the_pidfile_on_one_line(stop_bench, monkeypatch):
    """The remedy is "delete the pidfile", so the operator has to be able to
    paste its path (N7: a path wrapped at column 80 is not a path)."""
    from types import SimpleNamespace

    from click.testing import CliRunner

    bench = stop_bench.install(
        pid_text=4321,
        proc=_FakeProc(4321, ["notepad.exe"], created=time.time()))

    res = CliRunner().invoke(
        cc.daemon_stop, [],
        obj={"config": SimpleNamespace(vault_dir=bench.vault_dir),
             "vault": SimpleNamespace()},
        env={"COLUMNS": "80"},
    )

    path = str(bench.pid_file)
    assert path in res.output, (
        "the pidfile path is folded across lines, so the remedy cannot be "
        "carried out:\n" + res.output)


def test_the_default_path_and_the_all_sweep_key_on_the_same_marker():
    """The `--all` sweep was already careful; this closes the gap by consuming
    the SAME marker rather than spelling a second one (DEC-43)."""
    import inspect

    assert daemon_mod._DAEMON_CMDLINE_MARKER == _MARKER
    source = inspect.getsource(cc.daemon_stop.callback)
    assert "_DAEMON_CMDLINE_MARKER" in source, (
        "the --all sweep spells its own copy of the marker, which can drift "
        "from the one the default path checks")
    assert '"systemu.scheduler.daemon"' not in source, source


def test_nothing_here_reached_a_real_process():
    """Fixture premise, asserted rather than assumed: this file must not be
    able to terminate anything, and the seam it relies on has to exist."""
    assert callable(getattr(daemon_mod, "_terminate_pid", None))
    assert os.getpid() > 0
