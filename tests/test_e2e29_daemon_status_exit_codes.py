"""D6 -- `daemon status` exits with a code that NAMES the verdict.

WITNESSED DEFECT (v0.10.28)
    Every verdict left `daemon status` with exit code 0. "Ready", "Starting"
    and "Not running" were distinguishable only by reading a Rich panel, so the
    single thing a script, a CI step, a health check or a `&&` chain can act on
    said the same word for all three:

        systemu daemon status && open http://127.0.0.1:8765

    ...opens a dashboard that is not there. The command that exists to answer
    "is it up?" answered "yes" whatever the answer was.

THE RULING
    The exit code IS the verdict:

        0  Ready        -- the dashboard port accepted a connection
        2  Starting     -- the tracked process is alive, nothing listening yet
        1  Not running  -- nothing this vault tracks is serving that port

    "Not running" absorbs the one case that reads like a fourth: something IS
    accepting connections on the probed port, but no systemu daemon is tracked
    for this vault. That is a foreign program on the socket, and for the
    operator asking "is MY daemon up?" the honest answer is no -- 1, not 0.
    Reporting it as ready is the DEC-41 defect (a claim without an effect), and
    an exit code that disagrees with the panel would be a second derivation of
    the same fact.

    The three codes are documented in `daemon status --help`: an exit code no
    one is told about is not an interface, and a script author reading only the
    help must not have to guess which number means down.

WHAT IS NOT PINNED HERE
    That a build skew leaves the code alone -- `test_daemon_status_reports_the
    _skew` in tests/test_daemon_build_skew_witness.py already asserts exit 0 on
    a Ready-with-mismatch verdict, and a cheaper second copy of an existing
    check buys nothing (DEC-34).

NO REAL DAEMON IS STARTED HERE. The mint is monkeypatched for the verdict
matrix; the untracked-listener case drives the REAL probe against a real
listening socket this test opens and closes, and every vault lives under
`tmp_path`.
"""
from __future__ import annotations

import os
import re
import socket
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

import systemu.scheduler.daemon as daemon_mod
from systemu.interface import cli_commands as cc


#: Wide enough that no assertion below can turn into a line-wrapping test.
_COLUMNS = "200"

#: THE RULING, as data: verdict -> the exit code that names it.
_RULED_CODES = {"ready": 0, "starting": 2, "not-running": 1}

#: The flags on the projected status dict that select each verdict's branch.
_VERDICT_FLAGS = {
    "ready": dict(ready=True, process_alive=True),
    "starting": dict(ready=False, process_alive=True),
    "not-running": dict(ready=False, process_alive=False),
}


def _status(vault_root: str, *, ready: bool, process_alive: bool) -> dict:
    """One status dict in the exact shape `daemon.get_status` projects."""
    return {
        "running": ready,
        "ready": ready,
        "pid": 4242,
        "process_alive": process_alive,
        "host": "127.0.0.1",
        "port": 8765,
        "url": "http://127.0.0.1:8765",
        "reason": "a reason line the panel may or may not print",
        "port_source": "default",
        "vault_root": vault_root,
        "port_provenance": daemon_mod.port_provenance_note(8765, "default"),
        "daemon_version": "0.10.28",
        "daemon_path": "somewhere",
        "cli_version": "0.10.28",
        "cli_path": "somewhere",
        "build_match": True,
        "build_note": "daemon and CLI are the same build",
    }


def _invoke(monkeypatch, tmp_path, verdict: str):
    """Run `daemon status` with the MINT monkeypatched to one verdict.

    The seam is `daemon.get_status` -- the same one tests/test_e2e28_daemon_
    status_every_verdict_names_the_vault.py patches, and the one the command
    actually imports and calls -- so nothing here is a stub of the renderer
    under test.
    """
    vault_root = str(tmp_path / "home" / "vault")
    os.makedirs(vault_root, exist_ok=True)
    monkeypatch.setenv("COLUMNS", _COLUMNS)
    monkeypatch.setattr(
        daemon_mod, "get_status",
        lambda vault_dir, **kw: _status(vault_root, **_VERDICT_FLAGS[verdict]))

    return CliRunner().invoke(
        cc.daemon_status, [],
        obj={"config": SimpleNamespace(vault_dir=vault_root),
             "vault": SimpleNamespace()},
        env={"COLUMNS": _COLUMNS},
    )


