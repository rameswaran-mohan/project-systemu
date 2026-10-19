"""NIT -- a second `daemon start` reports the daemon that is already there.

THE WITNESS (0.10.31 human-style e2e)
-------------------------------------
`systemu daemon start` run twice printed::

    2026-09-07 ... [systemu.scheduler.daemon] WARNING: [Daemon] Already running
    (PID 30616). Stop it first.
    OK Daemon ready.

Two lines, and both of them are wrong for what happened. The first is a raw
log record leaking through a CLI verdict surface -- a timestamp, a module path
and a level, none of which is an answer -- and it says "Stop it first", which
is an INSTRUCTION TO DO SOMETHING DESTRUCTIVE that nothing here needs. The
second says "Daemon ready", which is true of a daemon this invocation did not
start, so the operator cannot tell an idempotent no-op from a successful start.
An operator who reads the pair in order does exactly the wrong thing: they stop
the daemon that was serving them.

THE RULING
    The idempotent case prints one plain line --

        OK Daemon already serving this vault (PID N) on http://127.0.0.1:P

    -- ASCII, no log-format prefix, no "Stop it first", exit 0. The log line
    becomes INFO: nothing is wrong, so nothing warns.

WHY IT IS A FACT ON THE VERDICT AND NOT A GUESS AT THE SURFACE. "Was this
daemon already there?" is known only inside `start_daemon`, at the two guards
that decline to spawn. The CLI cannot re-derive it -- a ready verdict looks
identical either way -- so the fact rides out on the minted verdict
(`already_serving`) exactly as the build, the interpreter and the port's
provenance already do (DEC-43).

WHAT IS KEPT. The F13/D1 disclosure lines. `daemon start` on a daemon that is
already up is precisely the mid-upgrade screen those exist for: new CLI, old
daemon still holding the port. They print only when there is something to say
and are indented under the verdict line, so the verdict itself is still one
line.

NOTHING IS SPAWNED AND NO PORT IS BOUND. The identity guard, the pidfile
process probe and the TCP connection witness are the seams faked; the verdict
is otherwise minted by the real `await_readiness`.
"""
from __future__ import annotations

import inspect
import logging
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

import systemu.interface.cli_commands as cc
import systemu.scheduler.daemon as daemon_mod


#: The PID the witness carried.
_LIVE_PID = 30616

#: Text only -- the connection witness below is faked, so nothing is contacted
#: and nothing is assumed about what may be listening on this machine.
_SERVED_PORT = 8912


def _vault(tmp_path) -> str:
    root = tmp_path / "operating-home" / "systemu" / "vault"
    root.mkdir(parents=True, exist_ok=True)
    return str(root)


@pytest.fixture()
def gates_open(monkeypatch):
    """The two refusals `daemon start` makes BEFORE it looks at any daemon.

    Patched at their own modules: the dashboard extra and a usable provider are
    real gates and this test is about neither.
    """
    monkeypatch.setattr("systemu.runtime.optional_deps.missing_groups",
                        lambda groups: ())
    monkeypatch.setattr("sharing_on.setup_flow.key_present", lambda: True)


@pytest.fixture()
def a_daemon_is_up(monkeypatch):
    """A live daemon of ours, holding the port, without one existing."""
    monkeypatch.setattr(daemon_mod, "_pidfile_process",
                        lambda vault_dir: (_LIVE_PID, True))
    monkeypatch.setattr(daemon_mod, "_connection_succeeds",
                        lambda host, port, timeout: True)


def _start_again(tmp_path, *, guard: bool, monkeypatch):
    """`daemon start` against a daemon that is already serving this vault.

    ``guard`` selects WHICH of the two decline-to-spawn guards answers: the
    identity guard (`_already_serving_this_socket`), or the pidfile guard --
    the one the witness went through.
    """
    vault_dir = _vault(tmp_path)
    monkeypatch.setattr(daemon_mod, "_already_serving_this_socket",
                        lambda vd, port: guard)
    if not guard:
        daemon_mod._pid_file_path(vault_dir).write_text(str(_LIVE_PID),
                                                        encoding="utf-8")

    return CliRunner().invoke(
        cc.daemon_start, ["--port", str(_SERVED_PORT), "--wait", "1"],
        obj={"config": SimpleNamespace(vault_dir=vault_dir),
             "vault": SimpleNamespace()},
    )


