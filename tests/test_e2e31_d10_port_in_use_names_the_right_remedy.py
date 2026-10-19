"""D-10 -- a port-in-use refusal names a remedy that works where it is read.

THE WITNESS (0.10.31 human-style e2e)
-------------------------------------
Run from the REFUSED vault's own directory, `systemu daemon start` printed::

    ERROR Daemon did not become ready.
      port 55186 is already in use by PID 30616; refusing to start a second
      daemon whose recordings would land in another vault
      (`systemu daemon stop --all`, or pick another --port)
      Log: ...\\daemon.log
      Stop the stuck process with: systemu daemon stop

The last line is the generic tail every failed start prints, and from THAT
directory it does nothing: `daemon stop` is pidfile-scoped, the pidfile it
reads belongs to THIS vault, and the process holding the port belongs to
another one. The operator runs it, is told "Daemon is not running", and is now
holding a remedy that has just told them the opposite of the refusal above.

A remedy that cannot work where it is printed is the DEC-34 defect in product
copy: a false assertion about what the operator can do.

THE RULING
    On the port-in-use branch the trailing generic remedy is DROPPED. The
    remedy names the OTHER daemon's vault directory --
    `cd <that vault's operating home> && systemu daemon stop` -- when the
    record says which vault that is, and falls back to `--port` when it does
    not.

WHOSE RECORD. `start_daemon` spawns its child as
``python -m systemu.scheduler.daemon --vault-dir <absolute root> --port N``, so
the root the other daemon serves is written down in the process table by the
parent that started it. `daemon_operating_home` turns that root into the
directory `daemon stop` has to be run from -- the SAME mint the spawn used.

FAIL-CLOSED (DEC-27). An unreadable command line, a process that is not a
systemu daemon, or an argv naming no `--vault-dir` all answer "unknown", and
unknown means the `--port` remedy. Naming the WRONG vault would send the
operator to stop a daemon that is not the one holding the port, which is worse
than naming none.

NO REAL PORT IS EVER TAKEN HERE. The port-availability probe, the listening-pid
lookup and the holder-record read are the three seams faked; nothing binds a
socket, nothing is spawned, and no assumption is made about any port on this
machine.
"""
from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

import systemu.interface.cli_commands as cc
import systemu.scheduler.daemon as daemon_mod


#: The generic tail, by its own words. This is the line the ruling drops.
_GENERIC_TAIL = "Stop the stuck process with"

#: Text only: the port probe below is faked, so nothing is bound.
_TAKEN_PORT = 55186

#: The process the fake lookup reports as holding it.
_HOLDER_PID = 30616


def _vault(tmp_path) -> str:
    root = tmp_path / "this-vault" / "systemu" / "vault"
    root.mkdir(parents=True, exist_ok=True)
    return str(root)


def _other_home(tmp_path) -> str:
    """The operating home of the daemon that holds the port -- a DIFFERENT
    tree from the vault being refused, which is the whole point."""
    home = tmp_path / "other-vault"
    home.mkdir(parents=True, exist_ok=True)
    return str(home)


@pytest.fixture()
def taken_port(monkeypatch):
    """The three seams that would touch the machine, faked at their own names."""
    monkeypatch.setattr(daemon_mod, "_check_port_available",
                        lambda host, port: False)
    monkeypatch.setattr(daemon_mod, "_find_listening_pid",
                        lambda port: _HOLDER_PID)


def _refuse(tmp_path, *, home: str):
    """The REAL `start_daemon`, on its real port-in-use branch."""
    return daemon_mod.start_daemon(
        vault_dir=_vault(tmp_path), config=SimpleNamespace(),
        vault=SimpleNamespace(), port=_TAKEN_PORT)


# --------------------------------------------------------------------------- #
# the MINT -- which remedy the refusal carries
# --------------------------------------------------------------------------- #

def test_the_refusal_names_the_other_vaults_directory_when_the_record_says_it(
        tmp_path, monkeypatch, taken_port):
    home = _other_home(tmp_path)
    monkeypatch.setattr(daemon_mod, "_holder_operating_home", lambda pid: home)

    verdict = _refuse(tmp_path, home=home)

    assert verdict is not None and verdict.ready is False, verdict
    assert verdict.remedy, "the refusal carries no remedy at all"
    assert home in verdict.remedy, verdict.remedy
    assert "systemu daemon stop" in verdict.remedy, verdict.remedy
    verdict.remedy.encode("ascii")          # DEC-32c


def test_an_unknown_holder_gets_the_port_remedy_not_a_guessed_directory(
        tmp_path, monkeypatch, taken_port):
    """Fail-closed. Naming the wrong vault sends the operator to stop a daemon
    that is not the one holding the port."""
    monkeypatch.setattr(daemon_mod, "_holder_operating_home", lambda pid: "")

    verdict = _refuse(tmp_path, home="")

    assert verdict.remedy, "an unknown holder must still say what to do"
    assert "--port" in verdict.remedy, verdict.remedy
    assert "cd " not in verdict.remedy, (
        "the refusal invented a directory it never read:\n" + verdict.remedy)
    verdict.remedy.encode("ascii")


