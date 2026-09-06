"""D6 -- `daemon status` names the port it PROBED, where that port came from,
and the vault it probed FOR.

THE REPRO (human e2e run of v0.10.27, scratch install)
    The daemon was started with `--port 51817`. While it ran, `status` correctly
    showed `http://127.0.0.1:51817`. After `daemon stop` -- which deletes the
    runtime sidecar that recorded 51817 -- `status` printed:

        no live daemon process and nothing is accepting connections
        on 127.0.0.1:8765

    8765 is the built-in default. It was never involved in this session. The
    operator is told, in the same breath, a true fact (nothing answers on 8765)
    and a misleading one (that 8765 is the socket their daemon would use). The
    line also never names WHICH VAULT it looked in, so an operator standing in
    the wrong directory reads a correct answer about a vault they did not mean.

THE PROPERTY
    Every readiness verdict names:
      * the port it actually probed, and
      * WHERE that port came from -- `--port`, the daemon's own record, the
        environment, or the built-in default -- so a defaulted guess can never
        be read as the operator's own number; and
      * the operating vault root, taken from THE mint
        (`systemu.runtime.vault_root.resolve_vault_root`), running or not.

    A not-ready verdict additionally names the remedy for the one case the
    system cannot know: `--port`, for a daemon started on another socket.

NO REAL DAEMON IS STARTED HERE. Every vault lives under `tmp_path`.
"""
from __future__ import annotations

import socket
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

import systemu.scheduler.daemon as daemon_mod
from systemu.runtime.vault_root import resolve_vault_root


# ── helpers ──────────────────────────────────────────────────────────────────

#: Rich draws panels with box-drawing characters and folds long lines. Neither
#: is content, so operator-visible text is compared with both removed.
_BOX = "┌┐└┘─│╭╮╰╯├┤"


def _flat(text: str) -> str:
    return "".join(c for c in text if not c.isspace() and c not in _BOX)