# --------------------------------------------------------------------------- #
# The matrix: one code per verdict, and no two verdicts sharing one
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("verdict", sorted(_RULED_CODES))
def test_each_verdict_exits_with_the_code_that_names_it(verdict, tmp_path,
                                                        monkeypatch):
    res = _invoke(monkeypatch, tmp_path, verdict)

    assert res.exit_code == _RULED_CODES[verdict], (
        f"the {verdict} verdict exited {res.exit_code}, not "
        f"{_RULED_CODES[verdict]}:\n{res.output}")


def test_no_two_verdicts_share_an_exit_code(tmp_path, monkeypatch):
    """The property, not three constants. Codes that collide are exactly the
    defect -- 0/0/0 satisfies "every verdict has a code" and tells a script
    nothing."""
    seen = {}
    for verdict in sorted(_VERDICT_FLAGS):
        code = _invoke(monkeypatch, tmp_path, verdict).exit_code
        assert code not in seen, (
            f"{verdict!r} and {seen[code]!r} both exit {code}, so no caller can "
            f"tell them apart")
        seen[code] = verdict


def test_a_not_ready_verdict_never_exits_zero(tmp_path, monkeypatch):
    """THE defect, stated as the one bit a `&&` chain reads: zero means the
    dashboard is reachable, and nothing else may claim it."""
    for verdict in ("starting", "not-running"):
        res = _invoke(monkeypatch, tmp_path, verdict)
        assert res.exit_code != 0, (
            f"the {verdict} verdict exited 0, so `systemu daemon status && ...` "
            f"runs against a dashboard that is not accepting connections:\n"
            + res.output)


def test_the_command_still_prints_its_panel_when_it_exits_non_zero(tmp_path,
                                                                   monkeypatch):
    """A non-zero exit must not swallow the report. The operator-facing facts --
    the verdict, the vault and the port's provenance -- are the reason the
    command exists; the code is an addition to them, not a replacement."""
    for verdict in ("starting", "not-running"):
        res = _invoke(monkeypatch, tmp_path, verdict)
        out = res.output
        assert daemon_mod.port_provenance_note(8765, "default") in out, out
        assert str(tmp_path) in out, out
        assert res.exception is None or isinstance(res.exception, SystemExit), (
            f"the {verdict} verdict exited by raising, not by naming a code: "
            f"{res.exception!r}")


# --------------------------------------------------------------------------- #
# The case that reads like a fourth verdict, driven through the REAL probe
# --------------------------------------------------------------------------- #

def test_a_foreign_listener_on_the_probed_port_is_not_running_not_ready(
        tmp_path, monkeypatch):
    """Something IS accepting connections; no systemu daemon is tracked here.

    Nothing is patched: a real socket is opened on a real port and the shipped
    probe is asked about it. For the operator asking "is MY daemon up?" the
    answer is no, and the exit code must say so -- 1. A 0 here would report a
    foreign program as this vault's dashboard.
    """
    vault = tmp_path / "home" / "vault"
    vault.mkdir(parents=True)
    monkeypatch.setenv("SYSTEMU_VAULT_DIR", str(vault))
    monkeypatch.delenv("SYSTEMU_DASHBOARD_PORT", raising=False)
    monkeypatch.setenv("COLUMNS", _COLUMNS)

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.listen(5)
    try:
        # No pidfile is written: this vault tracks no daemon at all.
        assert not daemon_mod._pid_file_path(str(vault)).exists()
        verdict = daemon_mod.probe_readiness(str(vault), port=port, timeout=1.0)

        # The premise is witnessed, not assumed: this really is the
        # connected-but-untracked branch and not merely a closed port.
        assert verdict.ready is False, verdict
        assert verdict.process_alive is False, verdict
        assert "no systemu daemon is tracked" in verdict.reason, verdict.reason

        res = CliRunner().invoke(
            cc.daemon_status, ["--port", str(port)],
            obj={"config": SimpleNamespace(vault_dir=str(vault)),
                 "vault": SimpleNamespace()},
            env={"COLUMNS": _COLUMNS},
        )
    finally:
        srv.close()

    assert res.exit_code == _RULED_CODES["not-running"], (
        "a foreign listener on the probed port was reported as this vault's "
        f"daemon (exit {res.exit_code}):\n{res.output}")
    assert "no systemu daemon is tracked" in res.output.replace("\n", " "), \
        res.output


