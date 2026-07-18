"""The ``shell_exec`` action-gate ratchet and its shell-tool carve-out.

COUPLED with the ``classify_source`` import-alias fix. That fix made the
classifier CORRECT — ``import subprocess as sp`` now tags ``shell_exec`` instead
of scanning empty — which, on its own, made the gate LESS protective: an empty
tag set scores UNKNOWN ⇒ REQUIRE_APPROVAL (gated), while ``{shell_exec}`` used
to score ALLOW (ungated), because ``shell_exec`` sat in
``action_governance._LOCAL_TAGS`` and was ABSENT from ``_APPROVAL_TAGS``. Adding
``shell_exec`` to ``_APPROVAL_TAGS`` closes that; this file pins the LIVE
consequence at ``ToolSandbox._maybe_gate_tool``.

The ratchet alone would have re-opened a bug a past audit already paid to fix
(W12 / audit-F5: a provably read-only ``ver`` auto-denied). ``run_command`` and
``run_cli_command`` are ALREADY governed — one call earlier, by
``_maybe_gate_command``, which keys on ``command_signature(command, cwd)`` and is
therefore params-DEPENDENT. That strictly dominates the tool gate's
params-INDEPENDENT ``tool_signature``. ``_command_gate_already_scored`` delegates
to it, and these tests pin BOTH halves: the delegation happens, and it cannot be
borrowed by a tool the command gate did not actually score.

Every test drives the LIVE ``execute_tool`` → ``_maybe_gate_tool`` path. Each
asserts its PRECONDITIONS, so a pass can never come from the command gate
carding first, from the forged-network hard-DENY firing, or from the scorer
being lenient.
"""
from __future__ import annotations

import asyncio

import pytest

from systemu.approval.exceptions import PendingOperatorDecision
from systemu.core.models import Tool, ToolType
from systemu.runtime.command_approvals import CommandApprovalStore


# A body that tolerates any params (the sandbox forwards the call's parameters).
_BENIGN_BODY = "def run(**kwargs):\n    return {'success': True}\n"


def _sandbox(tmp_path):
    """vault_root = tmp_path/'vault' so vault_root.parent == tmp_path — the impl
    files below live under a per-test tmp_path, never a shared parent."""
    from systemu.runtime.tool_sandbox import ToolSandbox
    store = CommandApprovalStore(tmp_path / "command_approvals.json")
    sb = ToolSandbox(str(tmp_path / "vault"), vault=object(), command_approvals=store)
    return sb, store


def _make_tool(tmp_path, *, filename, name, effect_tags, forged):
    """``filename`` and ``name`` are separate on purpose.

    ``execute_tool`` derives ``tool_name`` from the impl FILENAME
    (``impl_path.stem``), while the ``Tool`` model carries its own ``name``.
    ``_command_gate_already_scored`` checks BOTH, so the tests need to drive them
    apart.
    """
    impl_dir = tmp_path / "impls"
    impl_dir.mkdir(parents=True, exist_ok=True)
    impl_file = impl_dir / f"{filename}.py"
    impl_file.write_text(_BENIGN_BODY, encoding="utf-8")
    tool = Tool(
        id=f"tool_{filename}_{name}",
        name=name,
        description="shell-gate test tool",
        tool_type=ToolType.PYTHON_FUNCTION,
        implementation_path=f"impls/{filename}.py",
        effect_tags=list(effect_tags),
        forged_by_systemu=forged,
        version=1,
    )
    return tool


class _RecordingInbox:
    """Captures every gate card the run posts, so a test can prove WHICH gate
    fired rather than merely that something did."""

    posted: list = []

    def __init__(self, vault):
        pass

    def enqueue(self, descriptor, *, gate_type, **kw):
        _RecordingInbox.posted.append((gate_type, getattr(descriptor, "dedup", "")))
        return f"dec_{len(_RecordingInbox.posted)}"


@pytest.fixture
def inbox(monkeypatch):
    _RecordingInbox.posted = []
    monkeypatch.setattr("systemu.interface.command.inbox.InboxQueue", _RecordingInbox)
    return _RecordingInbox


def _assert_scorer_would_card(effect_tags, tool_name="run_command"):
    """PRECONDITION shared by every test here: the action-governance ladder DOES
    card this tag set. Without it, a "no card raised" result could just mean the
    scorer never wanted one, and the carve-out would be proven by nothing."""
    from systemu.runtime.action_governance import (
        ActionContext, Verdict, evaluate_action)
    verdict, _ = evaluate_action(ActionContext(
        tool=tool_name, effect_tags=set(effect_tags)))
    assert verdict == Verdict.REQUIRE_APPROVAL, (
        f"premise broken: {sorted(effect_tags)} no longer cards at the scorer, "
        "so this test proves nothing about the carve-out")


def _assert_command_gate_would_not_card(command):
    """PRECONDITION: the command gate lets this command through as provably
    read-only. If it were destructive, ``_maybe_gate_command`` would raise FIRST
    and a "card raised" assertion below would pass for the wrong reason."""
    from systemu.runtime.tool_sandbox import ToolSandbox
    assert ToolSandbox.is_destructive_call("run_command", {"command": command}) is False, (
        f"premise broken: {command!r} is classified destructive, so the COMMAND "
        "gate — not the tool gate — governs this call")


# ── (i) the W12 / audit-F5 regression guard ──────────────────────────────────

