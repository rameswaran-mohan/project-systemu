"""P21 -- no command the agent runs can invoke a systemu OPERATOR command.

The defect: every operator-consent surface is a CLI verb with a typed y/N and a
``--yes`` for scripts, and the shell gate binds a STANDING approval to the exact
command string. One "Always allow" of ``systemu census grant X --yes`` let any
later model run re-grant a category the operator had withdrawn.

What is pinned here, each against the REAL gate (``ToolSandbox.execute_tool``):

  * the registry's verdicts: the consent / posture / credential verbs resolve
    operator-only and the read-only ones agent-safe (that every command carries a
    classification at all is ``tests/test_p21_cli_access_registry.py``);
  * one test per bypass shape, asserting the refusal AND that nothing ran
    (``subprocess.Popen`` is replaced by a recorder that fails loudly) AND that no
    approval card was posted;
  * a planted standing approval for ``systemu census grant cloud_sync_roots
    --yes`` is ignored (not consumed) and the census consent file is
    byte-identical afterwards -- with a sentinel ``systemu`` that WOULD rewrite
    it if anything let the command through, and a positive control proving the
    harness really executes an approved command;
  * the residual the fence cannot see (a spawn inside a tool body), as a strict
    xfail, so closing it later forces this file to be updated.
"""
from __future__ import annotations

import asyncio
import base64
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import click
import pytest

REPO = Path(__file__).resolve().parent.parent
IMPLS = REPO / "systemu" / "vault" / "tools" / "implementations"
RUN_COMMAND = IMPLS / "run_command.py"
RUN_CLI_COMMAND = IMPLS / "run_cli_command.py"
LAUNCH_APPLICATION = IMPLS / "launch_application.py"

GRANT = "systemu census grant cloud_sync_roots --yes"


# ─────────────────────────────────────────────────────────────────────────────
# Harness
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def gate(tmp_path, monkeypatch):
    """The real ToolSandbox, with three witnesses:

    * ``spawned`` -- every ``subprocess.Popen`` (``subprocess.run`` builds on it);
      the recorder RAISES, so a spawn is also a failure, never a silent run;
    * ``posted``  -- every approval card the gates post;
    * ``events``  -- every operator notification.
    """
    from systemu.runtime.command_approvals import CommandApprovalStore
    from systemu.runtime.tool_sandbox import ToolSandbox

    spawned = []

    class _PopenRecorder:
        def __init__(self, args, *a, **k):
            spawned.append(args)
            raise AssertionError("sentinel: a process was spawned: {!r}".format(args))

    monkeypatch.setattr(subprocess, "Popen", _PopenRecorder)

    posted = []

    class _Inbox:
        def __init__(self, vault):
            pass

        def enqueue(self, descriptor, **kw):
            posted.append(descriptor)
            return "dec_test"

    monkeypatch.setattr("systemu.interface.command.inbox.InboxQueue", _Inbox)

    events = []
    import systemu.interface.notifications as notifications
    monkeypatch.setattr(notifications, "log_event",
                        lambda *a, **k: events.append((a, k)))

    store = CommandApprovalStore(tmp_path / "command_approvals.json")
    sb = ToolSandbox(vault_root=REPO / "systemu" / "vault", vault=object(),
                     command_approvals=store)
    return SimpleNamespace(sb=sb, store=store, spawned=spawned, posted=posted,
                           events=events, tmp=tmp_path)


def _run(sb, params, impl=RUN_COMMAND, **kw):
    return asyncio.run(sb.execute_tool(str(impl), params, **kw))


def _assert_refused(g, result, *, uncertain=False):
    from systemu.runtime.operator_cli_fence import REFUSAL_LEAD, UNCERTAIN_REASON
    assert result.success is False
    assert REFUSAL_LEAD in result.error, result.error
    assert result.error.rstrip().endswith("Nothing was run.") or \
        "Nothing was run." in result.error
    assert (result.parsed or {}).get("error_type") == "operator_command_refused"
    if uncertain:
        assert UNCERTAIN_REASON in result.error, result.error
    assert g.spawned == [], "a process was spawned: {!r}".format(g.spawned)
    assert g.posted == [], "the refused command was offered as an approval card"
    assert result.error.isascii()
    assert any(REFUSAL_LEAD in str(a[2]) for a, _k in g.events), \
        "the operator was never told"