#: The two guards, by the name a failure message must be able to say.
_GUARDS = {"identity-guard": True, "pidfile-guard": False}


def _expected_line() -> str:
    """The ruled line, spelled out here rather than imported from the product:
    a test that built it the way the code does would pass on any wording."""
    return "OK Daemon already serving this vault (PID {}) on http://127.0.0.1:{}".format(
        _LIVE_PID, _SERVED_PORT)


# --------------------------------------------------------------------------- #
# THE REPRO
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("guard", sorted(_GUARDS))
def test_the_idempotent_start_says_the_daemon_was_already_serving(
        guard, tmp_path, monkeypatch, gates_open, a_daemon_is_up):
    res = _start_again(tmp_path, guard=_GUARDS[guard], monkeypatch=monkeypatch)

    assert res.exit_code == 0, res.output
    assert _expected_line() in res.output, (
        "the idempotent start does not report the daemon that was already "
        "there:\n" + res.output)


def _everything_the_operator_sees(res, caplog) -> str:
    """The CLI's own output PLUS the log records the run emitted.

    The witnessed WARNING did not travel through click -- it was a logging
    record rendered by the handler the daemon's logger writes to, which is why
    it arrived with a timestamp and a module path in front of it. A test that
    read only `res.output` would therefore pass on the defect it exists to
    catch, so both halves of what reaches the console are checked together.
    """
    emitted = "\n".join(r.getMessage() for r in caplog.records)
    return res.output + "\n" + emitted


@pytest.mark.parametrize("guard", sorted(_GUARDS))
def test_the_idempotent_start_never_says_stop_it_first(
        guard, tmp_path, monkeypatch, caplog, gates_open, a_daemon_is_up):
    """The half of the witness that is actively harmful: an operator who
    follows it stops the daemon that was serving them."""
    caplog.set_level(logging.DEBUG, logger="systemu.scheduler.daemon")
    res = _start_again(tmp_path, guard=_GUARDS[guard], monkeypatch=monkeypatch)

    assert "Stop it first" not in _everything_the_operator_sees(res, caplog), (
        res.output + "\n" + caplog.text)


@pytest.mark.parametrize("guard", sorted(_GUARDS))
def test_no_warning_is_raised_by_a_daemon_doing_its_job(
        guard, tmp_path, monkeypatch, caplog, gates_open, a_daemon_is_up):
    """A timestamp, a module path and a level are not an answer -- and nothing
    here is wrong, so nothing may warn."""
    caplog.set_level(logging.DEBUG, logger="systemu.scheduler.daemon")
    res = _start_again(tmp_path, guard=_GUARDS[guard], monkeypatch=monkeypatch)

    assert "WARNING" not in res.output, (
        "a raw log record is on the verdict surface:\n" + res.output)
    loud = [r for r in caplog.records
            if r.name == "systemu.scheduler.daemon"
            and r.levelno >= logging.WARNING]
    assert not loud, (
        "the idempotent path still warns: "
        + repr([(r.levelname, r.getMessage()) for r in loud]))


@pytest.mark.parametrize("guard", sorted(_GUARDS))
def test_the_idempotent_start_does_not_claim_it_started_anything(
        guard, tmp_path, monkeypatch, gates_open, a_daemon_is_up):
    """"OK Daemon ready." is what a start that DID something says. Printing it
    here leaves an operator unable to tell a no-op from a start."""
    res = _start_again(tmp_path, guard=_GUARDS[guard], monkeypatch=monkeypatch)

    assert "OK Daemon ready." not in res.output, res.output


