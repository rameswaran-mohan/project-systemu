"""R-P1 SEC-1 — REAL gate-creation-path integration tests.

The green synthetic-context suite (test_rp1_resolution_class.py) hand-built
context dicts that ALWAYS carried ``verdict`` + ``effect_tags``. But the REAL
gate-creation paths (``GateDescriptor.to_decision_context`` + the sandbox
``context_extras``) did NOT persist those keys — so the money/DENY floors were
DEAD on real input and DENY / money / destructive / forge gates got stamped
``remotely_resolvable`` (tap-to-approve from a phone). Synthetic dicts are
exactly why the green suite missed it.

These tests drive the WHOLE real chokepoint:

    GateDescriptor.from_*(...) → InboxQueue(vault).enqueue(...,
    context_extras=...) → to_decision_context → OperatorDecisionQueue.post →
    classify_resolution → persisted context["resolution_class"]

and assert the persisted class on the SAVED decision — the same bit
``resolve_from_channel`` later reads to decide whether a phone tap is allowed.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from systemu.interface.command.gate import GateDescriptor
from systemu.interface.command.inbox import InboxQueue


# ── real-path harness ─────────────────────────────────────────────────────────

def _fake_vault():
    """A vault shim that captures every ``save_decision`` and supports the
    push-tag ``load_index`` call the post() path makes."""
    v = MagicMock()
    v.load_index.return_value = []          # no other pending rows
    saved = []
    v.save_decision.side_effect = lambda d: saved.append(d)
    return v, saved


def _enqueue_and_get_ctx(descriptor, *, gate_type, context_extras=None):
    """Run the REAL enqueue→post→classify chokepoint; return the persisted
    context dict of the single saved decision."""
    vault, saved = _fake_vault()
    InboxQueue(vault).enqueue(
        descriptor, gate_type=gate_type, policy=None,
        context_extras=context_extras or {},
    )
    assert len(saved) == 1, "expected exactly one posted decision"
    # to_dict() is what actually persists — assert on the round-tripped bit.
    return saved[0].to_dict()["context"]


# ── Finding 1: a DENY tool gate must FLOOR (was stamped remotely_resolvable) ───

def test_real_deny_tool_gate_floors():
    d = GateDescriptor.from_tool(
        tool_name="wipe_disk", sig="sigDENY", verdict="deny",
        reason="explicit policy denial", effect_tags=["local_delete"])
    # The sandbox stamps verdict + effect_tags into context_extras (Part C).
    ctx = _enqueue_and_get_ctx(
        d, gate_type="tool",
        context_extras={"verdict": "deny", "effect_tags": ["local_delete"],
                        "tool_signature": "sigDENY"})
    assert ctx["resolution_class"] == "floor"


# ── Finding 2: a money_move/irreversible tool gate must FLOOR ──────────────────

def test_real_money_move_tool_gate_floors():
    d = GateDescriptor.from_tool(
        tool_name="pay_invoice", sig="sigMONEY", verdict="require_approval",
        effect_tags=["money_move"])
    ctx = _enqueue_and_get_ctx(
        d, gate_type="tool",
        context_extras={"verdict": "require_approval",
                        "effect_tags": ["money_move"],
                        "tool_signature": "sigMONEY"})
    assert ctx["resolution_class"] == "floor"


def test_real_irreversible_tool_gate_floors():
    d = GateDescriptor.from_tool(
        tool_name="drop_table", sig="sigIRR", verdict="require_approval",
        effect_tags=["irreversible"])
    ctx = _enqueue_and_get_ctx(
        d, gate_type="tool",
        context_extras={"verdict": "require_approval",
                        "effect_tags": ["irreversible"],
                        "tool_signature": "sigIRR"})
    assert ctx["resolution_class"] == "floor"


# ── Part C load-bearing: a BENIGN tool gate must STAY remotely_resolvable ──────
# (else Part B floors everything and R-P1 is inert — no gate is ever remote.)

def test_real_benign_tool_gate_stays_remote():
    d = GateDescriptor.from_tool(
        tool_name="post_status", sig="sigBENIGN", verdict="require_approval",
        effect_tags=["net_mutate"])
    ctx = _enqueue_and_get_ctx(
        d, gate_type="tool",
        context_extras={"verdict": "require_approval",
                        "effect_tags": ["net_mutate"],
                        "tool_signature": "sigBENIGN"})
    assert ctx["resolution_class"] == "remotely_resolvable"


def test_real_command_gate_floors_on_unclassified_effects():
    """TIGHTENED (was ``test_real_benign_command_gate_stays_remote``, expecting
    ``remotely_resolvable``).

    Two independent reasons this must floor, and BOTH are load-bearing:

    1. The shape asserted here is one the real producer never emits. A command
       gate only posts for a DESTRUCTIVE, non-approved command (the
       ``is_destructive_call`` early return skips provably read-only ones), so
       ``_maybe_gate_command`` hard-codes ``destructive: True`` in its
       context_extras — see tool_sandbox.py. Every real command gate was already
       floored by step 4; this test's ``destructive``-less extras were fiction.
    2. ``effect_tags: []`` — which the command producer DOES stamp verbatim,
       because shell commands are not effect-tag classified at that seam — is the
       ABSENCE of a classification, not a finding of "no effect", and now floors
       on its own (classify_resolution step 3).

    So no command gate is remotely resolvable today, by construction. The
    remote lane belongs to positively-classified benign TOOL gates —
    ``test_real_benign_tool_gate_stays_remote`` below is the shape that survives.
    """
    d = GateDescriptor.from_command(tool_name="run_command", command="ls -la")
    ctx = _enqueue_and_get_ctx(
        d, gate_type="command",
        context_extras={"verdict": "require_approval", "effect_tags": [],
                        "command": "ls -la", "cwd": ""})
    assert ctx["resolution_class"] == "floor"


# ── Part B: MISSING evidence → floor (the real bug: extras omit them) ──────────

def test_real_tool_gate_without_verdict_key_floors():
    # Simulate the PRE-FIX sandbox: context_extras carry NO verdict/effect_tags.
    d = GateDescriptor.from_tool(
        tool_name="post_status", sig="sigNOEV", verdict="require_approval",
        effect_tags=["net_mutate"])
    ctx = _enqueue_and_get_ctx(
        d, gate_type="tool",
        context_extras={"tool_signature": "sigNOEV"})  # no verdict/effect_tags
    assert ctx["resolution_class"] == "floor"


def test_real_tool_gate_with_verdict_but_no_effect_tags_floors():
    d = GateDescriptor.from_tool(
        tool_name="post_status", sig="sigNOTAGS", verdict="require_approval",
        effect_tags=["net_mutate"])
    ctx = _enqueue_and_get_ctx(
        d, gate_type="tool",
        context_extras={"verdict": "require_approval"})  # effect_tags absent
    assert ctx["resolution_class"] == "floor"


# ── Finding 5: a FORGE gate must FLOOR (forge not in remote set — Part A) ──────

def test_real_forge_gate_floors():
    d = GateDescriptor.from_forge(
        {"id": "tool_x", "name": "shiny_tool", "description": "does things",
         "status": "proposed"})
    ctx = _enqueue_and_get_ctx(d, gate_type="forge")
    assert ctx["resolution_class"] == "floor"


# ── Finding 3 (the second-rail types): scroll / dep must FLOOR (Part A) ────────

def test_real_scroll_gate_floors():
    scroll = MagicMock()
    scroll.name = "my_scroll"
    scroll.id = "scroll_1"
    d = GateDescriptor.from_scroll(scroll, summary="a scroll")
    ctx = _enqueue_and_get_ctx(d, gate_type="scroll")
    assert ctx["resolution_class"] == "floor"


def test_real_dep_gate_floors():
    d = GateDescriptor.from_dep(
        {"package": "requests", "first_seen_tool": "t", "first_seen_tool_id": "t1",
         "request_count": 1})
    ctx = _enqueue_and_get_ctx(d, gate_type="dep")
    assert ctx["resolution_class"] == "floor"


def test_real_mcp_call_gate_floors():
    d = GateDescriptor.from_mcp_call(
        server="s", tool="t", params={}, destructive=False)
    ctx = _enqueue_and_get_ctx(d, gate_type="mcp_call")
    assert ctx["resolution_class"] == "floor"


# ── Finding 4: a destructive command gate must FLOOR ──────────────────────────

def test_real_destructive_command_gate_floors():
    d = GateDescriptor.from_command(
        tool_name="run_command", command="rm -rf /important")
    # Defense-in-depth: the sandbox may stamp destructive=True for a destructive
    # call. Even with a clean verdict + empty effect_tags, destructive floors.
    ctx = _enqueue_and_get_ctx(
        d, gate_type="command",
        context_extras={"verdict": "require_approval", "effect_tags": [],
                        "destructive": True})
    assert ctx["resolution_class"] == "floor"