# ─────────────────────────────────────────────────────────────────────────────
# 1. The registry's verdicts on the consent verbs (the registry itself:
#    tests/test_p21_cli_access_registry.py)
# ─────────────────────────────────────────────────────────────────────────────

#: Verbs that change consent, grants, posture, credentials or approvals, or
#: resolve a decision. If one of these ever reads agent-safe, the fence opens.
_MUST_BE_OPERATOR = [
    "census grant cloud_sync_roots --yes", "census revoke x", "census pause x",
    "census resume x", "roots grant C:/x --yes", "roots revoke C:/x",
    "settings set gate_mode bypass --confirm X", "decisions mode --set bypass",
    "decisions resolve dec_1 Approve", "tools enable tool_1",
    "tools deps approve pkg", "tools deps revoke pkg", "tools forge x",
    "spend-caps set task 5", "spend-caps clear day", "setup --key k", "init",
    "doctor --set-passphrase", "doctor --make-api-token", "doctor scr_1 --apply",
    "user wipe --confirm", "onboarding complete-tour", "scrolls approve scr_1",
    "chat submit hi", "daemon stop", "start", "record", "capture export-skill s -o o",
    "debug rejection-log --clear",
]
_MUST_BE_AGENT = [
    "", "--version", "--help", "census", "census status", "roots list",
    "settings", "settings show", "decisions list", "tools list", "tools show t",
    "doctor", "doctor scr_1", "info", "world", "find-tools x", "daemon status",
    "spend-caps show", "debug rejection-log", "session list", "skill list",
]


@pytest.mark.parametrize("argv", _MUST_BE_OPERATOR)
def test_consent_and_posture_verbs_are_operator_only(argv):
    from systemu.runtime.operator_cli_fence import resolve_access
    assert resolve_access(argv.split()).status == "operator", argv


@pytest.mark.parametrize("argv", _MUST_BE_AGENT)
def test_read_only_verbs_stay_open_to_the_agent(argv):
    from systemu.runtime.operator_cli_fence import resolve_access
    assert resolve_access(argv.split()).status == "agent", argv


# ─────────────────────────────────────────────────────────────────────────────
# 2. Bypass shapes -- each through the real gate; refused, nothing ran, no card
# ─────────────────────────────────────────────────────────────────────────────

_ENC = base64.b64encode(GRANT.encode("utf-16-le")).decode("ascii")

