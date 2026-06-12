"""W12-B2a/B2b (audit F5/F6) — the safety gate judges COMMANDS, not names.

F5: `is_destructive_call` marked ANY tool named run_command/shell
destructive regardless of the actual command — so a read-only `ver` was
auto-denied in every non-interactive context (which includes the daemon
the dashboard runs in). Read-only commands now execute; mutating ones
keep the gate.

F6: a headless auto-deny used to `return None` — invisible to the
stuck-counters/governor — so the model retried the same denied call until
max-iterations (30 → PARTIAL after ~90s in the A2 audit run). Denials now
flow back as FAILED tool results with an actionable message, arming the
same streak machinery that handles failures.

Also: the quick lane carried NO destructive gate at all (asymmetry hole) —
it now consults the same classifier and returns an honest denial the
model can adapt to (or ASK_USER about).
"""
from __future__ import annotations

import inspect

import pytest

from systemu.runtime.tool_sandbox import ToolSandbox, is_readonly_shell_command


class TestReadonlyShellClassifier:
    @pytest.mark.parametrize("cmd", [
        "ver",
        "dir",
        "dir C:\\Users",
        "type notes.txt",
        "systeminfo",
        "ipconfig /all",
        "whoami",
        "hostname",
        "tasklist",
        "where python",
        "ls -la",
        "cat file.txt",
        "pwd",
        "git status",
        "git log --oneline -5",
        "git diff",
        "C:\\Windows\\System32\\ipconfig.exe /all",
        "python --version",
        "pip list",
    ])
    def test_readonly_commands_pass(self, cmd):
        assert is_readonly_shell_command(cmd) is True, cmd

    @pytest.mark.parametrize("cmd", [
        "",
        "del file.txt",
        "rm -rf /",
        "format c:",
        "echo hi > out.txt",          # redirect WRITES
        "ver && del x",               # chaining hides a mutation
        "type a.txt | cmd",           # pipes chain
        "git push",
        "git commit -m x",
        "pip install requests",
        "python script.py",           # arbitrary code is not read-only
        "curl -o out.bin http://x",   # writes a file
        "shutdown /s",
        "reg add HKLM\\X",
    ])
    def test_mutating_or_ambiguous_commands_stay_gated(self, cmd):
        assert is_readonly_shell_command(cmd) is False, cmd


class TestDestructiveCallJudgesCommands:
    def test_readonly_run_command_not_destructive(self):
        assert ToolSandbox.is_destructive_call(
            "run_command", {"command": "ver"}) is False
        assert ToolSandbox.is_destructive_call(
            "run_cli_command", {"command": "git status"}) is False

    def test_mutating_run_command_still_destructive(self):
        assert ToolSandbox.is_destructive_call(
            "run_command", {"command": "del important.txt"}) is True
        assert ToolSandbox.is_destructive_call(
            "run_command", {"command": "ver && del x"}) is True

    def test_dangerous_param_patterns_always_win(self):
        assert ToolSandbox.is_destructive_call(
            "run_command", {"command": "cleanup", "flags": "--force"}) is True

    def test_non_shell_name_hints_unchanged(self):
        assert ToolSandbox.is_destructive_call("file_delete", {}) is True
        assert ToolSandbox.is_destructive_call("send_email", {}) is True
        assert ToolSandbox.is_destructive_call("file_read", {}) is False


class TestHeadlessDenialFeedsTheLoop:
    def test_headless_deny_returns_failed_result_not_none(self):
        """F6: the deny must arm the failure-streak machinery (W6.3) — a
        bare `return None` + continue left the governor blind and the model
        retrying the same denied call to max-iterations."""
        from systemu.runtime import shadow_runtime
        src = inspect.getsource(shadow_runtime)
        deny_region = src.split("AUTO-DENIED", 1)[1][:2500]
        assert "safety_gate_denied" in deny_region, \
            "the observation must say SAFETY GATE, not 'User denied' (no user exists headless)"
        assert "ToolResult(" in deny_region and "success=False" in deny_region, \
            "headless denial must return a FAILED result, not None"
        assert "read-only" in deny_region or "alternative" in deny_region, \
            "the model must be told how to adapt"


class TestQuickLaneGate:
    def test_quick_lane_consults_the_classifier(self):
        from systemu.pipelines import quick_task
        src = inspect.getsource(quick_task)
        assert "is_destructive_call" in src, \
            "the default chat lane ran shell commands UNGATED (asymmetry hole)"

    def test_denial_message_is_actionable(self):
        from systemu.pipelines.quick_task import _safety_denied
        msg = _safety_denied("run_command", {"command": "del x.txt"})
        assert msg and "del x.txt"[:3] not in ("", None)
        assert "read-only" in msg or "ASK_USER" in msg

    def test_readonly_commands_not_denied(self):
        from systemu.pipelines.quick_task import _safety_denied
        assert _safety_denied("run_command", {"command": "ver"}) is None
        assert _safety_denied("file_read", {"path": "x"}) is None

    def test_mutating_file_tools_pass_quick_lane(self):
        """Gate-3-enabled write tools are the lane's PURPOSE — only the
        destructive classifier's verdicts get denied, and write_text_file
        isn't one (it has no destructive name hint)."""
        from systemu.pipelines.quick_task import _safety_denied
        assert _safety_denied("write_text_file",
                              {"file_path": "x", "content": "y"}) is None
