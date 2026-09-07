"""P8 -- on `daemon status`, paths live only on the unwrapped lines.

THE WITNESS (0.10.30 dogfood, 80 columns)
------------------------------------------
N7 moved the `vault:` line OUT of the Rich panel, and that line is correct. The
same path is still inside the panel as well, carried there by `status["reason"]`
-- which the mint ends with the very same `vault: <path>` clause -- and the
panel folds it:

    | ... (port 8765 is the built-in default -- nothing here named a   |
    | port, so this is a guess); vault:                                |
    | C:\\...\\Workspaces\\sample_project_                                |
    | root\\nested_worktrees_dir\\example-nested-folder-1a2b3c\\systemu\\vault; if |
    ...
    vault: C:\\...\\systemu\\vault        <- the good line, below the panel

So the screen shows the path TWICE: once broken across three panel rows, where
it can be neither pasted nor searched for, and once correctly. The wrong copy
is the eye-catching one, and an operator comparing two vaults reads the broken
one first.

THE RULING
    The panel carries the verdict, the PID and the URL. Paths live only on the
    unwrapped lines.

WHAT THIS MUST NOT COST. The reason is not decoration: on the not-running
verdict it carries the `--port` remedy, and on the connected-but-untracked
verdict it carries "no systemu daemon is tracked for this vault", which is the
only thing on screen that distinguishes a foreign program on the socket from a
dashboard. It moves OUT of the panel rather than away, onto the same unwrapping
writer `_print_daemon_where` uses.

AND THE COPY IS DE-DUPLICATED, not re-derived. The clause removed from the
reason is removed by EXACT-STRING identity against `daemon.vault_note(
vault_root)` -- the same function that produced it, and the same value the
projection already carries -- never by parsing the sentence for something that
looks like a path. One fact, one place on the surface (DEC-43).
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

import systemu.interface.cli_commands as cc
import systemu.scheduler.daemon as daemon_mod


_COLUMNS = "80"

#: Rich's box-drawing characters -- how a panel row is told from a plain one.
_BOX = set("\u250c\u2510\u2514\u2518\u2502\u2500")

#: The distinctive directory segment; finding it inside the panel is the defect,
#: whatever the fold happened to land on.
_SEGMENT = "example-nested-folder-1a2b3c"


def _deep_vault(tmp_path) -> str:
    """A vault root an 80-column panel MUST fold.

    SYNTHETIC parts, deliberately: this file ships inside the sdist, so a
    fixture built from a development machine's own directory names would
    publish them.
    """
    root = tmp_path
    for part in ("Workspaces", "sample_project_root", "nested_worktrees_dir",
                 _SEGMENT, "systemu", "vault"):
        root = root / part
    root.mkdir(parents=True, exist_ok=True)
    text = str(root)
    assert len(text) > 76, (
        f"fixture premise: the path must not fit an 80-column panel ({len(text)})")
    return text


#: The three verdicts, by the flags that select the renderer's branch, and the
#: exit code each is ruled to answer with.
_VERDICTS = {
    "ready": (dict(ready=True, process_alive=True), 0),
    "starting": (dict(ready=False, process_alive=True), 2),
    "not-running": (dict(ready=False, process_alive=False), 1),
}


def _status(vault_root: str, *, ready: bool, process_alive: bool) -> dict:
    """A status dict shaped like `get_status`'s projection -- with the reason
    the REAL mint builds, vault clause and all. A reason with no path in it
    cannot witness this defect."""
    provenance = daemon_mod.port_provenance_note(8765, "default")
    where = daemon_mod.vault_note(vault_root)
    if ready:
        reason = ("accepting connections on http://127.0.0.1:8765 "
                  "({}); {}".format(provenance, where))
    elif process_alive:
        reason = ("process 4242 is alive but nothing is accepting connections "
                  "on 127.0.0.1:8765 yet ({}); {}".format(provenance, where))
    else:
        reason = ("no live daemon process and nothing is accepting connections "
                  "on 127.0.0.1:8765 ({}); {}; if the daemon was started on "
                  "another port, pass --port <number>".format(provenance, where))
    return {
        "running": ready,
        "ready": ready,
        "pid": 4242,
        "process_alive": process_alive,
        "host": "127.0.0.1",
        "port": 8765,
        "url": "http://127.0.0.1:8765",
        "reason": reason,
        "port_source": "default",
        "vault_root": vault_root,
        "port_provenance": provenance,
        "daemon_version": "0.10.30",
        "daemon_path": "somewhere",
        "cli_version": "0.10.30",
        "cli_path": "somewhere",
        "build_match": True,
        "build_note": "daemon and CLI are the same build",
    }


def _run(monkeypatch, tmp_path, verdict: str):
    vault_root = _deep_vault(tmp_path)
    flags, ruled_code = _VERDICTS[verdict]
    monkeypatch.setenv("COLUMNS", _COLUMNS)
    monkeypatch.setattr(daemon_mod, "get_status",
                        lambda vault_dir, **kw: _status(vault_root, **flags))

    res = CliRunner().invoke(
        cc.daemon_status, [],
        obj={"config": SimpleNamespace(vault_dir=vault_root),
             "vault": SimpleNamespace()},
        env={"COLUMNS": _COLUMNS},
    )
    assert res.exit_code == ruled_code, res.output
    return res.output, vault_root


def _panel_body(output: str) -> str:
    """Everything inside the panel, with the box art removed and the rows
    joined -- so a path folded across rows is still found."""
    rows = [ln for ln in output.splitlines() if set(ln) & _BOX]
    return "".join(ch for ln in rows for ch in ln if ch not in _BOX)


# --------------------------------------------------------------------------- #
# the panel carries no path
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("verdict", sorted(_VERDICTS))
def test_no_part_of_the_vault_path_is_inside_the_panel(verdict, tmp_path,
                                                       monkeypatch):
    """THE REPRO, read the way the fold actually breaks it: the panel body is
    reassembled across rows, so a path split over three of them is still
    caught."""
    output, _vault = _run(monkeypatch, tmp_path, verdict)

    assert _SEGMENT not in _panel_body(output), (
        "the vault path is inside the panel, which folds it:\n" + output)


@pytest.mark.parametrize("verdict", sorted(_VERDICTS))
def test_the_vault_path_appears_exactly_once_and_on_one_line(verdict, tmp_path,
                                                             monkeypatch):
    """One fact, one place on the surface. Two copies of a path -- one of them
    broken -- is worse than one, because the broken one is read first."""
    output, vault = _run(monkeypatch, tmp_path, verdict)

    carriers = [ln for ln in output.splitlines() if vault in ln]
    assert len(carriers) == 1, (
        f"the vault path appears on {len(carriers)} lines:\n" + output)
    assert not (set(carriers[0]) & _BOX), (
        "the surviving copy is inside a panel:\n" + carriers[0])
    assert output.count(vault) == 1, (
        "the vault path is written twice:\n" + output)


# --------------------------------------------------------------------------- #
# and nothing the reason carried is lost
# --------------------------------------------------------------------------- #

def test_the_not_running_verdict_still_carries_the_port_remedy(tmp_path,
                                                               monkeypatch):
    """`pass --port <number>` is the whole answer for an operator whose daemon
    is on another port. Dropping the reason to keep the panel clean would have
    thrown it away."""
    output, _vault = _run(monkeypatch, tmp_path, "not-running")

    assert [ln for ln in output.splitlines()
            if "pass --port <number>" in ln], (
        "the remedy is gone or folded:\n" + output)


def test_the_starting_verdict_still_says_why_it_is_not_ready(tmp_path,
                                                             monkeypatch):
    output, _vault = _run(monkeypatch, tmp_path, "starting")

    assert [ln for ln in output.splitlines()
            if "nothing is accepting connections" in ln], output


@pytest.mark.parametrize("verdict", sorted(_VERDICTS))
def test_the_provenance_clause_is_still_disclosed(verdict, tmp_path,
                                                  monkeypatch):
    """N3's fact, unchanged: 8765 with no record is a GUESS this process made."""
    output, _vault = _run(monkeypatch, tmp_path, verdict)

    assert daemon_mod.port_provenance_note(8765, "default") in output, output


