"""R-A14a slice 2 — the MCP → S3/S4 verification LINKAGE (LIVE, decoupled from S4_STAMP).

Drives the REAL execute() loop via the test_s3_credit_wiring harness (the anti-dormancy
tripwire — NOT synthetic evidence dicts). A known-mutation MCP call that completes an
objective must be gated on the persisted ExternalEvidence.confirmed bit, produced by the
mcp modality REUSING verify() + the hardened api_readback — with SYSTEMU_S4_STAMP=off.

Invariants:
  * LIVE-CONSUMER AC — MCP mutation → verify() confirms → credited (S4_STAMP=off).
  * money-move invariant — a money-move MCP with only an inline/advisory signal (no
    hardened readback) → NOT credited + UNVERIFIED_EXTERNAL.
  * read / non-mutation MCP → no obligation, no evidence, credited via the normal path.
"""
from __future__ import annotations

import pytest

from test_s3_credit_wiring import _drive_live_credit, _EchoReadbackClient


# ─────────────────────────────────────────────────────────────────────────────
#  helpers
# ─────────────────────────────────────────────────────────────────────────────

def _mcp_tool(name):
    from systemu.core.models import Tool, ToolStatus, ToolType
    return Tool(id="tool_mcp", name=name, description="mcp mutation",
                tool_type=ToolType.PYTHON_FUNCTION, status=ToolStatus.DEPLOYED,
                enabled=True, implementation_path="vault/tools/implementations/x.py")


def _register_v2_mcp(name, *, is_action_tool):
    """Register a namespaced v2 MCP entry (registry_bridge-shape) so the credit seam's
    _known_mutation_mcp_entry resolves it. Returns a cleanup callable."""
    from systemu.runtime.tool_registry_v2 import registry
    registry.register(name=name, toolset="mcp", schema={"type": "object"},
                      handler=lambda **k: {"success": True},
                      is_action_tool=is_action_tool)

    def _cleanup():
        try:
            registry.unregister(name)
        except Exception:
            pass
    return _cleanup


def _mcp_result(payload):
    """Wrap a structured MCP payload EXACTLY as the L4 guard + v2 dispatch would, so
    result.parsed == {"success": True, "response": {guarded}} — the real shape the
    linkage unwraps."""
    from systemu.runtime.mcp.dispatch import _guard_mcp_output
    return {"success": True, "response": _guard_mcp_output(payload)}


def _external_obj(goal, **ov):
    from systemu.core.models import Objective
    base = dict(id=1, goal=goal, success_criteria="verified via readback",
                requires_external_verification=False)   # DECOUPLED: binder did NOT stamp
    base.update(ov)
    return Objective(**base)


# ─────────────────────────────────────────────────────────────────────────────
#  1. LIVE-CONSUMER AC — MCP mutation → verified → credited (S4_STAMP=off)
# ─────────────────────────────────────────────────────────────────────────────

def test_mcp_mutation_verified_and_credited_s4_off(tmp_path, monkeypatch):
    monkeypatch.delenv("SYSTEMU_S4_STAMP", raising=False)   # OFF (default)
    name = "mcp__github__create_issue"
    cleanup = _register_v2_mcp(name, is_action_tool=True)
    try:
        token = "issue-99-fresh"
        url = "https://api.github.test/repos/o/r/issues/99"
        directive = {"strategy": "api_readback", "expected_tokens": [token],
                     "readback_url": url, "submit_host": "api.github.test",
                     # non-money: the created resource is create-once (tool self-report OK)
                     "pre_submit_absent": True}
        tool_parsed = _mcp_result({"external": directive, "html_url": url, "number": 99})
        client = _EchoReadbackClient(echo_tokens=[token])

        runtime, result, ctx = _drive_live_credit(
            tmp_path, monkeypatch,
            objectives=[_external_obj("open a GitHub issue for the login bug")],
            claim_obj_id=1, tool_parsed=tool_parsed, api_client=client,
            tool=_mcp_tool(name))

        assert result.get("status") == "success", (
            "an MCP mutation whose result is machine-verified via api_readback must "
            f"CREDIT with S4_STAMP=off; got {result.get('status')}")
        store = getattr(ctx, "_external_evidence", {})
        ev = store.get("1") or store.get(1)
        assert ev and ev.get("confirmed") is True, (
            f"a confirmed ExternalEvidence must be persisted for the MCP mutation; store={store}")
        assert ev.get("method") == "api_readback"
        assert not ev.get("shadow"), "the MCP receipt is a LIVE credit, not a shadow record"
    finally:
        cleanup()