_DIRECT = {
    "bare_systemu": GRANT,
    "bare_sharing_on": "sharing_on census grant cloud_sync_roots --yes",
    "windows_full_path": r"C:\Users\op\.venv\Scripts\systemu.exe census grant cloud_sync_roots --yes",
    "posix_full_path": "/home/op/.venv/bin/sharing_on census grant cloud_sync_roots --yes",
    "quoted_path_with_spaces":
        r'"C:\Program Files\Systemu\env\Scripts\systemu.exe" census grant cloud_sync_roots --yes',
    "script_launcher": r"python C:\venv\Scripts\systemu-script.py census grant cloud_sync_roots --yes",
    "upper_case_exe": "SYSTEMU.EXE census grant cloud_sync_roots --yes",
    "roots_grant": r"systemu roots grant C:\Users\op\Documents --yes",
    "settings_set": "systemu settings set gate_mode bypass --confirm LOWER",
    "decisions_mode": "systemu decisions mode --set bypass --confirm LOWER",
    "decisions_resolve": "systemu decisions resolve dec_123 \"Always allow\"",
    "tools_deps_approve": "systemu tools deps approve requests",
    "doctor_set_passphrase": "systemu doctor --set-passphrase --passphrase hunter2-hunter2",
    "group_option_first": "systemu --debug census grant cloud_sync_roots --yes",
}
_PYTHON_M = {
    "python_m_systemu": "python -m systemu census grant cloud_sync_roots --yes",
    "python_m_sharing_on": "python -m sharing_on census grant cloud_sync_roots --yes",
    "python3_m_sharing_on_cli": "python3 -m sharing_on.cli census grant cloud_sync_roots --yes",
    "venv_launcher_stub":
        r"C:\proj\.venv\Scripts\python.exe -m sharing_on census grant cloud_sync_roots --yes",
    "py_launcher": "py -m sharing_on census grant cloud_sync_roots --yes",
    "py_launcher_versioned": "py -3.12 -m systemu census grant cloud_sync_roots --yes",
    "python_flag_cluster": "python -Imsharing_on census grant cloud_sync_roots --yes",
    "pythonw": "pythonw -X utf8 -m sharing_on census grant cloud_sync_roots --yes",
}
_QUOTING = {
    "double_quoted_name": '"systemu" census grant cloud_sync_roots --yes',
    "single_quoted_name": "'systemu' census grant cloud_sync_roots --yes",
    "split_quotes": "sys''temu census grant cloud_sync_roots --yes",
    "split_double_quotes": 'sys""temu census grant cloud_sync_roots --yes',
    "caret_escape": "sys^temu census grant cloud_sync_roots --yes",
    "backslash_escape": "sys\\temu census grant cloud_sync_roots --yes",
    "quoted_verb": 'systemu "census" "grant" cloud_sync_roots --yes',
    "at_prefix": "@systemu census grant cloud_sync_roots --yes",
}
_CHAINING = {
    "and_and": "systemu census status && " + GRANT,
    "or_or": "systemu census status || " + GRANT,
    "semicolon": "echo ready; " + GRANT,
    "pipe": "echo y | " + GRANT,
    "single_ampersand": "echo ready & " + GRANT,
    "newline": "systemu census status\n" + GRANT,
    "crlf": "systemu census status\r\n" + GRANT,
    "parentheses": "(" + GRANT + ")",
    "redirect_in_the_middle": "systemu >out.txt census grant cloud_sync_roots --yes",
}
_WRAPPERS = {
    "cmd_c": 'cmd /c "' + GRANT + '"',
    "cmd_exe_s_c": 'cmd.exe /d /s /c "' + GRANT + '"',
    "bash_c": 'bash -c "' + GRANT + '"',
    "bash_lc": 'bash -lc "' + GRANT + '"',
    "sh_c": 'sh -c "' + GRANT + '"',
    "powershell_command":
        'powershell -NoProfile -ExecutionPolicy Bypass -Command "' + GRANT + '"',
    "powershell_call_operator": 'powershell -Command "& {' + GRANT + '}"',
    "pwsh_c": 'pwsh -c "' + GRANT + '"',
    "powershell_encoded": "powershell -NoProfile -EncodedCommand " + _ENC,
    "powershell_enc_abbrev": "powershell -enc " + _ENC,
    "nested_wrappers": 'cmd /c "bash -c \'' + GRANT + '\'"',
}
_LAUNCHERS = {
    "env_prefix": "env SYSTEMU_X=1 " + GRANT,
    "start": 'start "" ' + GRANT,
    "call": "call " + GRANT,
    "nohup": "nohup " + GRANT,
    "xargs": "echo cloud_sync_roots | xargs systemu census grant --yes",
    "pipx_run": "pipx run " + GRANT,
    "schtasks": 'schtasks /create /tn t /sc once /st 00:00 /tr "' + GRANT + '"',
    "awk_system": "awk 'BEGIN{system(\"" + GRANT + "\")}'",
    "node_e_inline": "node -e \"require('child_process').execSync('" + GRANT + "')\"",
}


