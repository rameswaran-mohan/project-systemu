"""DOGFOOD-28 D1 (fix 5) -- every "the daemon is up" surface names WHICH Python.

THE NIGHT THIS EXISTS FOR
    An operator on Windows 11, fresh venv, saw two live processes carrying
    identical daemon argv: one under the venv interpreter, one under the base
    interpreter. Nothing anywhere -- not `daemon start`, not `daemon status`,
    not the log -- said which interpreter the daemon was actually running
    under, so the only reading available was "two daemons racing for my port".

    It was one daemon. On Windows `python -m venv` installs
    `Scripts/python.exe` as a launcher that CreateProcess-es the base
    interpreter with the SAME argv and waits on it in a job object. The two
    rows are a launcher and the process it launched.

    The fact that would have ended that night in one line is: which interpreter
    is the daemon running under, and is it the one you are typing at?

PROPERTY
    The interpreter rides the SAME mint as readiness and the build (DEC-43 --
    one mint per operator-facing fact, no parallel derivation):
      * the daemon RECORDS its own `sys.executable` -- only the process that is
        running may say what it is running under, exactly as with the build;
      * `probe_readiness` compares it with the interpreter THIS process is
        using and mints a TRI-STATE verdict; UNVERIFIED is its own state and
        never reads as agreement (DEC-27);
      * `daemon start` prints the interpreter on its ready line, and
        `daemon status` says so when the recorded one differs from the
        invoking one.
    When the daemon runs out of a virtual environment, the disclosure also
    names the base interpreter and says that a process list can show it as a
    second row with the same arguments -- one daemon, not two.

WITNESS
    Delete the disclosure from either CLI surface, let UNVERIFIED collapse into
    agreement, or let the parent record an interpreter on the child's behalf,
    and a named test here goes red.
"""
from __future__ import annotations

import ast
import inspect
import json
import os
import socket
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

import systemu.scheduler.daemon as daemon_mod


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture()
def vault_dir(tmp_path) -> str:
    v = tmp_path / "home" / "vault"
    v.mkdir(parents=True)
    return str(v)


def _up(vault_dir: str, port: int, *, interpreter) -> None:
    """Make this process stand in for a tracked, live daemon."""
    daemon_mod._pid_file_path(vault_dir).write_text(str(os.getpid()))
    daemon_mod._write_runtime_state(vault_dir, pid=os.getpid(), port=port,
                                    build=daemon_mod._own_build(),
                                    interpreter=interpreter)


def _other_interpreter() -> dict:
    return {"executable": r"C:\Program Files\OtherPython\python.exe",
            "base": r"C:\Program Files\OtherPython\python.exe",
            "venv": False}


# ═════════════════════════════════════════════════════════════════════════════
#  the fact itself
# ═════════════════════════════════════════════════════════════════════════════

def test_own_interpreter_reports_this_process_and_nothing_else():
    rec = daemon_mod._own_interpreter()
    assert type(rec) is dict
    assert rec["executable"] == sys.executable
    assert type(rec["base"]) is str and rec["base"]
    assert type(rec["venv"]) is bool
    assert rec["venv"] is (sys.prefix != sys.base_prefix), (
        "whether this process runs from a virtual environment is the fact that "
        "explains the second row in the operator's process list")


def test_the_recorded_interpreter_survives_a_round_trip(tmp_path):
    path = tmp_path / "state.json"
    daemon_mod._write_runtime_state_at(path, pid=4242, port=1234,
                                       interpreter=daemon_mod._own_interpreter())
    rec = json.loads(path.read_text(encoding="utf-8"))
    assert rec["interpreter"]["executable"] == sys.executable


def test_an_odd_shaped_record_is_UNVERIFIED_not_a_crash(tmp_path):
    for junk in (None, "python.exe", 7, [], {"executable": 3}, {"base": "x"}):
        assert daemon_mod._recorded_interpreter({"interpreter": junk}) is None, (
            f"a malformed interpreter record ({junk!r}) must read as "
            "UNVERIFIED, never as a path")