# ─────────────────────────────────────────────────────────────────────────────
#  2. money-move invariant — inline/advisory money-move MCP → NOT credited
# ─────────────────────────────────────────────────────────────────────────────

def test_money_move_mcp_inline_only_not_credited(tmp_path, monkeypatch):
    monkeypatch.delenv("SYSTEMU_S4_STAMP", raising=False)
    name = "mcp__pay__send_payment"
    cleanup = _register_v2_mcp(name, is_action_tool=True)
    try:
        obs = []
        tok = "pay-confirm-123"
        # INLINE api_readback (self-reported observed token; NO hardened readback_url) —
        # the money-move gate in verify() demotes it (double-submit hazard).
        directive = {"strategy": "api_readback", "expected_tokens": [tok],
                     "observed_tokens": [tok], "pre_submit_absent": True}
        tool_parsed = _mcp_result({"external": directive})

        runtime, result, ctx = _drive_live_credit(
            tmp_path, monkeypatch,
            objectives=[_external_obj("pay the $500 invoice via the payments API")],
            claim_obj_id=1, tool_parsed=tool_parsed, spy_obs=obs,
            tool=_mcp_tool(name))

        assert result.get("status") != "success", (
            "a money-move MCP with only an inline signal (no hardened readback) must "
            f"NOT credit; got {result.get('status')}")
        unv = [o for o in obs if isinstance(o, dict) and o.get("type") == "UNVERIFIED_EXTERNAL"]
        assert unv, f"expected an UNVERIFIED_EXTERNAL observation; saw {obs}"
        store = getattr(ctx, "_external_evidence", {})
        ev = store.get("1") or store.get(1)
        assert not (ev and ev.get("confirmed") is True), (
            f"an inline token must not confirm a money-move MCP; store={store}")
    finally:
        cleanup()


# ─────────────────────────────────────────────────────────────────────────────
#  2b. money-move invariant — operator_attest (advisory) money-move MCP → NOT credited
# ─────────────────────────────────────────────────────────────────────────────

def test_money_move_mcp_attest_only_not_credited(tmp_path, monkeypatch):
    monkeypatch.delenv("SYSTEMU_S4_STAMP", raising=False)
    name = "mcp__pay__wire_funds"
    cleanup = _register_v2_mcp(name, is_action_tool=True)
    try:
        obs = []
        directive = {"strategy": "operator_attest", "attested": True}
        tool_parsed = _mcp_result({"external": directive})

        runtime, result, ctx = _drive_live_credit(
            tmp_path, monkeypatch,
            objectives=[_external_obj("wire $2000 to the vendor account")],
            claim_obj_id=1, tool_parsed=tool_parsed, spy_obs=obs,
            tool=_mcp_tool(name))

        assert result.get("status") != "success", (
            "operator_attest alone must NEVER credit a money-move MCP; "
            f"got {result.get('status')}")
        store = getattr(ctx, "_external_evidence", {})
        ev = store.get("1") or store.get(1)
        assert not (ev and ev.get("confirmed") is True)
    finally:
        cleanup()


# ─────────────────────────────────────────────────────────────────────────────
#  3. a READ MCP call → no obligation, no evidence, credited via the normal path
# ─────────────────────────────────────────────────────────────────────────────

def test_read_mcp_call_no_obligation_byte_identical(tmp_path, monkeypatch):
    monkeypatch.delenv("SYSTEMU_S4_STAMP", raising=False)
    name = "mcp__github__list_issues"
    cleanup = _register_v2_mcp(name, is_action_tool=False)   # read-only → not a mutation
    try:
        # even if the result carried an api_readback directive, a READ sets no obligation.
        tool_parsed = _mcp_result({"external": {"strategy": "api_readback",
                                                "expected_tokens": ["x"],
                                                "readback_url": "https://h/x",
                                                "submit_host": "h"}})
        runtime, result, ctx = _drive_live_credit(
            tmp_path, monkeypatch,
            objectives=[_external_obj("list the open issues")],
            claim_obj_id=1, tool_parsed=tool_parsed, tool=_mcp_tool(name))

        assert result.get("status") == "success", (
            "a READ MCP call must credit via the normal path (no obligation); "
            f"got {result.get('status')}")
        store = getattr(ctx, "_external_evidence", {}) or {}
        assert not store, (
            f"a read MCP call must write NO ExternalEvidence; store={store}")
    finally:
        cleanup()