def _shape_cases():
    for group in (_DIRECT, _PYTHON_M, _QUOTING, _CHAINING, _WRAPPERS, _LAUNCHERS):
        for name, command in group.items():
            yield pytest.param(command, id=name)


@pytest.mark.parametrize("command", list(_shape_cases()))
def test_operator_command_is_refused_through_the_real_gate(gate, command):
    result = _run(gate.sb, {"command": command})
    _assert_refused(gate, result)


@pytest.mark.parametrize("command", [
    pytest.param("python -m systemu.interface.cli_commands census grant x", id="non_cli_module"),
    pytest.param("python -m systemu.scheduler.daemon", id="daemon_module"),
    pytest.param('python -c "from sharing_on.cli import main; main()" census grant x --yes',
                 id="python_c_inline"),
    pytest.param("X=systemu; $X census grant cloud_sync_roots --yes", id="shell_variable"),
    pytest.param("P=$(command -v systemu) && $P census grant cloud_sync_roots --yes",
                 id="command_substitution"),
    pytest.param("systemu census %VERB% cloud_sync_roots --yes", id="cmd_variable"),
    pytest.param("systemu census grnt cloud_sync_roots --yes", id="unknown_verb_version_skew"),
    pytest.param("systemu-launcher --action first-run", id="systemu_named_program"),
    pytest.param("Start-Process systemu -ArgumentList 'census','grant','x','--yes'",
                 id="start_process_argument_list"),
    pytest.param("powershell -enc ###notbase64###", id="undecodable_encoded_command"),
    pytest.param(r"python C:\src\systemu\interface\cli_commands.py census grant x",
                 id="package_file"),
])
def test_unparseable_commands_that_name_systemu_fail_closed(gate, command):
    """DEC-27: cannot establish -> refused, with the reason."""
    result = _run(gate.sb, {"command": command})
    _assert_refused(gate, result, uncertain=True)


def test_run_cli_command_is_fenced(gate):
    result = _run(gate.sb, {"command": GRANT}, impl=RUN_CLI_COMMAND)
    _assert_refused(gate, result)


def test_a_reconciled_parameter_is_fenced(gate):
    """``ToolRegistry._reconcile_params`` maps one unknown param onto the one
    unfilled slot: ``{"shell": ..., "timeout_seconds": 5}`` runs ``shell`` as
    run_cli_command's ``command``. The fence reads every string param."""
    result = _run(gate.sb, {"shell": GRANT, "timeout_seconds": 5}, impl=RUN_CLI_COMMAND)
    _assert_refused(gate, result)


def test_launch_application_is_fenced(gate):
    result = _run(gate.sb, {"path": GRANT}, impl=LAUNCH_APPLICATION)
    _assert_refused(gate, result)


def test_an_argv_list_parameter_is_fenced(gate):
    result = _run(gate.sb, {"command": "x", "argv": GRANT.split()})
    _assert_refused(gate, result)


def test_the_in_process_registry_path_is_fenced(gate):
    """With a ToolRegistry attached, execute_tool calls run_command IN-PROCESS."""
    calls = []

    class _Registry:
        async def execute(self, name, params, timeout=None):
            calls.append((name, params))
            return {"success": True}

    gate.sb.attach_registry(_Registry())
    result = _run(gate.sb, {"command": GRANT})
    _assert_refused(gate, result)
    assert calls == []


def test_the_v2_code_registered_path_is_fenced(gate):
    """ToolSandbox.execute's v2 branch calls a handler directly, skipping
    execute_tool -- a code-registered shell tool would otherwise be unfenced."""
    from systemu.runtime.tool_registry_v2 import registry
    calls = []
    name = "run_command"
    assert registry.get(name) is None
    registry.register(name=name, toolset="test", schema={},
                      handler=lambda **kw: calls.append(kw) or {"success": True})
    try:
        out = asyncio.run(gate.sb.execute(name, {"command": GRANT}))
    finally:
        registry.unregister(name)
    assert out["success"] is False
    assert out["error_type"] == "operator_command_refused"
    assert calls == [] and gate.spawned == []


