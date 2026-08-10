"""`systemu start` -- the one-command golden path, and the witness that gates it.

WHAT THIS COMMAND IS
    `systemu start` runs the EXISTING `systemu daemon start` path and, when that
    path minted a real readiness witness, opens the operator's browser at the
    dashboard URL. It is a thin caller: the provider gate, the interactive setup
    fallback, the vault-root refusal (DEC-32) and the DEC-41 readiness witness
    are the same code, reached through `ctx.invoke(daemon_start, ...)`, so the
    two commands cannot drift apart or disagree on an exit code.

PROPERTY (DEC-41, in UX form)
    Opening a browser is a CLAIM that something is serving at that URL. So the
    ONLY thing that may open it is a verdict whose `ready` bit is the literal
    `True` minted by `probe_readiness`. No verdict, a not-ready verdict, a
    vault-root REFUSAL, or a truthy stand-in that is not a bool all read as
    "not witnessed" and open nothing -- and the nonzero exit is propagated
    unchanged, because a browser is not a substitute for a diagnosis.

PROPERTY (best effort, never fatal)
    A browser that will not open is a NOTE. The daemon is up, the URL was
    printed, and turning that into a nonzero exit would fail a working install
    for a cosmetic reason.

NO REAL DAEMON IS SPAWNED HERE. The decision is a pure function of the verdict
and two flags; the wiring is pinned against the source (GTM reachability rule:
delete the production call site and a NAMED test below goes red).
"""
from __future__ import annotations

import ast
import inspect
import textwrap
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

import systemu.scheduler.daemon as daemon_mod
from systemu.interface import cli_commands


# -- helpers ------------------------------------------------------------------

def _verdict(*, ready: bool, refused: bool = False, port: int = 8765):
    return daemon_mod.DaemonReadiness(
        ready=ready,
        pid=4242,
        process_alive=True,
        host="127.0.0.1",
        port=port,
        reason=("accepting connections" if ready else "nothing is accepting"),
        refused=refused,
    )


def _source_names(fn) -> set:
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}


# =============================================================================
#  The pure decision: readiness verdict (+ the two suppressors) -> should_open
# =============================================================================

def test_a_witnessed_ready_daemon_is_the_one_case_that_opens_a_browser():
    from systemu.interface.cli_commands import should_open_browser
    assert should_open_browser(_verdict(ready=True),
                               no_browser=False, interactive=True) is True


def test_an_unready_daemon_never_opens_a_browser():
    """DEC-41: not started != success. A browser on an unready daemon would be
    the same false assertion as exit 0 on one."""
    from systemu.interface.cli_commands import should_open_browser
    assert should_open_browser(_verdict(ready=False),
                               no_browser=False, interactive=True) is False


def test_the_vault_root_refusal_never_opens_a_browser():
    """DEC-32: a refusal spawned nothing. There is no URL to show."""
    from systemu.interface.cli_commands import should_open_browser
    assert should_open_browser(_verdict(ready=False, refused=True),
                               no_browser=False, interactive=True) is False
    # even a verdict that somehow carries both bits stays closed on the refusal
    assert should_open_browser(_verdict(ready=True, refused=True),
                               no_browser=False, interactive=True) is False


def test_absent_verdict_is_not_witnessed():
    from systemu.interface.cli_commands import should_open_browser
    assert should_open_browser(None, no_browser=False, interactive=True) is False


def test_no_browser_flag_suppresses_the_open_on_a_ready_daemon():
    from systemu.interface.cli_commands import should_open_browser
    assert should_open_browser(_verdict(ready=True),
                               no_browser=True, interactive=True) is False


def test_a_non_tty_session_behaves_like_daemon_start_does_today():
    """`daemon start` opens no browser; with no TTY neither does `start`.

    Not a style choice: on a DISPLAY-less box `webbrowser` can fall through to
    a console browser it runs with `p.wait()`, which would hang a CI job on a
    daemon that is genuinely up.
    """
    from systemu.interface.cli_commands import should_open_browser
    assert should_open_browser(_verdict(ready=True),
                               no_browser=False, interactive=False) is False


@pytest.mark.parametrize("bogus", [1, "yes", object()])
def test_a_truthy_stand_in_for_the_ready_bit_is_not_a_witness(bogus):
    """DEC-36: the concrete type is pinned in the deciding frame. Only the bool
    `True` the mint produces counts -- a duck-typed stand-in does not."""
    from systemu.interface.cli_commands import should_open_browser
    fake = SimpleNamespace(ready=bogus, refused=False, url="http://127.0.0.1:8765")
    assert should_open_browser(fake, no_browser=False, interactive=True) is False