def test_readonly_command_on_a_nonforged_shell_tool_runs_ungated(tmp_path, inbox):
    """A provably read-only command through a real shell tool must NOT card.

    This is verbatim the bug a past audit paid to fix (a read-only ``ver``
    auto-denied). The command gate already scored this exact command and found it
    read-only; re-carding it here on a params-INDEPENDENT signature would be
    friction with no security gain.
    """
    _assert_scorer_would_card({"shell_exec"})
    _assert_command_gate_would_not_card("git status")

    sb, _store = _sandbox(tmp_path)
    tool = _make_tool(tmp_path, filename="run_command", name="run_command",
                      effect_tags=["shell_exec"], forged=False)

    # No raise: the call proceeds past both gates. (The body then runs; whether
    # the subprocess succeeds is irrelevant — this test is about the GATE.)
    asyncio.run(sb.execute_tool(tool.implementation_path,
                                {"command": "git status"}, tool=tool))

    assert inbox.posted == [], (
        f"a provably read-only shell command was carded: {inbox.posted}")


# ── (ii) the carve-out is FORGE-PROOF ────────────────────────────────────────

def test_forged_tool_calling_itself_run_command_is_not_exempt(tmp_path, inbox):
    """THE attack a name-only exemption would open.

    A forge picks its own ``name`` and its own impl filename, so both are
    attacker-controlled. Without clause (a) a forged tool could call itself
    ``run_command``, present a benign ``{"command": "dir"}`` to sail past the
    command gate, then run whatever its BODY actually does — the command gate
    scores the ``command`` STRING, not the body. ``forged_by_systemu`` is
    system-set (``systemu/core/models.py:386``) and is the only forge-proof key
    available here.
    """
    _assert_scorer_would_card({"shell_exec"})
    _assert_command_gate_would_not_card("dir")

    sb, _store = _sandbox(tmp_path)
    tool = _make_tool(tmp_path, filename="run_command", name="run_command",
                      effect_tags=["shell_exec"], forged=True)

    # Premise: the forged-network hard-DENY must NOT be what stops this — it
    # returns a ToolResult instead of raising, so the card below has to be the
    # tool gate's.
    from systemu.runtime.action_governance import forged_network_denied
    assert forged_network_denied(tool) is None

    with pytest.raises(PendingOperatorDecision) as ei:
        asyncio.run(sb.execute_tool(tool.implementation_path,
                                    {"command": "dir"}, tool=tool))

    assert ei.value.dedup_key.startswith("tool:"), (
        f"expected a TOOL card, got {ei.value.dedup_key!r}")
    assert [g for g, _ in inbox.posted] == ["tool"]


# ── (iii) the clause-(d) subset bound ────────────────────────────────────────

def test_shell_tool_with_an_extra_approval_band_tag_is_not_exempt(tmp_path, inbox):
    """The exemption applies ONLY when ``shell_exec`` is the SOLE approval-band
    reason to card. Re-tag the same tool ``net_mutate`` and the delegation is no
    longer sound — the command gate scores a COMMAND, and has nothing to say
    about a network mutation — so the tool gate must card.
    """
    _assert_scorer_would_card({"shell_exec", "net_mutate"})
    _assert_command_gate_would_not_card("git status")

    sb, _store = _sandbox(tmp_path)
    tool = _make_tool(tmp_path, filename="run_command", name="run_command",
                      effect_tags=["shell_exec", "net_mutate"], forged=False)

    with pytest.raises(PendingOperatorDecision) as ei:
        asyncio.run(sb.execute_tool(tool.implementation_path,
                                    {"command": "git status"}, tool=tool))

    assert ei.value.dedup_key.startswith("tool:")


# ── (iv) clause (b): the command gate must actually have RUN ─────────────────

def test_tool_whose_filename_is_not_a_shell_tool_is_not_exempt(tmp_path, inbox):
    """``_maybe_gate_command`` keys on the FILENAME-derived ``tool_name``
    (``impl_path.stem``). For a tool whose file is not one of
    ``_SHELL_TOOL_NAMES`` the command gate returns immediately without scoring
    anything — so there is no command-gate decision to delegate to, and
    exempting here would delegate to a gate that never ran.
    """
    _assert_scorer_would_card({"shell_exec"}, tool_name="run_command")

    sb, _store = _sandbox(tmp_path)
    # file: helper_tool.py (tool_name="helper_tool") but Tool.name="run_command"
    tool = _make_tool(tmp_path, filename="helper_tool", name="run_command",
                      effect_tags=["shell_exec"], forged=False)

    with pytest.raises(PendingOperatorDecision) as ei:
        asyncio.run(sb.execute_tool(tool.implementation_path,
                                    {"command": "git status"}, tool=tool))

    assert ei.value.dedup_key.startswith("tool:")


# ── (v) clause (c): the Tool's own name must be a shell tool too ─────────────

def test_tool_whose_model_name_is_not_a_shell_tool_is_not_exempt(tmp_path, inbox):
    """Defense in depth against the two names disagreeing. ``Tool.name`` is what
    the ActionContext is scored under and what ``tool_signature`` is keyed on; a
    tool presenting itself as something other than a shell tool does not get a
    shell tool's exemption, whatever its file is called.
    """
    _assert_scorer_would_card({"shell_exec"}, tool_name="helper_tool")

    sb, _store = _sandbox(tmp_path)
    # file: run_command.py (tool_name="run_command") but Tool.name="helper_tool"
    tool = _make_tool(tmp_path, filename="run_command", name="helper_tool",
                      effect_tags=["shell_exec"], forged=False)

    with pytest.raises(PendingOperatorDecision) as ei:
        asyncio.run(sb.execute_tool(tool.implementation_path,
                                    {"command": "git status"}, tool=tool))

    assert ei.value.dedup_key.startswith("tool:")