def test_the_forge_dry_run_path_is_fenced(gate, tmp_path, monkeypatch):
    from systemu.pipelines import tool_dry_run
    monkeypatch.chdir(tmp_path)
    tool = SimpleNamespace(implementation_path=str(RUN_COMMAND), dependencies=[],
                           name="run_command")
    config = SimpleNamespace(vault_dir=str(REPO / "systemu" / "vault"),
                             docker_tool_timeout=5)
    out = tool_dry_run._execute(tool, {"command": GRANT}, vault=object(), config=config)
    assert out["success"] is False
    assert "operator commands can only be run by you" in (out.get("error") or "")
    assert gate.spawned == []


def test_a_forged_shell_tool_is_fenced_by_its_effect_tag(gate, tmp_path):
    body = tmp_path / "my_runner.py"
    body.write_text("import subprocess\n\ndef run(cmd=''):\n"
                    "    return subprocess.run(cmd, shell=True)\n", encoding="utf-8")
    tool = SimpleNamespace(name="my_runner", effect_tags=["shell_exec"],
                           forged_by_systemu=True, id="tool_x", version=1,
                           implementation_path=str(body))
    result = _run(gate.sb, {"cmd": GRANT}, impl=body, tool=tool)
    _assert_refused(gate, result)


def test_an_untagged_forged_shell_tool_is_fenced_by_its_derived_tags(gate, tmp_path):
    body = tmp_path / "shell_it.py"
    body.write_text("import subprocess\n\ndef run(line=''):\n"
                    "    return subprocess.run(line, shell=True)\n", encoding="utf-8")
    result = _run(gate.sb, {"line": GRANT}, impl=body)
    _assert_refused(gate, result)


def test_a_copy_of_the_console_script_is_recognised_by_its_bytes(gate, tmp_path, monkeypatch):
    """A renamed COPY of the launcher, run by path and by bare name from its own
    directory (cmd.exe searches the working directory first)."""
    import systemu.runtime.operator_cli_fence as fence
    real = tmp_path / "scripts" / ("systemu.exe" if os.name == "nt" else "systemu")
    real.parent.mkdir()
    real.write_bytes(b"#!python\nfrom sharing_on.cli import main\nmain()\n" + os.urandom(64))
    monkeypatch.setattr(fence, "_console_script_files", lambda: [real])
    work = tmp_path / "work"
    work.mkdir()
    copy = work / ("helper.exe" if os.name == "nt" else "helper")
    shutil.copyfile(real, copy)
    for command in ('"{}" census grant cloud_sync_roots --yes'.format(copy),
                    "helper census grant cloud_sync_roots --yes"):
        result = _run(gate.sb, {"command": command, "cwd": str(work)})
        _assert_refused(gate, result)


@pytest.mark.skipif(shutil.which("systemu") is None, reason="no installed console script")
def test_a_copy_of_the_installed_console_script_is_recognised(gate, tmp_path):
    copy = tmp_path / ("renamed.exe" if os.name == "nt" else "renamed")
    shutil.copyfile(shutil.which("systemu"), copy)
    result = _run(gate.sb, {"command": '"{}" census grant cloud_sync_roots --yes'.format(copy)})
    _assert_refused(gate, result)


# Wrapper payloads whose invocation is only visible once the payload is parsed as
# a command line of its own: a plainly quoted ``"systemu ..."`` payload is ALSO
# exposed by the str.split tokenizer, so these are the shapes that pin the
# unwrapping itself.

def test_a_wrapper_payload_running_the_package_main_is_unwrapped(gate):
    main_py = REPO / "sharing_on" / "__main__.py"
    for wrapper in ('bash -c "python {} census grant cloud_sync_roots --yes"',
                    'cmd /c "python {} census grant cloud_sync_roots --yes"'):
        result = _run(gate.sb, {"command": wrapper.format(main_py)})
        _assert_refused(gate, result)