# ═════════════════════════════════════════════════════════════════════════════
#  the mint carries it (DEC-43: no second derivation)
# ═════════════════════════════════════════════════════════════════════════════

def test_the_mint_reports_agreement_when_the_daemon_recorded_this_interpreter(vault_dir):
    port = _free_port()
    _up(vault_dir, port, interpreter=daemon_mod._own_interpreter())
    v = daemon_mod.probe_readiness(vault_dir, port=port, timeout=0.1)
    assert v.interpreter_match is True
    assert v.daemon_interpreter == sys.executable
    assert v.cli_interpreter == sys.executable
    assert sys.executable in v.interpreter_note


def test_the_mint_reports_a_SKEW_when_the_daemon_runs_another_python(vault_dir):
    port = _free_port()
    _up(vault_dir, port, interpreter=_other_interpreter())
    v = daemon_mod.probe_readiness(vault_dir, port=port, timeout=0.1)
    assert v.interpreter_match is False
    assert "INTERPRETER SKEW" in v.interpreter_note
    assert _other_interpreter()["executable"] in v.interpreter_note
    assert sys.executable in v.interpreter_note, (
        "a skew that names only one side leaves the operator with one path and "
        "no comparison")


def test_a_daemon_that_recorded_NO_interpreter_is_UNVERIFIED_and_never_agrees(vault_dir):
    port = _free_port()
    _up(vault_dir, port, interpreter=None)
    v = daemon_mod.probe_readiness(vault_dir, port=port, timeout=0.1)
    assert v.interpreter_match is None, (
        "an unrecorded interpreter is a DIFFERENT state from agreement -- it "
        "is an older daemon, which is the skew itself (DEC-27)")
    assert "UNVERIFIED" in v.interpreter_note


def test_no_daemon_at_all_makes_no_interpreter_claim(vault_dir):
    v = daemon_mod.probe_readiness(vault_dir, port=_free_port(), timeout=0.1)
    assert v.interpreter_match is None
    assert v.interpreter_note == "", (
        "a stopped daemon has no interpreter to disagree about; a note here "
        "would be noise on every `daemon status`")


def test_a_venv_daemon_explains_the_second_row_in_the_process_list(vault_dir):
    """THE LINE THAT ENDS THE NIGHT."""
    port = _free_port()
    _up(vault_dir, port, interpreter={
        "executable": r"C:\work\.venv\Scripts\python.exe",
        "base": r"C:\Python312\python.exe",
        "venv": True})
    v = daemon_mod.probe_readiness(vault_dir, port=port, timeout=0.1)
    note = v.interpreter_note
    assert r"C:\Python312\python.exe" in note, (
        "the base interpreter is the executable the operator SEES on the second "
        "row; a note that omits it does not explain the row")
    assert "one daemon" in note, (
        "the note must say in words that the pair is one daemon, not two")


def test_get_status_projects_the_interpreter_fact(vault_dir):
    port = _free_port()
    _up(vault_dir, port, interpreter=_other_interpreter())
    st = daemon_mod.get_status(vault_dir, port=port, timeout=0.1)
    assert st["interpreter_match"] is False
    assert st["daemon_interpreter"] == _other_interpreter()["executable"]
    assert st["cli_interpreter"] == sys.executable
    assert st["interpreter_note"]


def test_every_verdict_string_is_ascii(vault_dir):
    """DEC-32c: a verdict a cp1252 console cannot encode is not a verdict."""
    port = _free_port()
    for interp in (daemon_mod._own_interpreter(), _other_interpreter(), None,
                   {"executable": r"C:\v\Scripts\python.exe",
                    "base": r"C:\Python312\python.exe", "venv": True}):
        _up(vault_dir, port, interpreter=interp)
        note = daemon_mod.probe_readiness(vault_dir, port=port,
                                          timeout=0.1).interpreter_note
        assert note.isascii(), f"non-ASCII interpreter verdict: {note!r}"


# ═════════════════════════════════════════════════════════════════════════════
#  only the daemon may record what the daemon is running under
# ═════════════════════════════════════════════════════════════════════════════