@pytest.mark.parametrize("guard", sorted(_GUARDS))
def test_the_line_is_ascii(guard, tmp_path, monkeypatch, gates_open,
                           a_daemon_is_up):
    """DEC-32c: a cp1252 console must be able to print the whole screen."""
    res = _start_again(tmp_path, guard=_GUARDS[guard], monkeypatch=monkeypatch)

    res.output.encode("ascii")


# --------------------------------------------------------------------------- #
# nothing is wrong, so nothing warns
# --------------------------------------------------------------------------- #

def test_the_pidfile_guards_log_line_is_informational(tmp_path, monkeypatch,
                                                      caplog, gates_open,
                                                      a_daemon_is_up):
    """The witnessed record was WARNING. A daemon that is already doing its job
    is not a warning, and a log full of them is a log nobody reads."""
    caplog.set_level(logging.DEBUG, logger="systemu.scheduler.daemon")

    _start_again(tmp_path, guard=False, monkeypatch=monkeypatch)

    said = [r for r in caplog.records
            if r.name == "systemu.scheduler.daemon"
            and "already running" in r.getMessage().lower()]
    assert said, "the pidfile guard no longer records that it declined to spawn"
    for record in said:
        assert record.levelno <= logging.INFO, (
            "the idempotent case still logs at {}: {}".format(
                record.levelname, record.getMessage()))
        assert "Stop it first" not in record.getMessage(), record.getMessage()


# --------------------------------------------------------------------------- #
# the fact rides the VERDICT
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("guard", sorted(_GUARDS))
def test_both_guards_mint_the_already_serving_fact(guard, tmp_path, monkeypatch,
                                                   a_daemon_is_up):
    """The CLI cannot re-derive this: a ready verdict looks identical whether
    or not this invocation is what made it ready (DEC-43)."""
    vault_dir = _vault(tmp_path)
    monkeypatch.setattr(daemon_mod, "_already_serving_this_socket",
                        lambda vd, port: _GUARDS[guard])
    if not _GUARDS[guard]:
        daemon_mod._pid_file_path(vault_dir).write_text(str(_LIVE_PID),
                                                        encoding="utf-8")

    verdict = daemon_mod.start_daemon(
        vault_dir=vault_dir, config=SimpleNamespace(), vault=SimpleNamespace(),
        port=_SERVED_PORT, wait_timeout_s=1.0)

    assert verdict is not None and verdict.ready is True, verdict
    assert verdict.already_serving is True, verdict


def test_a_start_that_really_spawned_does_not_claim_it_was_already_serving(
        tmp_path, monkeypatch):
    """The fact must be FALSE by default, or every successful first start would
    report itself as a no-op."""
    monkeypatch.setattr(daemon_mod, "_connection_succeeds",
                        lambda host, port, timeout: False)
    verdict = daemon_mod.await_readiness(
        _vault(tmp_path), port=_SERVED_PORT, timeout_s=0.0,
        poll_s=0.01, probe_timeout=0.01)

    assert verdict.already_serving is False, verdict


def test_daemon_start_consumes_the_minted_fact():
    """Reachability pin (the standing rule): delete the production call site
    and a NAMED test goes red."""
    source = inspect.getsource(cc.daemon_start.callback)

    assert "already_serving" in source, (
        "`daemon start` no longer reads whether the daemon was already there, "
        "so an idempotent run claims it started one:\n" + source)


# --------------------------------------------------------------------------- #
# what is kept
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("guard", sorted(_GUARDS))
def test_the_build_skew_disclosure_survives_the_idempotent_path(
        guard, tmp_path, monkeypatch, gates_open, a_daemon_is_up):
    """F13's screen IS this one: new CLI, old daemon still holding the port.
    A skew is LOUD here or it is nowhere."""
    monkeypatch.setattr(cc, "_print_daemon_build",
                        lambda match, note: __import__("click").echo(
                            "BUILD-LINE {}".format(note)))
    res = _start_again(tmp_path, guard=_GUARDS[guard], monkeypatch=monkeypatch)

    assert "BUILD-LINE" in res.output, (
        "the idempotent path dropped the build disclosure, which is exactly "
        "the mid-upgrade screen it exists for:\n" + res.output)