@pytest.mark.parametrize("verdict", sorted(_VERDICTS))
def test_the_panel_still_carries_the_verdict(verdict, tmp_path, monkeypatch):
    """It keeps what fits: the verdict word, and the PID or the URL."""
    output, _vault = _run(monkeypatch, tmp_path, verdict)
    body = _panel_body(output)

    expected = {"ready": "Ready", "starting": "Starting",
                "not-running": "Not running"}[verdict]
    assert expected in body, body
    if verdict == "ready":
        assert "http://127.0.0.1:8765" in body, body
    if verdict in ("ready", "starting"):
        assert "4242" in body, body


# --------------------------------------------------------------------------- #
# the de-duplication is identity, not parsing
# --------------------------------------------------------------------------- #

def test_the_vault_clause_is_removed_by_identity_with_the_minted_note():
    """The clause dropped is the one `vault_note` PRODUCED, matched whole. A
    sentence-parser looking for something path-shaped would be a second,
    disagreeing derivation of the same fact (DEC-43)."""
    root = r"C:\some\where\systemu\vault"
    reason = "no live daemon process ({}); {}; and a tail".format(
        "port 8765 is the built-in default", daemon_mod.vault_note(root))

    trimmed = cc._reason_outside_the_panel(reason, root)

    assert root not in trimmed, trimmed
    assert "no live daemon process" in trimmed, trimmed
    assert "and a tail" in trimmed, trimmed


def test_a_reason_that_never_named_the_vault_is_left_alone():
    """A status dict minted by an older daemon whose reason has no vault clause
    must still print in full."""
    text = "a reason line the panel may or may not print"

    assert cc._reason_outside_the_panel(text, r"C:\v") == text


def test_a_missing_reason_prints_nothing_rather_than_None():
    assert cc._reason_outside_the_panel(None, r"C:\v") == ""
    assert cc._reason_outside_the_panel("x", None) == "x"


def test_daemon_status_consumes_the_reason_printer():
    """Reachability pin: delete the production call site and a NAMED test goes
    red."""
    source = inspect.getsource(cc.daemon_status.callback)
    assert "_print_daemon_reason(" in source, source
    assert "status['reason']" not in source, (
        "the reason is still interpolated into a panel body:\n" + source)


def test_the_reason_printer_uses_the_unwrapping_writer():
    """Structural: "unwrapped" means "not printed through Rich"."""
    import ast
    import textwrap

    tree = ast.parse(textwrap.dedent(
        inspect.getsource(cc._print_daemon_reason)))
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            owner = node.func.value
            if isinstance(owner, ast.Name):
                called.add("{}.{}".format(owner.id, node.func.attr))

    assert "console.print" not in called, sorted(called)
    assert called & {"click.echo", "click.secho"}, sorted(called)