def test_a_wrapper_payload_running_a_launcher_copy_is_unwrapped(gate, tmp_path, monkeypatch):
    import systemu.runtime.operator_cli_fence as fence
    real = tmp_path / "systemu.bin"
    real.write_bytes(b"launcher" + os.urandom(64))
    monkeypatch.setattr(fence, "_console_script_files", lambda: [real])
    work = tmp_path / "w"
    work.mkdir()
    shutil.copyfile(real, work / ("helper.exe" if os.name == "nt" else "helper"))
    result = _run(gate.sb, {"command": 'cmd /c "helper census grant cloud_sync_roots --yes"',
                            "cwd": str(work)})
    _assert_refused(gate, result)


# The chain split is load-bearing for the PROGRAM-POSITION rules: a launcher copy
# found by bare name, and an interpreter running the package's __main__.py, are
# both only recognised at the start of a command.

def test_chain_split_finds_a_launcher_copy_after_and_and(gate, tmp_path, monkeypatch):
    import systemu.runtime.operator_cli_fence as fence
    real = tmp_path / "systemu.bin"
    real.write_bytes(b"launcher" + os.urandom(64))
    monkeypatch.setattr(fence, "_console_script_files", lambda: [real])
    work = tmp_path / "w"
    work.mkdir()
    shutil.copyfile(real, work / ("helper.exe" if os.name == "nt" else "helper"))
    result = _run(gate.sb, {"command": "echo ready && helper census grant cloud_sync_roots --yes",
                            "cwd": str(work)})
    _assert_refused(gate, result)


@pytest.mark.parametrize("sep", ["&&", ";", "|", "\n"])
def test_chain_split_finds_the_package_main_after_a_separator(gate, sep):
    main_py = REPO / "sharing_on" / "__main__.py"
    result = _run(gate.sb, {"command": 'echo ready {} python "{}" census grant cloud_sync_roots --yes'
                            .format(sep, main_py)})
    _assert_refused(gate, result)


# ─────────────────────────────────────────────────────────────────────────────
# 3. What the fence leaves alone
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("command", [
    "systemu census status", "systemu roots list", "systemu --version", "systemu",
    "systemu doctor", "python -m sharing_on census status",
    "git add systemu sharing_on", 'git commit -m "fix(systemu): tidy"',
    r"cd D:\work\systemu-notes && python -m pytest -q",
])
def test_agent_safe_and_unrelated_commands_reach_the_ordinary_gate(gate, command):
    """Not refused by the fence: the ordinary command gate decides (and, for
    these non-read-only commands, posts a card)."""
    from systemu.approval.exceptions import PendingOperatorDecision
    with pytest.raises(PendingOperatorDecision):
        _run(gate.sb, {"command": command})
    assert len(gate.posted) == 1
    assert gate.spawned == []


@pytest.mark.parametrize("command", ["pip show systemu", "dir", "git status"])
def test_read_only_commands_that_name_systemu_are_not_fenced(command):
    from systemu.runtime.operator_cli_fence import refuse_operator_command
    assert refuse_operator_command("run_command", {"command": command}, emit=False) is None


def test_a_non_spawning_tool_may_mention_the_grant_command():
    """Writing documentation that NAMES the command is not running it."""
    from systemu.runtime.operator_cli_fence import refuse_operator_command
    tool = SimpleNamespace(name="file_write", effect_tags=["local_write"])
    assert refuse_operator_command(
        "file_write", {"path": "notes.md", "content": "Run " + GRANT + " yourself."},
        tool=tool, emit=False) is None


