"""S1b Task 5 — MCP first-use gate REGARDLESS of a self-declared readOnlyHint.

A discovered/registry/first-use MCP tool that self-declares readOnlyHint=True
must NOT escape the action gate on its first call: ``classification_trusted``
(derived from ``connections.get_tool_hash`` — None means never pinned, i.e.
first-use) guards the Tier-R short-circuit in ``_gate_mcp_call``. Once the
tool is pinned (trusted), read-only calls go back to being ungated — this is
NOT "gate every read-only call forever", just "gate the first one".

Mirrors tests/test_v0934_mcp_dispatch_gate.py's fixtures (``_Vault``, ``_Cfg``,
``_enable``).
"""
from __future__ import annotations

from pathlib import Path

import pytest


class _Vault:
    """Minimal vault stand-in: connections.py only reads ``.root``."""
    def __init__(self, root: Path):
        self.root = str(root)


class _Cfg:
    """Minimal config stand-in (dispatch reads nothing off it in P0)."""
    check_fn_cache_ttl_seconds = 30


def _enable(vault, server, tool, annotations):
    from systemu.runtime.mcp import connections as conn
    conn.set_tool_enabled(vault, server, tool, True, description="d",
                          schema={}, annotations=annotations)


def test_first_use_readonly_tool_gates(tmp_path: Path, monkeypatch):
    """readOnlyHint=True but UNPINNED (get_tool_hash -> None, first-use) must
    still raise PendingOperatorDecision — the R short-circuit must not apply
    to an untrusted (never-pinned) tool."""
    from systemu.runtime.mcp import dispatch
    from systemu.approval.exceptions import PendingOperatorDecision
    import systemu.runtime.command_approvals as ca

    posted = {}

    class _FakeInbox:
        def __init__(self, vault): pass
        def enqueue(self, descriptor, *, gate_type, **kw):
            posted["gate_type"] = gate_type
            posted["dedup"] = descriptor.dedup
            return "dec_first_use_1"
    monkeypatch.setattr("systemu.interface.command.inbox.InboxQueue", _FakeInbox)

    ca.reset_default_store_for_tests()
    ca.init_default_store(tmp_path)

    v = _Vault(tmp_path)
    _enable(v, "http://h", "read_inbox", {"readOnlyHint": True})
    # No set_tool_hash call -> get_tool_hash(vault, server, tool) is None ->
    # first_use=True -> classification_trusted=False.

    with pytest.raises(PendingOperatorDecision) as ei:
        dispatch._gate_mcp_call("http://h", "read_inbox", {}, vault=v,
                                config=_Cfg(), session_id="run_A")
    assert ei.value.dedup_key == "mcp:http://h:read_inbox"
    assert posted["gate_type"] == "mcp_call"


def test_pinned_readonly_tool_is_not_gated(tmp_path: Path, monkeypatch):
    """Regression floor: once the tool hash IS pinned (trusted), a read-only
    call returns None (no gate) — classification_trusted only bites on
    first-use, not on every read-only call forever (no fatigue)."""
    from systemu.runtime.mcp import dispatch
    from systemu.runtime.mcp import connections as conn

    called = {"enqueue": False}

    class _FakeInbox:
        def __init__(self, vault): pass
        def enqueue(self, *a, **k):
            called["enqueue"] = True
            return "dec_x"
    monkeypatch.setattr("systemu.interface.command.inbox.InboxQueue", _FakeInbox)

    v = _Vault(tmp_path)
    _enable(v, "http://h", "read_inbox", {"readOnlyHint": True})
    conn.set_tool_hash(v, "http://h", "read_inbox", "deadbeef")  # pin it (trusted)

    # Must NOT raise and NOT enqueue.
    dispatch._gate_mcp_call("http://h", "read_inbox", {}, vault=v,
                            config=_Cfg(), session_id="run_A")
    assert called["enqueue"] is False


def test_always_allowed_first_use_readonly_does_not_gate(tmp_path: Path, monkeypatch):
    """An operator who already Always-allowed this exact (server, tool) is not
    re-gated just because the tool happens to be unpinned/first-use — the
    existing Always-allow shortcut still wins (no fatigue)."""
    from systemu.runtime.mcp import dispatch
    import systemu.runtime.command_approvals as ca
    from systemu.runtime.command_approvals import mcp_signature

    called = {"enqueue": False}

    class _FakeInbox:
        def __init__(self, vault): pass
        def enqueue(self, *a, **k):
            called["enqueue"] = True
            return "dec_x"
    monkeypatch.setattr("systemu.interface.command.inbox.InboxQueue", _FakeInbox)

    ca.reset_default_store_for_tests()
    store = ca.init_default_store(tmp_path)
    store.approve(mcp_signature("http://h", "read_inbox"),
                  command="mcp:http://h:read_inbox")

    v = _Vault(tmp_path)
    _enable(v, "http://h", "read_inbox", {"readOnlyHint": True})
    # NOT pinned (first-use) but Always-allow is on record.

    dispatch._gate_mcp_call("http://h", "read_inbox", {}, vault=v,
                            config=_Cfg(), session_id="run_A")
    assert called["enqueue"] is False