def test_the_decision_is_pure_and_touches_no_browser():
    """It may not open anything, print anything, or read the environment -- it
    only answers the question. Checked on the BODY: the docstring names
    `webbrowser` to explain why the TTY suppressor exists."""
    from systemu.interface.cli_commands import should_open_browser
    fn = ast.parse(textwrap.dedent(inspect.getsource(should_open_browser))).body[0]
    body = fn.body[1:] if ast.get_docstring(fn) else fn.body
    code = "\n".join(ast.unparse(stmt) for stmt in body)
    for impurity in ("webbrowser", "console", "isatty", "environ", "getenv"):
        assert impurity not in code, code


# =============================================================================
#  Best effort: a browser that will not open is a note, never an error
# =============================================================================

def test_a_failing_browser_open_is_reported_and_never_raises(monkeypatch):
    from systemu.interface.cli_commands import _open_dashboard

    def _boom(url, *a, **kw):
        raise RuntimeError("no browser on this box")

    monkeypatch.setattr("webbrowser.open", _boom)
    assert _open_dashboard("http://127.0.0.1:8765") is False


def test_a_browser_open_that_returns_false_is_reported_and_never_raises(monkeypatch):
    from systemu.interface.cli_commands import _open_dashboard
    monkeypatch.setattr("webbrowser.open", lambda url, *a, **kw: False)
    assert _open_dashboard("http://127.0.0.1:8765") is False


def test_a_successful_browser_open_reports_true(monkeypatch):
    from systemu.interface.cli_commands import _open_dashboard
    seen = []
    monkeypatch.setattr("webbrowser.open", lambda url, *a, **kw: seen.append(url) or True)
    assert _open_dashboard("http://127.0.0.1:8765") is True
    assert seen == ["http://127.0.0.1:8765"]


# =============================================================================
#  The command, end to end -- with no daemon anywhere near it
# =============================================================================

@pytest.fixture()
def wired(monkeypatch, tmp_path):
    """Every gate `daemon start` consults, satisfied; `start_daemon` stubbed."""
    import sharing_on.setup_flow as _sf
    import systemu.runtime.optional_deps as _od

    monkeypatch.setattr(_sf, "key_present", lambda: True)
    monkeypatch.setattr(_od, "missing_groups", lambda pkgs: ())
    monkeypatch.setattr(cli_commands, "_is_interactive", lambda: True)

    calls = {"start_daemon": [], "opened": []}
    monkeypatch.setattr("webbrowser.open",
                        lambda url, *a, **kw: calls["opened"].append(url) or True)

    def _install(verdict):
        def _fake_start(*a, **kw):
            calls["start_daemon"].append(kw)
            return verdict
        monkeypatch.setattr(daemon_mod, "start_daemon", _fake_start)

    vault = tmp_path / "vault"
    vault.mkdir()
    calls["install"] = _install
    calls["obj"] = {"config": SimpleNamespace(vault_dir=str(vault)),
                    "vault": SimpleNamespace()}
    return calls


def test_start_opens_the_dashboard_once_the_daemon_is_witnessed_ready(wired):
    from systemu.interface.cli_commands import start_cmd
    wired["install"](_verdict(ready=True, port=8765))

    res = CliRunner().invoke(start_cmd, [], obj=dict(wired["obj"]))

    assert res.exit_code == 0, res.output
    assert wired["opened"] == ["http://127.0.0.1:8765"], res.output


def test_start_opens_nothing_and_keeps_the_exit_code_when_not_ready(wired):
    from systemu.interface.cli_commands import start_cmd
    wired["install"](_verdict(ready=False, port=8765))

    res = CliRunner().invoke(start_cmd, [], obj=dict(wired["obj"]))

    assert res.exit_code == 1, res.output
    assert wired["opened"] == [], res.output


def test_start_propagates_the_vault_root_refusal_exit_code_unchanged(wired):
    from systemu.interface.cli_commands import start_cmd
    wired["install"](_verdict(ready=False, refused=True, port=8765))

    res = CliRunner().invoke(start_cmd, [], obj=dict(wired["obj"]))

    assert res.exit_code == daemon_mod.VAULT_ROOT_REFUSED_EXIT, res.output
    assert wired["opened"] == [], res.output


def test_no_browser_starts_the_daemon_and_opens_nothing(wired):
    from systemu.interface.cli_commands import start_cmd
    wired["install"](_verdict(ready=True, port=8765))

    res = CliRunner().invoke(start_cmd, ["--no-browser"], obj=dict(wired["obj"]))

    assert res.exit_code == 0, res.output
    assert wired["opened"] == [], res.output
    assert len(wired["start_daemon"]) == 1, "the daemon must still be started"


