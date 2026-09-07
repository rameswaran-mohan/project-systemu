"""P1 -- a FAILED `daemon start` verdict is ASCII and never wraps a path.

THE WITNESS (0.10.30 dogfood, 80-column console)
------------------------------------------------
`daemon start --wait 1` against a port nothing was serving printed:

    ERROR Daemon did not become ready.
      timed out after 1s <U+2014> no live daemon process and nothing is
    accepting connections on 127.0.0.1:8765 (port 8765 is the built-in
    default); vault: C:\\Users\\...\\example-nested-folder\\systemu\\va
    ult; if the daemon was started on another port, pass --port <number>
      Log: C:\\Users\\...\\example-nested-folder\\systemu\\vault\\daemon
    .log

TWO DEFECTS ON ONE SCREEN, both already ruled elsewhere in this program:

  * DEC-32c -- verdict-carrying output is ASCII-only. The em dash the mint
    joined the timeout clause with is not encodable on a cp1252 or cp437
    console, and a console that cannot encode a verdict does not print a
    plainer one: it raises UnicodeEncodeError where the answer should have
    been. This is the failure path, so the traceback would land exactly where
    the operator most needs the sentence.

  * D2 / N7 -- a path wrapped at column 80 is not a path. It can be neither
    pasted nor searched for, and BOTH paths here are things the operator is
    being sent to look at: the vault the verdict is about, and the log that
    says why. The `roots` group, `_print_daemon_where`, `_print_daemon_build`
    and `_print_daemon_interpreter` all already carry this ruling; the failure
    branch of `daemon start` was still going out through `console.print`, which
    folds a paragraph at the console width wherever the break lands.

THE PROPERTY
------------
    The failed-readiness verdict body is ASCII, and the vault path and the log
    path each arrive on ONE line at 80 columns.

NO REAL DAEMON, NO REAL PORT. `start_daemon` is replaced with a call to the
REAL `await_readiness` -- so the reason under test is genuinely minted, not
hand-written -- and the connection witness itself is faked to "nothing is
accepting", which is the state being reported. Nothing is spawned and no socket
is opened, so no assumption is made about any port being free.
"""
from __future__ import annotations

import ast
import inspect
import textwrap
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

import systemu.interface.cli_commands as cc
import systemu.scheduler.daemon as daemon_mod


#: The width the wrap was witnessed at.
_COLUMNS = "80"

#: A port number that is never contacted: the connection witness below is
#: faked, so this is only ever text in a sentence. Nothing is bound and nothing
#: is assumed about what may be listening on this machine.
_UNSERVED_PORT = 8901


def _deep_vault(tmp_path) -> str:
    """A vault root long enough that an 80-column Rich paragraph MUST fold it.

    SYNTHETIC parts, deliberately: this file ships inside the sdist, so a
    fixture built from a development machine's own directory names would
    publish them. What the shape has to preserve is what the property depends
    on -- nested parts, long enough that 80 columns cannot carry the path and
    the sentence around it.
    """
    root = tmp_path
    for part in ("Workspaces", "sample_project_root", "nested_worktrees_dir",
                 "example-nested-folder-1a2b3c", "systemu", "vault"):
        root = root / part
    root.mkdir(parents=True, exist_ok=True)
    text = str(root)
    assert len(text) > 76, (
        f"fixture premise: the path must not fit 80 columns ({len(text)})")
    return text


@pytest.fixture()
def gates_open(monkeypatch):
    """The two refusals `daemon start` makes BEFORE it spawns anything.

    Patched at their own modules, not replaced with a stand-in for the command
    under test: the dashboard extra and a usable provider are real gates and
    this test is about neither.
    """
    monkeypatch.setattr("systemu.runtime.optional_deps.missing_groups",
                        lambda groups: ())
    monkeypatch.setattr("sharing_on.setup_flow.key_present", lambda: True)


@pytest.fixture()
def failed_start(monkeypatch, tmp_path, gates_open):
    """Run `daemon start --wait 1` at 80 columns and get (output, vault).

    The verdict is MINTED by the real `await_readiness`, so the reason string
    under test is the one production builds, em dash and all.
    """
    vault = _deep_vault(tmp_path)
    monkeypatch.setattr(daemon_mod, "_connection_succeeds",
                        lambda host, port, timeout: False)

    def _no_spawn(**kw):
        return daemon_mod.await_readiness(
            kw["vault_dir"], port=kw.get("port"),
            timeout_s=kw.get("wait_timeout_s") or 1.0,
            poll_s=0.01, probe_timeout=0.01)

    monkeypatch.setattr(daemon_mod, "start_daemon", _no_spawn)
    monkeypatch.setenv("COLUMNS", _COLUMNS)

    res = CliRunner().invoke(
        cc.daemon_start, ["--wait", "1", "--port", str(_UNSERVED_PORT)],
        obj={"config": SimpleNamespace(vault_dir=vault),
             "vault": SimpleNamespace()},
        env={"COLUMNS": _COLUMNS},
    )
    assert res.exit_code == 1, res.output      # DEC-41: not ready != success
    return res.output, vault


