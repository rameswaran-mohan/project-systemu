"""D-6 -- on `daemon status`, the port's provenance is said exactly once.

THE WITNESS (0.10.31 human-style e2e, 80-column console)
--------------------------------------------------------
`daemon status` against a running daemon printed::

    | (*) Ready  (PID 30616)                                          |
    | http://127.0.0.1:55193                                          |
    accepting connections on http://127.0.0.1:55193 (port 55193 is the one this vault's daemon recorded)
    vault: ...
    port 55193 is the one this vault's daemon recorded

The same sentence, twice, two lines apart -- once folded into the reason and
once on its own line. The not-running verdict repeats the default-port sentence
("port 8765 is the built-in default -- nothing here named a port, so this is a
guess") the same way, which is the copy that matters most: a guess restated
twice reads as two independent confirmations of a number nobody chose.

THE RULING
    The provenance note appears exactly ONCE per verdict. The unwrapped
    standalone line stays -- it is the one `_print_daemon_where` mints from the
    projection, on the writer that does not fold -- and the copy inside the
    reason is stripped.

AND THE COPY IS DE-DUPLICATED, NOT RE-DERIVED. The clause removed from the
reason is removed by EXACT-STRING IDENTITY against the note the projection
already carries (``status["port_provenance"]``, minted by
``daemon.port_provenance_note``) -- never by parsing the sentence for something
that looks like a port clause. This is the same de-duplication P8 applied to
the ``vault:`` clause, on the second clause the mint folds into every reason.

NO REAL DAEMON, NO REAL PORT. The reason under test is minted by the REAL
``probe_readiness`` and projected by the REAL ``get_status``; only the two
seams that would touch the machine -- the TCP connection witness and the
pidfile process probe -- are faked, so nothing is spawned, no socket is opened
and no assumption is made about any port on this machine being free.
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

import systemu.interface.cli_commands as cc
import systemu.scheduler.daemon as daemon_mod


#: The width the duplication was witnessed at.
_COLUMNS = "80"

#: A port number that is never contacted: the connection witness is faked, so
#: this is only ever text in a sentence. Nothing is bound and nothing is
#: assumed about what may be listening here.
_RECORDED_PORT = 55193

#: The three verdicts, by the two facts that select the renderer's branch, and
#: the exit code each is ruled to answer with (D6).
_VERDICTS = {
    "ready": (True, True, 0),
    "starting": (False, True, 2),
    "not-running": (False, False, 1),
}


def _vault(tmp_path) -> str:
    """A vault root under ``tmp_path``. Never the worktree's own vault: the
    sidecar below is a real file write."""
    root = tmp_path / "operating-home" / "systemu" / "vault"
    root.mkdir(parents=True, exist_ok=True)
    return str(root)


def _run(monkeypatch, tmp_path, verdict: str, *, record_port: bool):
    """`daemon status` at 80 columns, with the mint REAL and the machine faked.

    ``record_port`` selects which provenance sentence the mint produces: the
    witnessed "this vault's daemon recorded" one when a sidecar names a port,
    and the "built-in default ... this is a guess" one when nothing does.
    """
    vault_dir = _vault(tmp_path)
    connected, alive, ruled_code = _VERDICTS[verdict]

    if record_port:
        daemon_mod._write_runtime_state(vault_dir, pid=4242, port=_RECORDED_PORT)
    monkeypatch.delenv("SYSTEMU_DASHBOARD_PORT", raising=False)
    monkeypatch.setattr(daemon_mod, "_connection_succeeds",
                        lambda host, port, timeout: connected)
    monkeypatch.setattr(daemon_mod, "_pidfile_process",
                        lambda vd: ((4242, True) if alive else (None, False)))
    monkeypatch.setenv("COLUMNS", _COLUMNS)

    res = CliRunner().invoke(
        cc.daemon_status, [],
        obj={"config": SimpleNamespace(vault_dir=vault_dir),
             "vault": SimpleNamespace()},
        env={"COLUMNS": _COLUMNS},
    )
    assert res.exit_code == ruled_code, res.output

    status = daemon_mod.get_status(vault_dir)
    note = status["port_provenance"]
    assert note, "fixture premise: the projection carries a provenance note"
    return res.output, note


# --------------------------------------------------------------------------- #
# THE REPRO -- once per verdict, on both provenance sentences
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("verdict", sorted(_VERDICTS))
def test_the_recorded_port_provenance_is_said_once(verdict, tmp_path, monkeypatch):
    """The witnessed screen: `port 55193 is the one this vault's daemon
    recorded`, printed twice, two lines apart."""
    output, note = _run(monkeypatch, tmp_path, verdict, record_port=True)

    assert output.count(note) == 1, (
        f"the {verdict} verdict says the port's provenance "
        f"{output.count(note)} times (wanted once):\n{output}")


@pytest.mark.parametrize("verdict", sorted(_VERDICTS))
def test_the_default_port_provenance_is_said_once(verdict, tmp_path, monkeypatch):
    """The copy that matters most. 8765 with no record is a GUESS this process
    made, and a guess restated twice reads as two confirmations."""
    output, note = _run(monkeypatch, tmp_path, verdict, record_port=False)

    assert "this is a guess" in note, (
        "fixture premise: nothing named a port, so the note is the default one")
    assert output.count(note) == 1, (
        f"the {verdict} verdict says the port's provenance "
        f"{output.count(note)} times (wanted once):\n{output}")


# --------------------------------------------------------------------------- #
# and the surviving copy is the RIGHT one, on the writer that does not fold
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("verdict", sorted(_VERDICTS))
def test_the_surviving_copy_is_the_unwrapped_standalone_line(verdict, tmp_path,
                                                             monkeypatch):
    """The line kept is `_print_daemon_where`'s: the note ALONE on its own row,
    not a fragment of a sentence that Rich may fold."""
    output, note = _run(monkeypatch, tmp_path, verdict, record_port=True)

    assert note in [ln.strip() for ln in output.splitlines()], (
        "the provenance note no longer stands on its own line:\n" + output)


@pytest.mark.parametrize("verdict", sorted(_VERDICTS))
def test_nothing_the_reason_carried_is_lost(verdict, tmp_path, monkeypatch):
    """Stripping a clause must not strip the sentence around it: the port
    NUMBER, the verdict's own words and the `--port` remedy all survive."""
    output, _note = _run(monkeypatch, tmp_path, verdict, record_port=True)

    assert str(_RECORDED_PORT) in output, output
    if verdict == "not-running":
        assert "pass --port <number>" in output, output
    if verdict == "starting":
        assert "nothing is accepting connections" in output, output
    if verdict == "ready":
        assert "accepting connections on" in output, output


