"""S1b (THE CRUX) — the live per-tool action gate inside ToolSandbox.

Task 3: ``ToolSandbox.execute_tool(..., tool=<Tool>)`` now builds an
``ActionContext``, runs ``evaluate_action``, and — when the verdict is
REQUIRE_APPROVAL/DENY and the tool's signature isn't already approved — posts a
``gate_type='tool'`` gate card and raises the SAME ``PendingOperatorDecision``
the command gate raises, so the existing quick-lane park/poll/resume machinery
catches it. A benign local (ALLOW) tool runs unchanged.

Mirrors ``tests/test_v0932_command_gate.py`` for ToolSandbox construction +
``PendingOperatorDecision`` import.
"""
import asyncio
import hashlib
from pathlib import Path

import pytest

from systemu.approval.exceptions import PendingOperatorDecision
from systemu.core.models import Tool, ToolType
from systemu.runtime.command_approvals import CommandApprovalStore, tool_signature


# ── construction helper (mirrors test_v0932_command_gate._sandbox_with_store) ──

def _sandbox_with_store(tmp_path, vault=None):
    from systemu.runtime.tool_sandbox import ToolSandbox
    store = CommandApprovalStore(tmp_path / "command_approvals.json")
    sb = ToolSandbox(str(tmp_path), vault=vault, command_approvals=store)
    return sb, store


_BENIGN_BODY = "def run():\n    return {'success': True}\n"


def _make_tool(tmp_path, *, name, effect_tags, body=_BENIGN_BODY):
    """Write a real impl file on disk and return a Tool pointing at it (relative
    to vault_root.parent, matching execute_tool's resolution). vault_root is
    tmp_path (str-constructed sandbox), so vault_root.parent is tmp_path.parent —
    write the impl under tmp_path.parent so the relative path resolves."""
    impl_dir = tmp_path.parent / "impls"
    impl_dir.mkdir(parents=True, exist_ok=True)
    impl_file = impl_dir / f"{name}.py"
    impl_file.write_text(body, encoding="utf-8")
    rel = impl_file.relative_to(tmp_path.parent)
    return Tool(
        id=f"tool_{name}",
        name=name,
        description=f"test tool {name}",
        tool_type=ToolType.PYTHON_FUNCTION,
        implementation_path=str(rel),
        effect_tags=list(effect_tags),
        version=1,
    ), impl_file


def _expected_sig(impl_file: Path, *, name, effect_tags, host_class):
    body_hash = hashlib.sha1(impl_file.read_bytes()).hexdigest()
    return tool_signature(name, body_hash, set(effect_tags), host_class=host_class)


# ── Test 1: an effectful (send_message) forged tool PARKS ─────────────────────

def test_effectful_forged_tool_parks(tmp_path, monkeypatch):
    class _FakeInbox:
        def __init__(self, vault):
            pass

        def enqueue(self, descriptor, *, gate_type, **kw):
            return "dec_tool_1"

    monkeypatch.setattr("systemu.interface.command.inbox.InboxQueue", _FakeInbox)

    sb, _store = _sandbox_with_store(tmp_path, vault=object())
    tool, _impl = _make_tool(tmp_path, name="send_slack", effect_tags=["send_message"])

    with pytest.raises(PendingOperatorDecision) as ei:
        asyncio.run(sb.execute_tool(tool.implementation_path, {}, tool=tool))

    assert ei.value.dedup_key.startswith("tool:")


# ── Test 2: a benign local (ALLOW) tool RUNS (no gate) ────────────────────────