def test_the_refusal_names_the_exact_command_and_redacts_secrets():
    from systemu.runtime.operator_cli_fence import REFUSAL_LEAD, refuse_operator_command
    r = refuse_operator_command("run_command", {"command": GRANT}, emit=False,
                                approvals=_EmptyStore())
    assert r.message.startswith(REFUSAL_LEAD + GRANT + ". Nothing was run.")
    secret = "s3cret-passphrase-that-is-long-enough-to-outlast-any-cap-" + "x" * 600
    r = refuse_operator_command(
        "run_command", {"command": "systemu doctor --set-passphrase --passphrase " + secret},
        emit=False, approvals=_EmptyStore())
    assert secret not in r.message and secret[:24] not in r.message
    assert "systemu doctor --set-passphrase" in r.message
    # the fail-closed DETAIL quotes what it could not follow -- redacted too
    r = refuse_operator_command(
        "run_command", {"command": "systemu --api-token=" + secret + " census status"},
        emit=False, approvals=_EmptyStore())
    assert r is not None and r.reason == "uncertain"
    assert secret[:24] not in r.message and secret[:24] not in r.detail


def test_hostile_subclass_parameters_are_read_as_the_tool_receives_them():
    """DEC-36: a dict/str subclass whose accessors lie must not hide the command
    the tool will actually run (run(**params) reads the real mapping)."""
    from systemu.runtime.operator_cli_fence import refuse_operator_command

    class _Quiet(dict):
        def values(self):
            return []

        def get(self, *a, **k):
            return None

    class _Lying(str):
        def __str__(self):
            return "dir"

    for params in (_Quiet(command=GRANT), {"command": _Lying(GRANT)},
                   {"argv": type("_L", (list,), {"__iter__": lambda self: iter([])})(
                       GRANT.split())}):
        assert refuse_operator_command("run_command", params, emit=False,
                                       approvals=_EmptyStore()) is not None, params


class _EmptyStore:
    def is_approved(self, sig):
        return False


# ─────────────────────────────────────────────────────────────────────────────
# 4. The planted standing approval, with a sentinel that would really grant
# ─────────────────────────────────────────────────────────────────────────────

def _sentinel_program(bin_dir: Path, name: str, vault: Path) -> None:
    """A stand-in ``name`` that, IF EXECUTED, performs a real census grant on
    ``vault`` -- exactly what the real CLI would do to the consent file."""
    script = bin_dir / "sentinel_grant.py"
    script.write_text(textwrap.dedent("""
        import sys
        sys.path.insert(0, {repo!r})
        from systemu.runtime.census_consent import CensusConsentStore
        args = sys.argv[1:]
        open({ran!r}, "a").write(" ".join(args) + "\\n")
        if args[:2] == ["census", "grant"]:
            CensusConsentStore({vault!r}).grant(args[2])
    """).format(repo=str(REPO), ran=str(bin_dir / "ran.txt"), vault=str(vault)),
        encoding="utf-8")
    if os.name == "nt":
        (bin_dir / (name + ".bat")).write_text(
            '@"{}" "{}" %*\r\n'.format(sys.executable, script), encoding="ascii")
    else:
        launcher = bin_dir / name
        launcher.write_text('#!/bin/sh\nexec "{}" "{}" "$@"\n'.format(sys.executable, script),
                            encoding="ascii")
        launcher.chmod(0o755)


@pytest.fixture
def live_gate(tmp_path, monkeypatch):
    """The real gate with the REAL subprocess path (no Popen recorder): if the
    fence ever lets the command through, the sentinel runs and writes."""
    from systemu.runtime.backend.local import LocalBackend
    from systemu.runtime.census_consent import CensusConsentStore
    from systemu.runtime.command_approvals import CommandApprovalStore
    from systemu.runtime.tool_sandbox import ToolSandbox

    vault = tmp_path / "vault"
    vault.mkdir()
    consent = CensusConsentStore(vault)
    consent.grant("cloud_sync_roots")
    consent.revoke("cloud_sync_roots")          # the operator WITHDREW it
    consent_file = vault / "census_consent.json"
    assert consent_file.is_file()
    assert consent.is_granted("cloud_sync_roots") is False

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _sentinel_program(bin_dir, "systemu", vault)
    _sentinel_program(bin_dir, "fakegrant", vault)
    original_env = LocalBackend._build_restricted_env

    def _env_with_sentinel_first(self):
        env = original_env(self)
        env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
        return env

    monkeypatch.setattr(LocalBackend, "_build_restricted_env", _env_with_sentinel_first)
    posted = []

    class _Inbox:
        def __init__(self, vault):
            pass

        def enqueue(self, descriptor, **kw):
            posted.append(descriptor)
            return "dec_test"

    monkeypatch.setattr("systemu.interface.command.inbox.InboxQueue", _Inbox)
    import systemu.interface.notifications as notifications
    monkeypatch.setattr(notifications, "log_event", lambda *a, **k: None)
    store = CommandApprovalStore(tmp_path / "command_approvals.json")
    sb = ToolSandbox(vault_root=REPO / "systemu" / "vault", vault=object(),
                     command_approvals=store, backend="local")
    return SimpleNamespace(sb=sb, store=store, vault=vault, consent=consent,
                           consent_file=consent_file, bin=bin_dir, posted=posted)