# --------------------------------------------------------------------------- #
# the de-duplication is identity, not parsing
# --------------------------------------------------------------------------- #

def test_the_provenance_clause_is_removed_by_identity_with_the_minted_note():
    """The clause dropped is the one `port_provenance_note` PRODUCED, matched
    whole. A sentence-parser looking for something port-shaped would be a
    second, disagreeing derivation of a fact the mint already answered."""
    root = r"C:\some\where\systemu\vault"
    note = daemon_mod.port_provenance_note(_RECORDED_PORT, "recorded")
    reason = "accepting connections on http://127.0.0.1:{} ({}); {}".format(
        _RECORDED_PORT, note, daemon_mod.vault_note(root))

    trimmed = cc._reason_outside_the_panel(reason, root, note)

    assert note not in trimmed, trimmed
    assert "accepting connections on" in trimmed, trimmed
    assert root not in trimmed, trimmed


def test_a_reason_that_never_named_the_provenance_is_left_alone():
    """A status dict minted by an older daemon whose reason has no provenance
    clause must still print in full."""
    text = "a reason line the panel may or may not print"

    assert cc._reason_outside_the_panel(text, r"C:\v", "port 1 came from x") == text


def test_a_missing_provenance_note_prints_the_reason_unchanged():
    """`.get` on a projection an older daemon minted answers None, and the
    renderer must still print the reason rather than raising."""
    text = "no live daemon process (port 8765 is the built-in default)"

    assert cc._reason_outside_the_panel(text, None, None) == text
    assert cc._reason_outside_the_panel(text, None, "") == text


def test_daemon_status_hands_the_minted_note_to_the_reason_printer():
    """Reachability pin (the standing rule): the de-duplication is driven by
    the projection's own note, so deleting that argument at the production call
    site makes a NAMED test go red."""
    source = inspect.getsource(cc.daemon_status.callback)

    assert "_print_daemon_reason(" in source, source
    assert "port_provenance" in source, (
        "`daemon status` no longer feeds the minted provenance note to the "
        "reason printer, so the reason can carry a second copy of it:\n" + source)
