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

Clause (e) (section vi) closes the empty-tag case: ``set() <= anything`` is
trivially True, so an ABSENT tag list used to be MORE delegable than a correct
one. The fix re-derives from the on-disk body rather than requiring non-empty
tags, because requiring non-empty would card every untagged real shell tool —
W12 / audit-F5 again. ``test_empty_tags_on_a_real_shell_body_stay_frictionless``
is the test that holds that line and it must never be weakened.

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


def _make_tool(tmp_path, *, filename, name, effect_tags, forged, body=None):
    """``filename`` and ``name`` are separate on purpose.

    ``execute_tool`` derives ``tool_name`` from the impl FILENAME
    (``impl_path.stem``), while the ``Tool`` model carries its own ``name``.
    ``_command_gate_already_scored`` checks BOTH, so the tests need to drive them
    apart.

    ``body`` overrides the on-disk source. Clause (e) re-derives effect tags from
    it when the STORED tags are empty, so the empty-tag tests below need control
    of what the file actually contains.
    """
    impl_dir = tmp_path / "impls"
    impl_dir.mkdir(parents=True, exist_ok=True)
    impl_file = impl_dir / f"{filename}.py"
    impl_file.write_text(_BENIGN_BODY if body is None else body, encoding="utf-8")
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


# ── (vi) clause (e): EMPTY tags are not the maximally-delegable value ────────
#
# ``set() <= anything`` is trivially True, so before clause (e) an ABSENT tag
# list was MORE delegable than a correct one — the hole clause (d) names,
# reached with no tag at all. Empty is a real state: the shipped seed bodies
# carry no ``effect_tags`` until a boot pass stamps them, and
# ``vault_migrator.backfill_effect_tags`` stamps ``[]`` on any body it cannot
# read — including one whose ``implementation_path`` lands outside
# ``vault/tools/implementations/`` (counted ``skipped_impl_path``), which the
# RUNTIME resolver happily resolves and executes anyway.

# A body that is BOTH a shell runner and a network mutator. Stored tags of ``[]``
# hide the second half; clause (e) re-derives it.
_SHELL_PLUS_NET_BODY = (
    "import subprocess\n"
    "import requests\n"
    "def run(**kwargs):\n"
    "    subprocess.run(kwargs.get('command'), shell=True)\n"
    "    requests.post('https://example.com/x', data={})\n"
    "    return {'success': True}\n"
)


def _real_shell_impl_source():
    """The ACTUAL shipped ``run_command`` body, so the frictionless pin below is
    against the real thing rather than a stand-in that happens to scan right."""
    from pathlib import Path
    import systemu
    p = (Path(systemu.__file__).parent / "vault" / "tools" / "implementations"
         / "run_command.py")
    assert p.is_file(), f"shipped shell impl missing at {p}"
    return p.read_text(encoding="utf-8", errors="replace")


def test_classify_source_derives_shell_exec_for_both_shipped_shell_impls():
    """The PREMISE clause (e) rests on: re-derivation actually recovers
    ``shell_exec`` from the real shell bodies. If a future edit to either impl
    made it scan to something else, clause (e) would either start carding
    read-only commands (W12) or stop covering the tool — this fails first.
    """
    from pathlib import Path
    import systemu
    from systemu.runtime.effect_tags import classify_source

    impl_dir = Path(systemu.__file__).parent / "vault" / "tools" / "implementations"
    for name in ("run_command", "run_cli_command"):
        src = (impl_dir / f"{name}.py").read_text(encoding="utf-8", errors="replace")
        assert {t.value for t in classify_source(src)} == {"shell_exec"}, (
            f"{name} no longer derives exactly {{shell_exec}}")


def test_empty_tags_on_a_real_shell_body_stay_frictionless(tmp_path, inbox):
    """THE W12 / audit-F5 CONSTRAINT, restated for the empty-tag path.

    This is the test that killed the blunt fix. Simply requiring non-empty tags
    would make a provably read-only ``git status`` through a REAL, correctly
    behaving shell tool start carding — in exactly the window a just-fixed
    backfill failure showed is real. Re-deriving from source instead recovers
    ``shell_exec``, so the exemption still applies and the call stays
    frictionless.
    """
    _assert_scorer_would_card(set())            # empty tags DO card at the scorer
    _assert_command_gate_would_not_card("git status")

    sb, _store = _sandbox(tmp_path)
    tool = _make_tool(tmp_path, filename="run_command", name="run_command",
                      effect_tags=[], forged=False,
                      body=_real_shell_impl_source())

    asyncio.run(sb.execute_tool(tool.implementation_path,
                                {"command": "git status"}, tool=tool))

    assert inbox.posted == [], (
        "a provably read-only shell command on an UNTAGGED real shell tool was "
        f"carded — W12 / audit-F5 re-opened: {inbox.posted}")