def test_a_real_ready_daemon_exits_zero_with_nothing_patched(tmp_path,
                                                             monkeypatch):
    """The other end of the same real path, so the exit code is pinned to the
    shipped mint and not only to a hand-made dict: a live tracked PID plus a
    real listening socket is the ONE shape that mints ready, and it exits 0."""
    vault = tmp_path / "home" / "vault"
    vault.mkdir(parents=True)
    monkeypatch.setenv("SYSTEMU_VAULT_DIR", str(vault))
    monkeypatch.delenv("SYSTEMU_DASHBOARD_PORT", raising=False)
    monkeypatch.setenv("COLUMNS", _COLUMNS)

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.listen(5)
    # This test process stands in for the tracked daemon: certainly alive.
    daemon_mod._pid_file_path(str(vault)).write_text(str(os.getpid()))
    try:
        res = CliRunner().invoke(
            cc.daemon_status, ["--port", str(port)],
            obj={"config": SimpleNamespace(vault_dir=str(vault)),
                 "vault": SimpleNamespace()},
            env={"COLUMNS": _COLUMNS},
        )
    finally:
        srv.close()

    assert res.exit_code == _RULED_CODES["ready"], res.output


# --------------------------------------------------------------------------- #
# An exit code nobody is told about is not an interface
# --------------------------------------------------------------------------- #

def _help_text() -> str:
    res = CliRunner().invoke(cc.daemon_status, ["--help"],
                             env={"COLUMNS": _COLUMNS})
    assert res.exit_code == 0, res.output
    return res.output


def test_help_documents_every_ruled_exit_code_beside_its_verdict():
    """A script author reading only `--help` must not have to guess which
    number means down. Each code and the verdict it names on ONE line, so the
    pairing survives being read."""
    help_text = _help_text()
    assert "exit code" in help_text.lower(), help_text
    for verdict, code in sorted(_RULED_CODES.items()):
        words = {"not-running": "not running"}.get(verdict, verdict)
        assert re.search(rf"(?m)^.*\b{code}\b.*{re.escape(words)}.*$",
                         help_text, re.IGNORECASE), (
            f"`--help` does not pair exit code {code} with {words!r}:\n"
            + help_text)


def test_help_says_a_foreign_listener_is_reported_as_not_running():
    """The one case an operator would otherwise read as ready-ish. If the help
    does not say where it lands, the code cannot be acted on."""
    help_text = _help_text().lower().replace("\n", " ")
    help_text = " ".join(help_text.split())
    assert "listening" in help_text or "accepting connections" in help_text, \
        help_text
    assert "tracked" in help_text, help_text


def test_the_help_text_is_ascii(tmp_path):
    """DEC-32c: verdict-carrying text a cp1252 console cannot encode is text the
    operator never gets to read. Only the lines this ruling adds are checked --
    Rich's own box art elsewhere is not this command's help."""
    for line in _help_text().splitlines():
        if "exit" in line.lower() or re.search(r"\b[012]\b", line):
            line.encode("ascii")