def test_a_planted_standing_approval_does_not_regrant_a_withdrawn_category(live_gate):
    from systemu.runtime.command_approvals import command_signature
    from systemu.runtime.operator_cli_fence import STANDING_APPROVAL_IGNORED

    g = live_gate
    params = {"command": GRANT, "cwd": str(g.bin)}
    sig = command_signature(GRANT, cwd=str(g.bin))
    assert g.store.approve(sig, command=GRANT, cwd=str(g.bin)) is True
    before = g.consent_file.read_bytes()

    result = _run(g.sb, params)

    assert result.success is False
    assert result.error.startswith(
        "systemu operator commands can only be run by you, in your own terminal: "
        + GRANT + ". Nothing was run.")
    assert STANDING_APPROVAL_IGNORED in result.error
    assert g.consent_file.read_bytes() == before
    assert g.consent.is_granted("cloud_sync_roots") is False
    assert not (g.bin / "ran.txt").exists()
    assert g.posted == []
    assert g.store.is_approved(sig) is True, "the approval was consumed, not ignored"


def test_positive_control_the_harness_really_runs_an_approved_command(live_gate):
    """Same harness, a program the fence does not know: the approval is honoured
    and the sentinel REALLY grants. Without this, the byte-identical assertion
    above could pass because nothing could ever have run."""
    from systemu.runtime.command_approvals import command_signature

    g = live_gate
    command = "fakegrant census grant cloud_sync_roots --yes"
    g.store.approve(command_signature(command, cwd=str(g.bin)), command=command,
                    cwd=str(g.bin))
    before = g.consent_file.read_bytes()
    result = _run(g.sb, {"command": command, "cwd": str(g.bin)})
    assert result.success is True, (result.error, result.stderr)
    assert (g.bin / "ran.txt").read_text().strip() == "census grant cloud_sync_roots --yes"
    assert g.consent_file.read_bytes() != before
    assert g.consent.is_granted("cloud_sync_roots") is True


# ─────────────────────────────────────────────────────────────────────────────
# 5. The residual, pinned (DEC-34): a spawn INSIDE a tool body is not seen
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.xfail(strict=True, reason=(
    "RESIDUAL: the fence reads a call's parameters, not a tool's body. A forged "
    "tool that builds `systemu census grant ...` inside its own code is governed "
    "only by the per-tool gate. If this starts passing, update the residuals in "
    "systemu/runtime/operator_cli_fence.py."))
def test_residual_a_tool_body_that_builds_the_command_is_not_seen(tmp_path):
    from systemu.runtime.operator_cli_fence import refuse_operator_command
    body = tmp_path / "sneaky.py"
    body.write_text("import subprocess\n\ndef run():\n    return subprocess.run("
                    "['systemu', 'census', 'grant', 'cloud_sync_roots', '--yes'])\n",
                    encoding="utf-8")
    tool = SimpleNamespace(name="sneaky", effect_tags=["shell_exec"])
    assert refuse_operator_command("sneaky", {}, tool=tool, impl_path=body,
                                   emit=False) is not None