def test_the_parent_never_stamps_its_own_interpreter_onto_the_childs_record():
    """The parent knows the interpreter it NAMED, not the one the child ended
    up executing -- which on Windows is routinely not the same file. A
    parent-written record would manufacture agreement for the very skew this
    reports."""
    tree = ast.parse(Path(daemon_mod.__file__).read_text(encoding="utf-8-sig"))
    offenders, forwarders = [], []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if type(name) is not str or not name.startswith("_write_runtime_state"):
                continue
            for kw in node.keywords:
                if kw.arg != "interpreter":
                    continue
                if isinstance(kw.value, ast.Constant) and kw.value.value is None:
                    continue
                if (isinstance(kw.value, ast.Name) and kw.value.id == "interpreter"
                        and any(a.arg == "interpreter"
                                for a in (fn.args.kwonlyargs + fn.args.args))):
                    forwarders.append(fn.name)
                    continue
                offenders.append(fn.name)
    assert offenders == ["_run_daemon_loop"], (
        "the interpreter may be recorded ONLY by the daemon process that is "
        f"running under it (_run_daemon_loop); found writers: {offenders}")
    assert forwarders == ["_write_runtime_state"], (
        f"unexpected pass-through writer(s): {forwarders}")


def test_the_daemon_loop_records_the_interpreter_it_runs_under():
    src = inspect.getsource(daemon_mod._run_daemon_loop)
    assert "_own_interpreter()" in src, (
        "the daemon loop no longer records which Python it runs under -- every "
        "surface downstream silently degrades to UNVERIFIED")


# ═════════════════════════════════════════════════════════════════════════════
#  the two CLI surfaces
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture()
def cli_env(monkeypatch, vault_dir):
    """Point the CLI at this vault without touching the worktree's own."""
    monkeypatch.setenv("SYSTEMU_VAULT_DIR", vault_dir)
    return vault_dir


def _run_status(vault_dir, port):
    from systemu.interface.cli_commands import daemon_status
    return CliRunner().invoke(daemon_status, ["--port", str(port)],
                              obj={}, catch_exceptions=False)


def test_daemon_status_names_the_interpreter_when_the_two_AGREE(cli_env):
    port = _free_port()
    _up(cli_env, port, interpreter=daemon_mod._own_interpreter())
    res = _run_status(cli_env, port)
    assert sys.executable in res.output, (
        "`daemon status` never says which Python the daemon runs under, so an "
        f"operator cannot tell a venv from a base install:\n{res.output}")


def test_daemon_status_says_LOUDLY_when_the_interpreters_DIFFER(cli_env):
    port = _free_port()
    _up(cli_env, port, interpreter=_other_interpreter())
    res = _run_status(cli_env, port)
    assert "INTERPRETER SKEW" in res.output, (
        f"a daemon running under another Python is not disclosed:\n{res.output}")
    assert _other_interpreter()["executable"] in res.output


def test_daemon_status_discloses_an_UNVERIFIED_interpreter(cli_env):
    port = _free_port()
    _up(cli_env, port, interpreter=None)
    res = _run_status(cli_env, port)
    assert "UNVERIFIED" in res.output and "interpreter" in res.output.lower(), (
        f"an unrecorded interpreter is reported as nothing at all:\n{res.output}")


def test_daemon_start_ready_line_names_the_interpreter(monkeypatch, cli_env):
    """The ready line is the one an operator reads at the exact moment the two
    processes appear."""
    from systemu.interface import cli_commands as cc

    port = _free_port()
    _up(cli_env, port, interpreter=daemon_mod._own_interpreter())
    minted = daemon_mod.probe_readiness(cli_env, port=port, timeout=0.1)
    ready = daemon_mod._restate(minted, ready=True, process_alive=True)

    monkeypatch.setattr(cc, "_get_vault_and_config",
                        lambda ctx: (type("C", (), {"vault_dir": cli_env})(), None))
    monkeypatch.setattr("systemu.runtime.optional_deps.missing_groups",
                        lambda groups: ())
    monkeypatch.setattr("sharing_on.setup_flow.key_present", lambda: True)
    monkeypatch.setattr("systemu.scheduler.daemon.start_daemon",
                        lambda *a, **kw: ready)

    res = CliRunner().invoke(cc.daemon_start, ["--port", str(port)], obj={},
                             catch_exceptions=False)
    assert "OK Daemon ready." in res.output, res.output
    assert sys.executable in res.output, (
        "the ready line does not name the interpreter the daemon runs under:\n"
        + res.output)