def test_benign_local_tool_runs(tmp_path, monkeypatch):
    called = {"enqueue": False}

    class _FakeInbox:
        def __init__(self, vault):
            pass

        def enqueue(self, *a, **k):
            called["enqueue"] = True
            return "dec_x"

    monkeypatch.setattr("systemu.interface.command.inbox.InboxQueue", _FakeInbox)

    sb, _store = _sandbox_with_store(tmp_path, vault=object())
    # A genuinely-local tool (local_read) → ALLOW. (Empty effect_tags is UNKNOWN
    # by design — dangerous-until-proven — so it would gate; that's correct, not
    # "benign".) The name has no escalating verb, so it stays local-only.
    tool, _impl = _make_tool(tmp_path, name="read_a_file", effect_tags=["local_read"])

    # No registry attached + impl exists → subprocess path runs it; the point is
    # ONLY that no gate was posted (verdict ALLOW).
    asyncio.run(sb.execute_tool(tool.implementation_path, {}, tool=tool))
    assert called["enqueue"] is False


# ── Test 3: an already-approved signature SKIPS the gate ──────────────────────

def test_always_allowed_signature_skips_gate(tmp_path, monkeypatch):
    called = {"enqueue": False}

    class _FakeInbox:
        def __init__(self, vault):
            pass

        def enqueue(self, *a, **k):
            called["enqueue"] = True
            return "dec_x"

    monkeypatch.setattr("systemu.interface.command.inbox.InboxQueue", _FakeInbox)

    sb, store = _sandbox_with_store(tmp_path, vault=object())
    tool, impl = _make_tool(tmp_path, name="send_email", effect_tags=["send_message"])

    # Pre-approve the EXACT signature the gate will compute. host_class is
    # DEFERRED (no host resolver yet) so it is unconditionally "".
    sig = _expected_sig(impl, name="send_email",
                        effect_tags=["send_message"], host_class="")
    store.approve(sig, command="send_email")

    asyncio.run(sb.execute_tool(tool.implementation_path, {}, tool=tool))
    assert called["enqueue"] is False


# ── Test 4: the gate posts gate_type='tool' with tool_signature in extras ─────

def test_gate_posts_tool_gate_type(tmp_path, monkeypatch):
    posted = {}

    class _FakeInbox:
        def __init__(self, vault):
            pass

        def enqueue(self, descriptor, *, gate_type, context_extras=None, **kw):
            posted["gate_type"] = gate_type
            posted["context_extras"] = context_extras or {}
            posted["dedup"] = descriptor.dedup
            return "dec_tool_2"

    monkeypatch.setattr("systemu.interface.command.inbox.InboxQueue", _FakeInbox)

    sb, _store = _sandbox_with_store(tmp_path, vault=object())
    tool, _impl = _make_tool(tmp_path, name="post_update", effect_tags=["net_mutate"])

    with pytest.raises(PendingOperatorDecision):
        asyncio.run(sb.execute_tool(tool.implementation_path, {}, tool=tool))

    assert posted["gate_type"] == "tool"
    assert "tool_signature" in posted["context_extras"]
    assert posted["dedup"].startswith("tool:")


# ── Test 5: a net_read-only tool RUNS (ALLOW) — the host-flag regression ──────

def test_net_read_only_tool_runs(tmp_path, monkeypatch):
    """A forged tool tagged ONLY net_read is the frictionless-majority ALLOW
    (test_action_governance::test_net_read_allow). The gate must NOT synthesize
    target_is_network from the net tag — doing so escalated net_read → net_mutate
    → REQUIRE_APPROVAL, over-gating every network-reading tool (weather lookups,
    page fetches, API GETs). The host signal is deferred until a real resolver
    lands; net_read stays ALLOW."""
    called = {"enqueue": False}

    class _FakeInbox:
        def __init__(self, vault):
            pass

        def enqueue(self, *a, **k):
            called["enqueue"] = True
            return "dec_x"

    monkeypatch.setattr("systemu.interface.command.inbox.InboxQueue", _FakeInbox)

    sb, _store = _sandbox_with_store(tmp_path, vault=object())
    tool, _impl = _make_tool(tmp_path, name="fetch_weather", effect_tags=["net_read"])

    # Must NOT raise (ALLOW) and must NOT post a gate.
    asyncio.run(sb.execute_tool(tool.implementation_path, {}, tool=tool))
    assert called["enqueue"] is False