def test_port_reaches_the_daemon_and_the_browser(wired):
    from systemu.interface.cli_commands import start_cmd
    wired["install"](_verdict(ready=True, port=9911))

    res = CliRunner().invoke(start_cmd, ["--port", "9911"], obj=dict(wired["obj"]))

    assert res.exit_code == 0, res.output
    assert wired["start_daemon"][0]["port"] == 9911, wired["start_daemon"]
    assert wired["opened"] == ["http://127.0.0.1:9911"], res.output


def test_start_refuses_exactly_where_daemon_start_refuses(monkeypatch, tmp_path):
    """The provider gate is NOT reimplemented: with no usable provider and no
    TTY, `start` must abort nonzero and spawn nothing -- same as `daemon start`.
    """
    import sharing_on.setup_flow as _sf
    import systemu.runtime.optional_deps as _od
    from systemu.interface.cli_commands import start_cmd

    monkeypatch.setattr(_od, "missing_groups", lambda pkgs: ())
    monkeypatch.setattr(_sf, "key_present", lambda: False)
    monkeypatch.setattr(daemon_mod, "start_daemon",
                        lambda *a, **kw: pytest.fail("must not spawn without a provider"))
    monkeypatch.setattr("webbrowser.open",
                        lambda *a, **kw: pytest.fail("must not open a browser"))

    vault = tmp_path / "vault"
    vault.mkdir()
    res = CliRunner().invoke(
        start_cmd, [],
        obj={"config": SimpleNamespace(vault_dir=str(vault)), "vault": SimpleNamespace()},
    )
    assert res.exit_code != 0, res.output


# =============================================================================
#  Reachability pins -- delete a call site and one of these goes red
# =============================================================================

def test_start_is_registered_on_the_real_top_level_cli():
    """The whole feature IS the registration. A perfect command nobody can
    type is the half-built failure mode this pin exists to catch."""
    from sharing_on.cli import cli
    from systemu.interface.cli_commands import start_cmd
    assert "start" in cli.commands, sorted(cli.commands)
    assert cli.commands["start"] is start_cmd


def test_start_delegates_to_daemon_start_and_reimplements_nothing():
    from systemu.interface.cli_commands import start_cmd
    tree = ast.parse(textwrap.dedent(inspect.getsource(start_cmd.callback)))
    invoked = [
        node.args[0].id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "invoke"
        and node.args
        and isinstance(node.args[0], ast.Name)
    ]
    assert invoked == ["daemon_start"], invoked

    src = inspect.getsource(start_cmd.callback)
    for reimplementation in ("start_daemon(", "key_present", "probe_readiness",
                             "missing_groups", "run_setup"):
        assert reimplementation not in src, (
            f"`start` reimplements {reimplementation!r} instead of reusing "
            "`daemon start` -- the two will drift")


def test_the_only_path_to_the_browser_runs_through_the_gate():
    """Pinned STRUCTURALLY, and here is why that is not belt-and-braces theatre.

    `daemon start` already exits nonzero on every non-ready path, so at RUNTIME
    the earlier exit is usually what stops the open. That hides the gate: delete
    it and every runtime test in this file would still pass. So the gate is
    pinned on the shape of the code instead -- every `_open_dashboard` call must
    sit under an `if` that consults `should_open_browser`.
    """
    from systemu.interface.cli_commands import start_cmd
    tree = ast.parse(textwrap.dedent(inspect.getsource(start_cmd.callback)))
    opens = {
        id(n) for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        and n.func.id == "_open_dashboard"
    }
    assert opens, "`start` never opens the dashboard at all"

    guarded = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and "should_open_browser(" in ast.unparse(node.test):
            for stmt in node.body:
                for n in ast.walk(stmt):
                    if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                            and n.func.id == "_open_dashboard"):
                        guarded.add(id(n))
    assert opens == guarded, (
        "an `_open_dashboard` call is reachable without the readiness gate")


def test_start_gates_the_browser_on_the_pure_decision():
    from systemu.interface.cli_commands import start_cmd
    names = _source_names(start_cmd.callback)
    assert {"should_open_browser", "_open_dashboard", "_is_interactive"} <= names, \
        sorted(names)


def test_the_browser_is_opened_by_webbrowser_open():
    from systemu.interface.cli_commands import _open_dashboard
    assert "webbrowser.open(" in inspect.getsource(_open_dashboard)


def test_daemon_start_still_hands_back_the_verdict_start_gates_on():
    """`start` can only be thin because `daemon start` RETURNS its minted
    verdict. Drop that return and `start` is blind."""
    from systemu.interface.cli_commands import daemon_start
    src = inspect.getsource(daemon_start.callback)
    assert "return verdict" in src, src


def test_start_help_is_ascii_and_names_the_dashboard():
    from systemu.interface.cli_commands import start_cmd
    res = CliRunner().invoke(start_cmd, ["--help"])
    assert res.exit_code == 0, res.output
    assert res.output.isascii(), res.output
    assert "--no-browser" in res.output
    assert "--port" in res.output