def test_the_holder_record_reader_is_fail_closed():
    """Every way of NOT knowing answers the same way: "".

    A pid that is not an int, a process psutil cannot be asked about, and a
    process whose command line does not name this daemon module are the three
    shapes seen in the field; none of them may produce a directory.
    """
    assert daemon_mod._holder_operating_home(None) == ""
    assert daemon_mod._holder_operating_home("30616") == ""
    # A pid that certainly is not a systemu daemon: this very test process.
    import os
    assert daemon_mod._holder_operating_home(os.getpid()) == ""


# --------------------------------------------------------------------------- #
# THE SEAM -- what the operator actually reads
# --------------------------------------------------------------------------- #

def _run_start(monkeypatch, tmp_path, *, verdict):
    """`daemon start` with the readiness mint faked to the given verdict."""
    monkeypatch.setattr("systemu.runtime.optional_deps.missing_groups",
                        lambda groups: ())
    monkeypatch.setattr("sharing_on.setup_flow.key_present", lambda: True)
    monkeypatch.setattr(daemon_mod, "start_daemon", lambda **kw: verdict)

    res = CliRunner().invoke(
        cc.daemon_start, ["--port", str(_TAKEN_PORT)],
        obj={"config": SimpleNamespace(vault_dir=_vault(tmp_path)),
             "vault": SimpleNamespace()},
    )
    return res


def test_the_refusal_screen_drops_the_generic_remedy(tmp_path, monkeypatch,
                                                     taken_port):
    """THE REPRO. `systemu daemon stop` from this directory stops nothing, and
    telling the operator to run it contradicts the refusal above it."""
    home = _other_home(tmp_path)
    monkeypatch.setattr(daemon_mod, "_holder_operating_home", lambda pid: home)
    verdict = _refuse(tmp_path, home=home)

    res = _run_start(monkeypatch, tmp_path, verdict=verdict)

    assert res.exit_code == 1, res.output      # DEC-41: not started != success
    assert _GENERIC_TAIL not in res.output, (
        "the port-in-use refusal still prints the pidfile-scoped remedy, which "
        "does nothing from this directory:\n" + res.output)


def test_the_refusal_screen_names_the_other_vaults_directory(tmp_path,
                                                             monkeypatch,
                                                             taken_port):
    home = _other_home(tmp_path)
    monkeypatch.setattr(daemon_mod, "_holder_operating_home", lambda pid: home)
    verdict = _refuse(tmp_path, home=home)

    res = _run_start(monkeypatch, tmp_path, verdict=verdict)

    carriers = [ln for ln in res.output.splitlines() if home in ln]
    assert carriers, (
        "the refusal never names the vault whose daemon holds the port:\n"
        + res.output)
    assert len(carriers) == 1, res.output
    assert "systemu daemon stop" in carriers[0], carriers[0]
    res.output.encode("ascii")


def test_the_refusal_screen_is_one_line_per_path(tmp_path, monkeypatch,
                                                 taken_port):
    """The remedy carries a PATH, so it goes out on the unwrapping writer: a
    path folded at column 80 can be neither pasted nor `cd`-ed into."""
    home = _other_home(tmp_path)
    monkeypatch.setattr(daemon_mod, "_holder_operating_home", lambda pid: home)
    verdict = _refuse(tmp_path, home=home)
    monkeypatch.setenv("COLUMNS", "80")

    res = _run_start(monkeypatch, tmp_path, verdict=verdict)

    assert home in res.output, res.output


# --------------------------------------------------------------------------- #
# and the change is SCOPED: every other failure keeps the generic remedy
# --------------------------------------------------------------------------- #

def test_a_timed_out_start_still_names_the_generic_remedy(tmp_path, monkeypatch):
    """The branch the tail was written for: a daemon of OURS that is stuck.
    Dropping it everywhere would trade one wrong remedy for a missing one."""
    monkeypatch.setattr(daemon_mod, "_connection_succeeds",
                        lambda host, port, timeout: False)
    verdict = daemon_mod.await_readiness(
        _vault(tmp_path), port=_TAKEN_PORT, timeout_s=0.0,
        poll_s=0.01, probe_timeout=0.01)

    assert not verdict.remedy, (
        "a plain timeout is not a port conflict and must carry no special "
        "remedy: " + repr(verdict.remedy))

    res = _run_start(monkeypatch, tmp_path, verdict=verdict)

    assert res.exit_code == 1, res.output
    assert _GENERIC_TAIL in res.output, res.output


# --------------------------------------------------------------------------- #
# reachability
# --------------------------------------------------------------------------- #

def test_daemon_start_hands_the_minted_remedy_to_the_failure_printer():
    """Reachability pin (the standing rule): delete the production call site
    and a NAMED test goes red."""
    source = inspect.getsource(cc.daemon_start.callback)

    assert "_print_start_failure(" in source, source
    assert "remedy" in source, (
        "`daemon start` no longer feeds the minted remedy to the failure "
        "printer, so every failure prints the pidfile-scoped tail:\n" + source)


def test_the_refusal_mints_its_remedy_rather_than_the_surface_deriving_one():
    """DEC-43. The surface prints what the mint says; it does not work out for
    itself which remedy a refusal deserves."""
    source = inspect.getsource(daemon_mod.start_daemon)

    assert "_holder_operating_home(" in source, (
        "`start_daemon` no longer reads which vault the port's holder serves:\n"
        + source)
    assert "port_conflict_remedy(" in source, source