# --------------------------------------------------------------------------- #
# DEC-32c -- the body is ASCII
# --------------------------------------------------------------------------- #

def test_the_minted_timeout_reason_is_ascii(monkeypatch, tmp_path):
    """At the MINT, not only at the surface: every consumer of this verdict --
    `daemon start`, `systemu start`, a log line -- inherits the encoding."""
    monkeypatch.setattr(daemon_mod, "_connection_succeeds",
                        lambda host, port, timeout: False)
    verdict = daemon_mod.await_readiness(
        str(tmp_path), port=_UNSERVED_PORT, timeout_s=0.0,
        poll_s=0.01, probe_timeout=0.01)

    assert not verdict.ready, verdict
    assert verdict.reason.startswith("timed out after"), verdict.reason
    verdict.reason.encode("ascii")


def test_the_whole_failed_start_output_is_ascii(failed_start):
    """The screen a cp1252 console would have raised on instead of printing."""
    output, _vault = failed_start
    output.encode("ascii")


def test_the_verdict_line_still_says_ERROR(failed_start):
    """D5's ASCII prefixes are unchanged by the rewriting done here."""
    output, _vault = failed_start
    line = [ln for ln in output.splitlines()
            if "Daemon did not become ready." in ln]
    assert line, output
    assert line[0].strip().startswith("ERROR"), line[0]


# --------------------------------------------------------------------------- #
# N7 -- one line per path, at 80 columns
# --------------------------------------------------------------------------- #

def test_the_vault_path_in_the_verdict_body_is_on_one_line(failed_start):
    """The verdict names the vault it is about. Folded, it names nothing the
    operator can act on."""
    output, vault = failed_start
    assert vault in output, (
        "the failed verdict folded the vault path across lines -- it can be "
        "neither pasted nor searched for:\n" + output)


def test_the_log_path_is_on_one_line(failed_start):
    """`Log: <path>` is the one instruction on this screen. A path broken at
    column 80 sends the operator to a file that does not exist."""
    output, vault = failed_start
    carrier = [ln for ln in output.splitlines() if ln.strip().startswith("Log:")]
    assert carrier, "no Log: line at all:\n" + output
    assert carrier[0].strip().endswith("daemon.log"), (
        "the log path is folded away from its own line:\n" + output)
    assert vault in carrier[0], carrier[0]


def test_the_remedy_command_is_not_broken_across_lines(failed_start):
    """A command split by a wrap cannot be copied, and the half that survives
    a copy runs and does nothing (the F29/D9 ruling, same screen)."""
    output, _vault = failed_start
    assert [ln for ln in output.splitlines() if "systemu daemon stop" in ln], (
        "the remedy command was folded:\n" + output)


# --------------------------------------------------------------------------- #
# structural: "unwrapped" means "not printed through Rich"
# --------------------------------------------------------------------------- #

def test_the_failure_printer_uses_the_unwrapping_writer():
    """`console.print` folds at the console width BY DESIGN, so the property
    cannot be held by wording -- only by which writer the branch uses."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(cc._print_start_failure)))
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            owner = node.func.value
            if isinstance(owner, ast.Name):
                called.add("{}.{}".format(owner.id, node.func.attr))

    assert "console.print" not in called, (
        "the failed-start verdict is printed through Rich, which folds the two "
        "paths it exists to name: " + repr(sorted(called)))
    assert called & {"click.echo", "click.secho"}, (
        "the failed-start verdict is not printed with a writer that leaves it "
        "unwrapped: " + repr(sorted(called)))


def test_daemon_start_consumes_the_failure_printer():
    """Reachability pin (the standing rule): delete the production call site
    and a NAMED test goes red."""
    source = inspect.getsource(cc.daemon_start.callback)
    assert "_print_start_failure(" in source, (
        "`daemon start` no longer routes its failure branch through the "
        "unwrapping printer")
