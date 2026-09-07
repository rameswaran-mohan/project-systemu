"""D5 -- the daemon lifecycle VERDICT lines are ASCII.

THE DEFECT (e2e regression of v0.10.29)
    The four lines that carry a lifecycle verdict were prefixed with glyphs:

        U+26A1 lightning   ".. Starting Systemu daemon on port 8765 ..."
        U+2713 check       "OK Daemon ready."
        U+2713 check       "OK Daemon stopped."
        U+2717 ballot X    "ERROR Daemon did not become ready."

    DEC-32c: verdict-carrying output is ASCII-only. A console that cannot
    encode a verdict does not print a slightly plainer verdict -- it raises a
    UnicodeEncodeError instead of printing one, and the operator is left with
    a traceback where the answer should have been. `daemon start` and
    `daemon stop` are the first two commands a new install runs.

THE PROPERTY
    The lifecycle verdict LINES are pure ASCII and carry ASCII prefixes --
    `OK`, `ERROR`, `..`. The Rich panel bullets on `daemon status`
    (U+25CF / U+25CB / U+25D0) are decoration on a bordered panel, not
    verdicts, and are deliberately left alone.

NO REAL DAEMON. `start_daemon` and `stop_daemon` are monkeypatched at
`systemu.scheduler.daemon`, together with the dashboard-extra gate and the
provider gate that run before them, so nothing is spawned, no port is bound
and no vault is written.
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

import systemu.interface.cli_commands as cc
import systemu.scheduler.daemon as daemon_mod


#: Every non-ASCII codepoint that was on a lifecycle verdict line.
_GLYPHS = ("✓", "✗", "⚡")


@pytest.fixture()
def gates_open(monkeypatch):
    """The two refusals `daemon start` makes BEFORE it spawns anything.

    Patched at their own modules, not replaced with a stand-in for the command
    under test: the dashboard extra and a usable provider are real gates and
    this test is not about either of them.
    """
    monkeypatch.setattr("systemu.runtime.optional_deps.missing_groups",
                        lambda groups: ())
    monkeypatch.setattr("sharing_on.setup_flow.key_present", lambda: True)


def _verdict(*, ready: bool):
    return daemon_mod.DaemonReadiness(
        ready=ready,
        pid=4242,
        process_alive=True,
        host="127.0.0.1",
        port=8765,
        reason=("accepting connections on http://127.0.0.1:8765" if ready
                else "nothing is accepting on 127.0.0.1:8765"),
    )


def _run_start(monkeypatch, tmp_path, *, ready: bool):
    monkeypatch.setattr(daemon_mod, "start_daemon",
                        lambda **kw: _verdict(ready=ready))
    return CliRunner().invoke(
        cc.daemon_start, [],
        obj={"config": SimpleNamespace(vault_dir=str(tmp_path / "vault")),
             "vault": SimpleNamespace()},
    )


def _run_stop(monkeypatch, tmp_path, *, stopped: bool):
    monkeypatch.setattr(daemon_mod, "stop_daemon", lambda vault_dir: stopped)
    return CliRunner().invoke(
        cc.daemon_stop, [],
        obj={"config": SimpleNamespace(vault_dir=str(tmp_path / "vault")),
             "vault": SimpleNamespace()},
    )


def _carrier(output: str, needle: str) -> str:
    lines = [ln for ln in output.splitlines() if needle in ln]
    assert lines, "no line carries {!r}:\n{}".format(needle, output)
    return lines[0]


# --------------------------------------------------------------------------- #
# `daemon start`
# --------------------------------------------------------------------------- #

def test_the_ready_verdict_is_ascii_and_says_OK(gates_open, tmp_path,
                                                monkeypatch):
    res = _run_start(monkeypatch, tmp_path, ready=True)

    assert res.exit_code == 0, res.output
    line = _carrier(res.output, "Daemon ready.")
    line.encode("ascii")
    assert line.strip().startswith("OK"), line


def test_the_starting_line_is_ascii(gates_open, tmp_path, monkeypatch):
    """The first line the command prints, and the one a cp1252 console would
    have failed on before anything else happened."""
    res = _run_start(monkeypatch, tmp_path, ready=True)

    line = _carrier(res.output, "Starting Systemu daemon on port")
    line.encode("ascii")
    assert line.strip().startswith(".."), line


def test_the_not_ready_verdict_is_ascii_and_says_ERROR(gates_open, tmp_path,
                                                       monkeypatch):
    """DEC-41: not started != success. This is the line that carries the
    failure, so it is the one that must not be replaced by a traceback."""
    res = _run_start(monkeypatch, tmp_path, ready=False)

    assert res.exit_code == 1, res.output
    line = _carrier(res.output, "Daemon did not become ready.")
    line.encode("ascii")
    assert line.strip().startswith("ERROR"), line


@pytest.mark.parametrize("ready", [True, False])
def test_no_lifecycle_glyph_survives_anywhere_in_the_start_output(
        gates_open, tmp_path, monkeypatch, ready):
    res = _run_start(monkeypatch, tmp_path, ready=ready)

    for glyph in _GLYPHS:
        assert glyph not in res.output, (
            "{!r} is still on a `daemon start` line:\n{}".format(
                glyph, res.output))


# --------------------------------------------------------------------------- #
# `daemon stop`
# --------------------------------------------------------------------------- #

def test_the_stopped_verdict_is_ascii_and_says_OK(tmp_path, monkeypatch):
    res = _run_stop(monkeypatch, tmp_path, stopped=True)

    assert res.exit_code == 0, res.output
    line = _carrier(res.output, "Daemon stopped.")
    line.encode("ascii")
    assert line.strip().startswith("OK"), line


def test_the_not_running_line_stays_as_it_is(tmp_path, monkeypatch):
    """The control: this line never carried a glyph, and nothing here should
    have given it one."""
    res = _run_stop(monkeypatch, tmp_path, stopped=False)

    line = _carrier(res.output, "Daemon is not running.")
    line.encode("ascii")


@pytest.mark.parametrize("stopped", [True, False])
def test_no_lifecycle_glyph_survives_anywhere_in_the_stop_output(
        tmp_path, monkeypatch, stopped):
    res = _run_stop(monkeypatch, tmp_path, stopped=stopped)

    for glyph in _GLYPHS:
        assert glyph not in res.output, (
            "{!r} is still on a `daemon stop` line:\n{}".format(
                glyph, res.output))


# --------------------------------------------------------------------------- #
# Source-level: the glyphs are gone from the lifecycle callbacks themselves
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("command", ["daemon_start", "daemon_stop"])
def test_the_lifecycle_callbacks_carry_no_verdict_glyph(command):
    """A rendered line can be glyph-free because the branch was not reached.
    This reads the source, so an unreached branch cannot hide one.
    """
    source = inspect.getsource(getattr(cc, command).callback)
    for glyph in _GLYPHS:
        assert glyph not in source, (
            "{} still spells a verdict with {!r}".format(command, glyph))


def test_the_status_panel_bullets_are_left_alone():
    """The ruled boundary. The panel bullets are decoration on a bordered
    Rich panel that is already non-ASCII by construction; only the lines that
    CARRY a verdict were in scope, and rewriting the rest would be a change
    nobody asked for.
    """
    source = inspect.getsource(cc.daemon_status.callback)
    assert any(b in source for b in ("●", "○", "◐")), (
        "the status panel bullets were rewritten too:\n" + source)