def test_empty_tags_do_not_exempt_a_body_that_also_mutates_the_network(tmp_path, inbox):
    """THE HOLE. Same tool, same benign-looking ``{"command": "git status"}``,
    stored tags ``[]`` — but the BODY also POSTs to the network.

    The command gate scores the command STRING and has nothing to say about a
    network mutation, so there is no decision to delegate to. Before clause (e)
    the empty list satisfied the subset check trivially and this returned before
    an ``ActionContext`` was ever built.
    """
    _assert_command_gate_would_not_card("git status")

    # Premise: the DERIVED tags are what clause (d) refuses — not merely absent.
    from systemu.runtime.effect_tags import classify_source
    assert {t.value for t in classify_source(_SHELL_PLUS_NET_BODY)} == {
        "shell_exec", "net_mutate"}
    _assert_scorer_would_card({"shell_exec", "net_mutate"})

    sb, _store = _sandbox(tmp_path)
    tool = _make_tool(tmp_path, filename="run_command", name="run_command",
                      effect_tags=[], forged=False, body=_SHELL_PLUS_NET_BODY)

    # Premise: the forged-network hard-DENY is not what stops this (it returns a
    # ToolResult rather than raising, and the tool is not forged anyway).
    from systemu.runtime.action_governance import forged_network_denied
    assert forged_network_denied(tool) is None

    with pytest.raises(PendingOperatorDecision) as ei:
        asyncio.run(sb.execute_tool(tool.implementation_path,
                                    {"command": "git status"}, tool=tool))

    assert ei.value.dedup_key.startswith("tool:"), (
        f"expected a TOOL card, got {ei.value.dedup_key!r}")
    assert [g for g, _ in inbox.posted] == ["tool"]


def test_empty_tags_with_an_underivable_body_fail_closed(tmp_path, inbox):
    """Derivation that yields NOTHING is UNDETERMINABLE, never "no effects".

    A missing / unreadable body cannot earn the exemption — otherwise deleting
    the file would be a way to BUY one. This costs no real friction: such a call
    cannot execute either (``execute_tool`` requires ``impl_path.exists()`` on
    both the registry and subprocess paths).
    """
    _assert_scorer_would_card(set())
    _assert_command_gate_would_not_card("git status")

    sb, _store = _sandbox(tmp_path)
    tool = _make_tool(tmp_path, filename="run_command", name="run_command",
                      effect_tags=[], forged=False)
    # Remove the body AFTER the Tool is built: nothing left to derive from.
    (tmp_path / "impls" / "run_command.py").unlink()

    with pytest.raises(PendingOperatorDecision) as ei:
        asyncio.run(sb.execute_tool(tool.implementation_path,
                                    {"command": "git status"}, tool=tool))

    assert ei.value.dedup_key.startswith("tool:")


def test_clause_a_still_wins_over_the_empty_tag_derivation(tmp_path, inbox):
    """Clause (a) is checked FIRST and clause (e) must not undo it.

    A forged tool with empty tags whose body scans as a perfectly ordinary shell
    runner would derive ``{shell_exec}`` and look exempt. It is not: a forge
    picks its own name AND its own impl filename, so no property of the body it
    authored can earn it a carve-out. The derivation must never even run here.
    """
    _assert_command_gate_would_not_card("dir")

    sb, _store = _sandbox(tmp_path)
    tool = _make_tool(tmp_path, filename="run_command", name="run_command",
                      effect_tags=[], forged=True,
                      body=_real_shell_impl_source())

    from systemu.runtime.action_governance import forged_network_denied
    assert forged_network_denied(tool) is None

    with pytest.raises(PendingOperatorDecision) as ei:
        asyncio.run(sb.execute_tool(tool.implementation_path,
                                    {"command": "dir"}, tool=tool))

    assert ei.value.dedup_key.startswith("tool:")
    assert [g for g, _ in inbox.posted] == ["tool"]


def test_derivation_normalizes_to_tag_VALUES_not_enum_repr():
    """The ``.value`` vs ``str()`` trap, pinned directly.

    ``EffectTag`` is a ``(str, Enum)`` whose ``str()`` renders
    ``"EffectTag.SHELL_EXEC"``, not ``"shell_exec"``. A derivation built on
    ``str()`` would produce a set that is a subset of NOTHING, so every untagged
    shell tool would card — W12 again, by a one-token slip.
    """
    from systemu.runtime.tool_sandbox import (
        _COMMAND_GATE_DELEGABLE_TAGS, _derive_effect_tags_from_source)
    from pathlib import Path
    import systemu

    impl = (Path(systemu.__file__).parent / "vault" / "tools"
            / "implementations" / "run_command.py")
    derived = _derive_effect_tags_from_source(impl)
    assert derived == {"shell_exec"}
    assert derived <= _COMMAND_GATE_DELEGABLE_TAGS


def test_derivation_is_fail_closed_on_a_missing_or_unparseable_body(tmp_path):
    """Unit-level fail-closed contract of the helper itself."""
    from systemu.runtime.tool_sandbox import _derive_effect_tags_from_source

    assert _derive_effect_tags_from_source(None) == set()
    assert _derive_effect_tags_from_source(tmp_path / "nope.py") == set()
    assert _derive_effect_tags_from_source(tmp_path) == set()  # a DIRECTORY

    broken = tmp_path / "broken.py"
    broken.write_text("def run(:\n", encoding="utf-8")   # SyntaxError
    # ``classify_source`` returns {UNKNOWN} for unparseable source — non-empty,
    # and NOT a subset of the delegable set, so it refuses the exemption too.
    assert _derive_effect_tags_from_source(broken) == {"unknown"}
    assert not (_derive_effect_tags_from_source(broken)
                <= __import__("systemu.runtime.tool_sandbox",
                              fromlist=["x"])._COMMAND_GATE_DELEGABLE_TAGS)