def _closed_port() -> int:
    """A port number that is (as of this instant) not accepting connections."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture()
def vault_dir(tmp_path, monkeypatch) -> str:
    """A vault under tmp_path whose PARENT holds the daemon pid/runtime files.

    The daemon's own env var is pinned at the same place so nothing in this test
    can reach the worktree's `systemu/vault/`.
    """
    v = tmp_path / "home" / "vault"
    v.mkdir(parents=True)
    monkeypatch.setenv("SYSTEMU_VAULT_DIR", str(v))
    monkeypatch.delenv("SYSTEMU_DASHBOARD_PORT", raising=False)
    return str(v)


def _minted_root(vault_dir: str) -> str:
    return resolve_vault_root(explicit=vault_dir).root


# ── the port that was probed, and where it came from ─────────────────────────

def test_the_repro_after_a_stop_the_default_port_is_disclosed_as_a_guess(vault_dir):
    """THE REPRO, in the state transition that produced it.

    The daemon recorded its real port in the sidecar; `daemon stop` deletes that
    record; the next `status` therefore has nothing left to consult and falls
    back to 8765 -- a number the operator never chose, printed with the same
    confidence as one they did.
    """
    daemon_mod._write_runtime_state(vault_dir, pid=None, port=_closed_port())
    daemon_mod.stop_daemon(vault_dir)          # takes the recorded port with it

    verdict = daemon_mod.probe_readiness(vault_dir, timeout=0.2)

    assert verdict.ready is False
    assert verdict.port == daemon_mod.DEFAULT_DASHBOARD_PORT
    # Through the provenance CLAUSE: `tmp_path` carries the test's own name, so
    # a bare substring test against the whole line could pass on the path.
    note = daemon_mod.port_provenance_note(verdict.port, verdict.port_source)
    assert "default" in note.lower(), note
    assert note in verdict.reason, (
        "the fallback port is presented as fact, not as the guess it is: "
        + verdict.reason)
    assert "--port" in verdict.reason, verdict.reason


def test_an_explicitly_probed_port_never_shares_the_line_with_the_default(vault_dir):
    """`status --port N` must talk about N -- and must not put 8765 in front of
    the operator at all, since 8765 was not probed."""
    port = _closed_port()
    verdict = daemon_mod.probe_readiness(vault_dir, port=port, timeout=0.2)

    assert verdict.ready is False
    assert str(port) in verdict.reason, verdict.reason
    assert str(daemon_mod.DEFAULT_DASHBOARD_PORT) not in verdict.reason, (
        "the not-running line named the built-in default port, which was never "
        "probed: " + verdict.reason)


def test_an_explicit_port_is_disclosed_as_coming_from_the_port_flag(vault_dir):
    port = _closed_port()
    verdict = daemon_mod.probe_readiness(vault_dir, port=port, timeout=0.2)

    assert verdict.port_source == "explicit", verdict.port_source
    assert "--port" in verdict.reason, verdict.reason


def test_the_defaulted_port_says_it_is_the_default_and_offers_the_remedy(vault_dir):
    """No `--port`, no record, no env: 8765 is a GUESS and must say so, plus the
    one thing the operator can do about it."""
    verdict = daemon_mod.probe_readiness(vault_dir, timeout=0.2)

    assert verdict.port == daemon_mod.DEFAULT_DASHBOARD_PORT
    assert verdict.port_source == "default", verdict.port_source
    assert str(daemon_mod.DEFAULT_DASHBOARD_PORT) in verdict.reason
    # Asserted through the provenance CLAUSE, not the whole line: `tmp_path` is
    # named after the test, so a bare `"default" in reason` would pass on the
    # vault path alone and never see the clause go missing.
    note = daemon_mod.port_provenance_note(verdict.port, verdict.port_source)
    assert "default" in note.lower(), note
    assert note in verdict.reason, verdict.reason
    assert "--port" in verdict.reason, (
        "a defaulted port must tell the operator how to name the real one: "
        + verdict.reason)


def test_the_port_the_daemon_recorded_is_disclosed_as_the_daemons_own(vault_dir):
    port = _closed_port()
    daemon_mod._write_runtime_state(vault_dir, pid=None, port=port)

    verdict = daemon_mod.probe_readiness(vault_dir, timeout=0.2)

    assert verdict.port == port
    assert verdict.port_source == "recorded", verdict.port_source
    assert str(port) in verdict.reason


def test_an_env_port_is_disclosed_as_coming_from_the_environment(vault_dir,
                                                                 monkeypatch):
    port = _closed_port()
    monkeypatch.setenv("SYSTEMU_DASHBOARD_PORT", str(port))

    verdict = daemon_mod.probe_readiness(vault_dir, timeout=0.2)

    assert verdict.port == port
    assert verdict.port_source == "env", verdict.port_source
    assert "SYSTEMU_DASHBOARD_PORT" in verdict.reason, verdict.reason


# ── the vault it probed FOR ──────────────────────────────────────────────────

def test_a_not_running_verdict_names_the_vault_root_from_the_mint(vault_dir):
    verdict = daemon_mod.probe_readiness(vault_dir, port=_closed_port(),
                                         timeout=0.2)

    assert verdict.vault_root == _minted_root(vault_dir)
    assert _flat(_minted_root(vault_dir)) in _flat(verdict.reason), verdict.reason


def test_a_starting_verdict_names_the_vault_root_too(vault_dir):
    """The `process_alive but not connectable` branch -- a live PID of our own
    with nothing listening. `os.getpid()` is alive by construction."""
    import os
    (tmp_parent := daemon_mod._pid_file_path(vault_dir)).write_text(str(os.getpid()))
    assert tmp_parent.exists()

    verdict = daemon_mod.probe_readiness(vault_dir, port=_closed_port(),
                                         timeout=0.2)

    assert verdict.process_alive is True
    assert verdict.ready is False
    assert _flat(_minted_root(vault_dir)) in _flat(verdict.reason), verdict.reason


def test_a_ready_verdict_names_the_vault_root_too(vault_dir):
    """A real listening socket plus a live tracked PID -- the only shape that
    mints ready=True. The vault is part of the answer here as well: "ready" for
    WHICH vault is the question a two-vault machine actually asks."""
    import os
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.listen(5)
    daemon_mod._pid_file_path(vault_dir).write_text(str(os.getpid()))
    try:
        verdict = daemon_mod.probe_readiness(vault_dir, port=port, timeout=1.0)
    finally:
        srv.close()

    assert verdict.ready is True, verdict.reason
    assert verdict.vault_root == _minted_root(vault_dir)
    assert _flat(_minted_root(vault_dir)) in _flat(verdict.reason), verdict.reason


def test_get_status_projects_both_new_facts(vault_dir):
    """`get_status` is a pure PROJECTION of the mint (DEC-43) -- a fact added to
    the verdict that never reaches the dict reaches no surface either."""
    port = _closed_port()
    status = daemon_mod.get_status(vault_dir, port=port, timeout=0.2)

    assert status["port_source"] == "explicit"
    assert status["vault_root"] == _minted_root(vault_dir)


# ── the operator surface, rendered by the real CLI command ───────────────────

def test_the_status_command_prints_the_probed_port_and_the_vault(vault_dir,
                                                                 monkeypatch):
    """End to end through the shipped renderer: `systemu daemon status --port N`
    on a stopped daemon. This is the exact command from the repro."""
    monkeypatch.setenv("COLUMNS", "200")
    from systemu.interface.cli_commands import daemon_status

    port = _closed_port()
    res = CliRunner().invoke(
        daemon_status, ["--port", str(port)],
        obj={"config": SimpleNamespace(vault_dir=vault_dir),
             "vault": SimpleNamespace()},
    )

    flat = _flat(res.output)
    assert str(port) in flat, res.output
    assert str(daemon_mod.DEFAULT_DASHBOARD_PORT) not in flat, res.output
    assert _flat(_minted_root(vault_dir)) in flat, res.output


def test_the_bare_status_command_admits_the_port_is_a_default(vault_dir,
                                                              monkeypatch):
    monkeypatch.setenv("COLUMNS", "200")
    from systemu.interface.cli_commands import daemon_status

    res = CliRunner().invoke(
        daemon_status, [],
        obj={"config": SimpleNamespace(vault_dir=vault_dir),
             "vault": SimpleNamespace()},
    )

    flat = _flat(res.output)
    note = daemon_mod.port_provenance_note(daemon_mod.DEFAULT_DASHBOARD_PORT,
                                           "default")
    assert str(daemon_mod.DEFAULT_DASHBOARD_PORT) in flat, res.output
    assert _flat(note) in flat, res.output
    assert "--port" in flat, res.output


# ── the text is ASCII (DEC-32c) ──────────────────────────────────────────────

def test_the_new_verdict_text_is_ascii_only(vault_dir):
    """A verdict-carrying string that cannot be encoded on a cp1252 console is a
    verdict the operator does not get to read."""
    verdict = daemon_mod.probe_readiness(vault_dir, port=_closed_port(),
                                         timeout=0.2)
    note = daemon_mod.port_provenance_note(verdict.port, verdict.port_source)
    note.encode("ascii")
    daemon_mod.vault_note(verdict.vault_root).encode("ascii")