def test_both_cli_surfaces_read_the_interpreter_off_the_MINTED_value():
    """DEC-43 -- no surface may derive this a second way. Reachability pin:
    delete either consumption and this goes red."""
    from systemu.interface import cli_commands as cc

    for fn in (cc.daemon_start, cc.daemon_status):
        src = inspect.getsource(fn.callback)
        assert "interpreter_note" in src, (
            f"{fn.name} no longer consumes the minted interpreter note")
    printer = inspect.getsource(cc._print_daemon_interpreter)
    assert "interpreter" in printer


# ═════════════════════════════════════════════════════════════════════════════
#  D2 applies here too: this line carries ABSOLUTE PATHS
# ═════════════════════════════════════════════════════════════════════════════
#
#  `_print_daemon_interpreter` is the sibling of `_print_daemon_build`, and the
#  build line's own regression (v0.10.29) was that Rich folded it at the
#  console width, delivering an absolute path in four fragments -- a path that
#  can be neither pasted nor searched for. The interpreter line names TWO
#  installations, and naming them is only useful if the operator can go and
#  look at them.
#
#  The printer's docstring already states the rule. DEC-34: a false assertion
#  of enforcement is itself a defect, so the rule is pinned rather than
#  asserted.

#: The width the build-line regression was witnessed at.
_COLUMNS = "80"

#: A synthetic interpreter path past 150 characters -- long enough that no
#: plausible terminal width can carry it AND the sentence around it, which is
#: the only way to tell an unwrapped line from a luckily short one.
_LONG_EXE = (r"C:\opt\systemu-runtime\environments\production-3"
             r"\very-long-directory-segment-for-the-regression"
             r"\another-long-directory-segment\.venv\Scripts\python.exe")


def test_the_interpreter_printer_uses_the_unwrapping_writer():
    """Structural, not cosmetic: `console.print` folds at the console width by
    design, so "unwrapped" means "not printed through Rich"."""
    import textwrap

    from systemu.interface import cli_commands as cc

    tree = ast.parse(textwrap.dedent(
        inspect.getsource(cc._print_daemon_interpreter)))
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            owner = node.func.value
            if isinstance(owner, ast.Name):
                called.add("{}.{}".format(owner.id, node.func.attr))

    assert "console.print" not in called, (
        "the interpreter line is printed through Rich, which folds it at the "
        "console width and breaks the two paths it exists to name: "
        + repr(sorted(called)))
    assert called & {"click.echo", "click.secho"}, (
        "the interpreter line is not printed with a writer that leaves it "
        "unwrapped: " + repr(sorted(called)))


def test_the_interpreter_line_carries_the_whole_path_on_one_line(monkeypatch,
                                                                 cli_env):
    """THE build line's regression, applied to its sibling, at 80 columns."""
    port = _free_port()
    _up(cli_env, port, interpreter={"executable": _LONG_EXE,
                                    "base": _LONG_EXE, "venv": False})
    monkeypatch.setenv("COLUMNS", _COLUMNS)
    from systemu.interface.cli_commands import daemon_status

    res = CliRunner().invoke(daemon_status, ["--port", str(port)], obj={},
                             env={"COLUMNS": _COLUMNS},
                             catch_exceptions=False)
    assert _LONG_EXE in res.output, (
        "the interpreter path is not in the output at all:\n" + res.output)
    assert [ln for ln in res.output.splitlines() if _LONG_EXE in ln], (
        "the interpreter line folded the path across lines -- it can be "
        "neither pasted nor searched for:\n" + res.output)
