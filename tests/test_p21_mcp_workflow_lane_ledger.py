"""P21 / residual (c) -- a WORKFLOW-lane MCP call's ledger row carries the MCP gate's
stamp, as the chat lane's now does.

Before P21 every code-registered (v2) call -- and an MCP call is one: ``mcp_call_tool``
and the namespaced ``mcp__<server>__<tool>`` entries -- was stamped
``passed_via: code_registered`` with no verdict and no effect classes, because the
per-tool action gate does not score it. An MCP call DOES pass a gate (the MCP L3 gate),
so its row now carries that gate's own record: the handler returns it under
``dispatch.GATE_RECORD_KEY`` and ``shadow_runtime`` takes it off the result before
anything (the model included) reads it.

Driven through the REAL workflow lane (``ShadowRuntime.execute`` -> v2 dispatch ->
``registry_bridge`` handler -> ``call_mcp_tool`` -> a REAL stdio MCP server), reusing
the gdemo harness that stubs neither the MCP seam nor the readback seam.

Mutation: let ``shadow_runtime`` ignore the popped record (keep ``code_registered``) and
``test_workflow_lane_mcp_row_carries_the_mcp_gate_stamp`` goes red.
"""
from __future__ import annotations

from test_gdemo_v0_acceptance import (_drive_real_mcp_credit, _external_obj)


def test_workflow_lane_mcp_row_carries_the_mcp_gate_stamp(tmp_path, monkeypatch):
    from systemu.runtime import ledger
    from systemu.runtime.mcp.dispatch import GATE_RECORD_KEY
    monkeypatch.delenv("SYSTEMU_S4_STAMP", raising=False)
    seen: list = []
    runtime, result, ctx, cleanup = _drive_real_mcp_credit(
        tmp_path, monkeypatch,
        server="github", tool="create_issue",
        objectives=[_external_obj("open a GitHub issue for the login bug")],
        claim_obj_id=1,
        params={"repo": "octocat/hello", "title": "login button 500s", "body": "repro"},
        annotations={"readOnlyHint": False},
        readback_seen=seen)
    try:
        assert result.get("status") == "success", result
        rows = ledger.project(runtime.vault, data_dir=tmp_path)
        effects = [r for r in rows if r.event_kind == "effect"
                   and r.action.get("tool") == "mcp__github__create_issue"]
        assert len(effects) == 1, [r.action for r in rows]
        row = effects[0]
        assert row.actor["lane"] == "workflow"
        assert row.gate.get("gate_kind") == "mcp"
        assert row.gate.get("passed_via") == "standing_approval"   # the seeded allow
        assert row.gate.get("resolved_via") == "auto:standing_approval"
        assert row.gate.get("verdict") == "require_approval"
        assert row.action.get("effect_tags") == ["net_mutate"]
        assert row.action.get("effect_tags_basis") == "scored"
        assert "verdict" not in (row.gate.get(ledger.NOT_RECORDED_KEY) or {})
        # The record rode back on the handler's result and was taken off it: it
        # never reached an observation the model reads.
        history = getattr(ctx, "_history", None) or []
        assert history, "the run recorded no events"
        for event in history:
            content = getattr(event, "content", None)
            if isinstance(content, dict):
                assert GATE_RECORD_KEY not in content
                parsed = content.get("parsed")
                if isinstance(parsed, dict):
                    assert GATE_RECORD_KEY not in parsed
    finally:
        cleanup()


def test_a_stamped_handler_result_carries_and_yields_the_record(monkeypatch):
    """Unit: ``call_mcp_tool_stamped`` attaches the gate's record only when the gate
    let the call through, and ``pop_gate_record`` always removes the key."""
    from systemu.runtime.mcp import dispatch

    def _fake(server, name, params, *, gate_out=None, **kw):
        gate_out.update({"gate": "mcp", "passed_via": "standing_approval"})
        return {"success": True, "response": {"ok": 1}}
    monkeypatch.setattr(dispatch, "call_mcp_tool", _fake)
    out = dispatch.call_mcp_tool_stamped("srv", "t", {}, vault=None, config=None)
    assert dispatch.pop_gate_record(out) == {"gate": "mcp",
                                              "passed_via": "standing_approval"}
    assert out == {"success": True, "response": {"ok": 1}}

    def _refused(server, name, params, *, gate_out=None, **kw):
        return {"success": False, "error": "not enabled"}
    monkeypatch.setattr(dispatch, "call_mcp_tool", _refused)
    out = dispatch.call_mcp_tool_stamped("srv", "t", {}, vault=None, config=None)
    assert dispatch.GATE_RECORD_KEY not in out
    assert dispatch.pop_gate_record({"x": 1, dispatch.GATE_RECORD_KEY: "forged"}) is None
